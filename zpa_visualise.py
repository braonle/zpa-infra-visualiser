#!/usr/bin/env python3
"""Pull ZPA infrastructure objects and render an interactive dependency graph.

The chain this visualises:

    App Connector -> App Connector Group -> Server Group -> App Segment -> Segment Group

Fetching needs the network (it is a SaaS API), but everything else is offline:
the generated HTML inlines vis-network, so it opens over file:// on a
disconnected machine, and --from-json re-renders an earlier dump with no API
call at all.

Modes (see README.md for full usage):

    --download-vendor   one-time fetch of vis-network into vendor/ (needs internet)
    (default)           live pull from the ZPA API
    --from-json PATH    offline re-render of an earlier dump

Errors from the HTTP and ZPA libraries are deliberately left unhandled, so a
failure surfaces with its original type and traceback rather than a summary.
Only the script's own preconditions raise a tidy message.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import re
import sys
import urllib.request
from collections import Counter
from pathlib import Path

from constants import (
    CLOUDS,
    COLLECTIONS,
    CREDENTIAL_VARS,
    CUSTOMER_PATH,
    DATA_PLACEHOLDER,
    DETAIL_SPECS,
    LEVEL_BY_KEY,
    LINKS,
    LT_SLASH,
    LT_SLASH_ESCAPED,
    MIN_VENDOR_BYTES,
    TEMPLATE_FILE,
    TYPE_BY_KEY,
    TYPES,
    UNKNOWN_ORG,
    VENDOR_FILE,
    VENDOR_PLACEHOLDER,
    VIS_NETWORK_URL,
)

logger = logging.getLogger("zpa_visualise")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

class _CliFormatter(logging.Formatter):
    """Bare lines at INFO, level-prefixed above it."""

    def format(self, record):
        if record.levelno <= logging.INFO:
            return record.getMessage()
        return record.levelname.lower() + ": " + record.getMessage()


def configure_logging(verbose=False):
    """Send this module's output to stderr, leaving stdout clean.

    Only our own logger is configured by default, so the HTTP library's
    handlers are left alone. ``--verbose`` also opens up the root logger,
    which surfaces the request detail the library emits.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_CliFormatter())
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    if verbose:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr, force=True)


# --------------------------------------------------------------------------- #
# Field access
# --------------------------------------------------------------------------- #

def pick(obj, *names, default=None):
    """First of ``names`` with a non-null value, else ``default``.

    Two reasons this is not just ``dict.get``: some fields appear under more
    than one spelling in the same response (an app segment carries both
    ``tcpPortRange`` and ``tcpPortRanges``), and a field present but
    explicitly null should fall back to the default rather than yield None.
    """
    for name in names:
        value = obj.get(name)
        if value is not None:
            return value
    return default


def refs(obj, key):
    """IDs a field points at, whether it holds one id or nested objects.

    ZPA expresses the same relationship either way depending on the endpoint,
    so both shapes collapse to a list of id strings here.
    """
    value = obj.get(key)
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, list):
        return [str(child["id"]) for child in value
                if isinstance(child, dict) and child.get("id") is not None]
    return [] if value is None else [str(value)]


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #

def load_credential_file(path):
    """Read KEY=VALUE lines into the environment, literally.

    Deliberately not done by sourcing the file in a shell. A shell expands
    ``$`` inside double quotes, and zsh treats glob metacharacters such as
    ``[`` or ``?`` in an unquoted value as a pattern and abandons the
    assignment outright. Either way a secret containing those characters is
    silently truncated, and the only symptom is an opaque 401. Nothing here
    expands or splits: the text after the first ``=`` is the value.

    Naming a file is an explicit instruction, so it overrides whatever is
    already exported. Otherwise a value mangled by an earlier ``source`` of
    the same file would shadow the correct one.
    """
    path = Path(path)
    loaded, replaced = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if not key:
            continue
        if os.environ.get(key) not in (None, value):
            replaced.append(key)
        os.environ[key] = value
        loaded.append(key)

    if loaded:
        logger.info("Loaded %s from %s", ", ".join(loaded), path)
    if replaced:
        logger.warning("%s differed from the exported value and was replaced "
                       "by the file", ", ".join(replaced))
    return loaded


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def _talker_class():
    """ZpaTalker with its pagination helper repaired.

    zscaler-api-talkers 6.4.1 declares
    ``_obtain_all_results(self, url, url_params)`` with no default for
    ``url_params``, while all 17 call sites pass only ``url`` -- so every
    list-all method raises TypeError out of the box. Supplying the default in
    a subclass fixes all of them without patching the installed package. The
    parent already treats ``None`` as "start at page 1, pagesize 500".
    """
    from zscaler_api_talkers import ZpaTalker

    class Patched(ZpaTalker):
        def _obtain_all_results(self, url, url_params=None):
            return super()._obtain_all_results(url, url_params)

    return Patched


def resolve_cloud(cloud):
    """A cloud name, or a full base URL passed straight through."""
    if cloud.lower().startswith("http"):
        return cloud.rstrip(chr(47))
    if cloud.upper() not in CLOUDS:
        raise SystemExit("error: unknown cloud " + repr(cloud) + ". Use one of: "
                         + ", ".join(sorted(CLOUDS)) + ", or a full base URL.")
    return CLOUDS[cloud.upper()]


def fetch_org_name(talker):
    """The tenant's organisation name, from its customer record.

    No collection endpoint carries it and ZpaTalker has no method for the
    customer record, so this builds the one URL. A restricted API role may be
    refused it, leaving no name for the caller to fall back from.
    """
    url = chr(47) + chr(47).join(CUSTOMER_PATH + (str(talker.customer_id),))
    response = talker.hp_http.get_call(url, headers=talker.header,
                                       error_handling=False)
    return response.json().get("name") if response.ok else None


def org_folder(name):
    """Turn an organisation name into one safe path segment.

    Any run of characters outside ``[A-Za-z0-9._]`` collapses to a single
    dash, so "Acme Corp - EMEA" becomes "Acme-Corp-EMEA" rather than keeping
    the original dash and the spaces either side of it. Case is kept, and the
    result cannot escape its parent directory.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._]+", "-", (name or "").strip()).strip("-._")
    return cleaned or UNKNOWN_ORG


def fetch_all(cloud):
    """Pull all five collections, plus the organisation name."""
    missing = [v for v in CREDENTIAL_VARS if not os.environ.get(v)]
    if missing:
        raise SystemExit("error: missing credentials: " + ", ".join(missing)
                         + "\nExport them, or pass --creds-file (see README).")

    talker = _talker_class()(
        # ZpaTalker annotates customer_id as int, but only ever interpolates
        # it into a URL, so the string form is what we want.
        customer_id=os.environ["ZPA_CUSTOMER_ID"],
        cloud=resolve_cloud(cloud),
        client_id=os.environ["ZPA_CLIENT_ID"],
        client_secret=os.environ["ZPA_CLIENT_SECRET"],
    )

    org = fetch_org_name(talker)
    if org:
        logger.info("Organisation: %s", org)

    # Carried in the dump so --from-json lands in the same folder later.
    raw = {"_org": org}
    for json_key, _type_key, method in COLLECTIONS:
        raw[json_key] = getattr(talker, method)()
        logger.info("  %-16s %5d", json_key, len(raw[json_key]))
    return raw


# --------------------------------------------------------------------------- #
# Graph construction
# --------------------------------------------------------------------------- #

def _fmt(value, kind):
    """Render a field value for the detail panel, or None to omit the row."""
    if value is None or value == "" or value == [] or value == {}:
        return None
    if kind == "bool":
        return "Yes" if _truthy(value) else "No"
    if kind == "count":
        return str(len(value) if isinstance(value, (list, dict)) else value)
    if kind == "list":
        return ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
    if kind == "ports":
        return _fmt_ports(value)
    return str(value)


def _fmt_ports(value):
    """ZPA gives port ranges either as [{from,to}] or as a flat [from, to, ...]."""
    if not isinstance(value, list) or not value:
        return None
    if all(isinstance(v, dict) for v in value):
        pairs = [(v.get("from"), v.get("to")) for v in value]
    else:
        flat = [str(v) for v in value]
        pairs = list(zip(flat[::2], flat[1::2]))
        if len(flat) % 2:
            pairs.append((flat[-1], flat[-1]))
    return ", ".join(str(lo) if str(lo) == str(hi) else f"{lo}-{hi}"
                     for lo, hi in pairs) or None


def _details(type_key, rec):
    rows = [[label, _fmt(pick(rec, *names), kind)]
            for label, names, kind in DETAIL_SPECS[type_key]]
    return [row for row in rows if row[1] is not None]


class GraphBuilder:
    """Collects nodes and edges, tolerating references to absent objects."""

    def __init__(self):
        self.nodes = {}
        self.edges = set()

    def placeholder(self, type_key, obj_id):
        """Reference an object no record has been seen for -- yet, or at all."""
        nid = TYPE_BY_KEY[type_key]["prefix"] + ":" + str(obj_id)
        self.nodes.setdefault(nid, {
            "id": nid,
            "type": type_key,
            "level": LEVEL_BY_KEY[type_key],
            "rawId": str(obj_id),
            "name": f"(not returned) {obj_id}",
            "enabled": True,
            "missing": True,
            "detail": [],
        })
        return nid

    def real(self, type_key, rec):
        """Record a fetched object, upgrading any placeholder."""
        obj_id = rec.get("id")
        if obj_id is None:
            return
        self.nodes[self.placeholder(type_key, obj_id)].update(
            name=str(pick(rec, "name", default=f"(unnamed) {obj_id}")),
            enabled=_truthy(pick(rec, "enabled", default=True)),
            missing=False,
            detail=_details(type_key, rec),
        )

    def link(self, src_type, src_id, dst_type, dst_id):
        if src_id not in (None, "") and dst_id not in (None, ""):
            self.edges.add((self.placeholder(src_type, src_id),
                            self.placeholder(dst_type, dst_id)))


def build_graph(raw):
    """Turn the five raw collections into nodes and edges."""
    records = {type_key: raw.get(json_key) or []
               for json_key, type_key, _method in COLLECTIONS}

    g = GraphBuilder()
    for type_key, items in records.items():
        for rec in items:
            g.real(type_key, rec)

    for upstream, downstream, down_field, up_field in LINKS:
        for rec in records[upstream]:
            for other in refs(rec, down_field):
                g.link(upstream, rec.get("id"), downstream, other)
        for rec in records[downstream]:
            for other in refs(rec, up_field):
                g.link(upstream, other, downstream, rec.get("id"))

    nodes = sorted(g.nodes.values(), key=lambda n: (n["level"], n["name"].lower()))
    counts = Counter(n["type"] for n in nodes)
    return {
        "types": TYPES,
        "nodes": nodes,
        "edges": [{"from": a, "to": b} for a, b in sorted(g.edges)],
        "counts": {t["key"]: counts[t["key"]] for t in TYPES},
        "missing": sum(n["missing"] for n in nodes),
    }


# --------------------------------------------------------------------------- #
# Vendored library
# --------------------------------------------------------------------------- #

def download_vendor(dest=VENDOR_FILE):
    """Fetch the graph library once.

    No SSL context is built here on purpose. ``pip_system_certs`` patches
    ``ssl.create_default_context`` to use the operating system trust store,
    which both fixes framework Python's missing CA store on macOS and keeps
    an inspecting proxy's root CA trusted -- likely to matter to anyone
    running ZPA. Pinning certifi's own root list instead would undo that.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading %s", VIS_NETWORK_URL)
    with urllib.request.urlopen(VIS_NETWORK_URL, timeout=60) as response:
        payload = response.read()
    # Checked before writing, so a captive portal or an error page cannot
    # overwrite a good copy with something that renders a blank page.
    if len(payload) < MIN_VENDOR_BYTES:
        raise SystemExit(
            "error: received only " + format(len(payload), ",") + " bytes, too "
            "small to be the vis-network library. Check outbound access and retry.")
    dest.write_bytes(payload)
    logger.info("Wrote %s (%s bytes)", dest, format(len(payload), ","))
    return dest


def read_vendor(path=VENDOR_FILE):
    """The vendored library, with any close-script tag escaped.

    A literal one would terminate the inline <script> block early.
    """
    if not path.is_file():
        raise SystemExit("error: missing " + str(path)
                         + "\nRun once with --download-vendor to fetch it.")
    return path.read_text(encoding="utf-8").replace(
        LT_SLASH + "script", LT_SLASH_ESCAPED + "script")


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def render_html(graph, meta, vendor_js, template_path=None):
    template = Path(template_path or TEMPLATE_FILE).read_text(encoding="utf-8")
    # Every '<' in JSON is inside a string, so rewriting the close-tag opener
    # to its valid JSON escape can break neither the document nor the parse.
    data_json = json.dumps(dict(graph, meta=meta)).replace(LT_SLASH, LT_SLASH_ESCAPED)
    return (template
            .replace(VENDOR_PLACEHOLDER, vendor_js)
            .replace(DATA_PLACEHOLDER, data_json))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--from-json", metavar="PATH",
                        help="re-render from a previous zpa_raw.json (no API call)")
    parser.add_argument("--download-vendor", action="store_true",
                        help="download vis-network into vendor/ and exit")
    parser.add_argument("--cloud", default=os.environ.get("ZSCALER_CLOUD", "PRODUCTION"),
                        help="ZPA cloud name (PRODUCTION, ZPATWO) or a full "
                             "base URL (default: %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="debug logging, including the HTTP library's own "
                             "request detail")
    parser.add_argument("--creds-file", metavar="PATH",
                        help="read KEY=VALUE credentials from this file, parsed "
                             "literally (no shell expansion)")
    parser.add_argument("--out-dir", default=".",
                        help="base output directory (default: %(default)s)")
    parser.add_argument("--no-org-subfolder", action="store_true",
                        help="skip the organisation-named subfolder")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    configure_logging(args.verbose)

    if args.download_vendor:
        download_vendor()
        return 0

    if args.creds_file:
        load_credential_file(args.creds_file)

    if args.from_json:
        source = "file:" + args.from_json
        raw = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
    else:
        source = "live:" + args.cloud
        logger.info("Fetching from ZPA (%s)", args.cloud)
        raw = fetch_all(args.cloud)

    # A dump written by an earlier run carries the name, so re-rendering it
    # lands in the same folder as the run that produced it.
    org = raw.get("_org")
    out_dir = Path(args.out_dir)
    if not args.no_org_subfolder:
        out_dir = out_dir.joinpath(org_folder(org))
    out_dir.mkdir(parents=True, exist_ok=True)

    graph = build_graph(raw)
    meta = {
        "generated": _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "source": source,
        "org": org or "",
    }

    json_path = out_dir.joinpath("zpa_raw.json")
    html_path = out_dir.joinpath("zpa_graph.html")
    if not args.from_json:  # re-rendering must not rewrite the dump it read
        json_path.write_text(json.dumps(raw, indent=2, sort_keys=True),
                             encoding="utf-8")
    html_path.write_text(render_html(graph, meta, read_vendor()), encoding="utf-8")

    logger.info("")
    logger.info("  ".join(t["label"] + ": " + str(graph["counts"][t["key"]])
                          for t in TYPES))
    logger.info("Links: %d%s", len(graph["edges"]),
                "   Dangling refs: " + str(graph["missing"]) if graph["missing"] else "")
    if not args.from_json:
        logger.info("Wrote %s", json_path)
    logger.info("Wrote %s  (%s bytes, opens offline)", html_path,
                format(html_path.stat().st_size, ","))
    return 0


if __name__ == "__main__":
    sys.exit(main())
