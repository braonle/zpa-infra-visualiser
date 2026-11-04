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
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from constants import (
    CLOUDS,
    COLLECTIONS,
    CUSTOMER_PATH,
    DATA_PLACEHOLDER,
    DETAIL_SPECS,
    LEVEL_BY_KEY,
    LT_SLASH,
    LT_SLASH_ESCAPED,
    TEMPLATE_FILE,
    TYPE_BY_KEY,
    TYPES,
    UNKNOWN_ORG,
    VENDOR_FILE,
    VENDOR_PLACEHOLDER,
    VIS_NETWORK_URL,
)

logger = logging.getLogger("zpa_visualise")


class ZpaError(RuntimeError):
    """Anything that should stop the run with a readable message."""


class _CliFormatter(logging.Formatter):
    """Bare lines at INFO, level-prefixed above it.

    Routine progress output is what a CLI user expects to read unadorned,
    while a warning or error should announce itself.
    """

    def format(self, record):
        message = record.getMessage()
        if record.levelno <= logging.INFO:
            return message
        return record.levelname.lower() + ": " + message


def configure_logging(verbose=False):
    """Send this module's output to stderr, leaving stdout clean.

    Only our own logger is configured by default, so the HTTP library's
    handlers are left alone. ``--verbose`` additionally opens up the root
    logger, which surfaces the request-level detail the library emits -- the
    useful view when a sign-in is being rejected.
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
    ``tcpPortRange`` and ``tcpPortRanges``), and a field present but explicitly
    null should fall back to the default rather than yield None.
    """
    for name in names:
        value = obj.get(name)
        if value is not None:
            return value
    return default


def child_ids(obj, key):
    """IDs of embedded child objects, tolerating a malformed value.

    ZPA normally nests a list of objects here, but a lone object, a null, or
    an entry without an id should cost us that one edge, not the whole run.
    """
    value = obj.get(key)
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(child["id"]) for child in value
            if isinstance(child, dict) and child.get("id") is not None]


# --------------------------------------------------------------------------- #
# Fetching
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
    the same file would shadow the correct one, which is exactly the failure
    this function exists to avoid.
    """
    path = Path(path)
    if not path.is_file():
        raise ZpaError("No such credential file: " + str(path))

    loaded, replaced = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.lower().startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if not key:
            continue
        previous = os.environ.get(key)
        if previous is not None and previous != value:
            replaced.append(key)
        os.environ[key] = value
        loaded.append(key)

    if loaded:
        logger.info("Loaded %s from %s", ", ".join(loaded), path)
    if replaced:
        logger.warning("%s differed from the exported value and was replaced "
                       "by the file", ", ".join(replaced))
    return loaded


def _talker_class():
    """Return a ZpaTalker subclass with its pagination helper repaired.

    zscaler-api-talkers 6.4.1 declares
    ``_obtain_all_results(self, url, url_params)`` with no default for
    ``url_params``, while all 17 call sites pass only ``url`` -- so every
    list-all method raises TypeError out of the box. Supplying the default in
    a subclass fixes all of them without patching the installed package. The
    parent already treats ``None`` as "start at page 1, pagesize 500".
    """
    try:
        from zscaler_api_talkers import ZpaTalker
    except ImportError as exc:
        raise ZpaError(
            "zscaler-api-talkers is not available in this interpreter. "
            "Install the pinned requirements (see README) and retry."
        ) from exc

    class PatchedZpaTalker(ZpaTalker):
        def _obtain_all_results(self, url, url_params=None):
            return super()._obtain_all_results(url, url_params)

    return PatchedZpaTalker


def resolve_cloud(cloud):
    """Accept either a cloud name or a full base URL."""
    if cloud.lower().startswith("http"):
        return cloud.rstrip("" + chr(47))
    try:
        return CLOUDS[cloud.upper()]
    except KeyError:
        raise ZpaError(
            "Unknown cloud " + repr(cloud) + ". Use one of: "
            + ", ".join(sorted(CLOUDS)) + ", or pass a full base URL."
        ) from None


def fetch_org_name(talker, customer_id):
    """The tenant's organisation name, from its customer record.

    None of the five collection endpoints carries it, and ZpaTalker has no
    method for the customer record, so this builds the one URL directly. A
    read-only API role may be denied it, which is not worth failing the run
    over -- the caller falls back to a placeholder.
    """
    url = chr(47) + chr(47).join(CUSTOMER_PATH + (str(customer_id),))
    try:
        response = talker.hp_http.get_call(url, headers=talker.header,
                                           error_handling=False)
        if response.status_code != 200:
            logger.warning("Could not read the organisation name (HTTP %s)",
                           response.status_code)
            return None
        name = response.json().get("name")
    except Exception as exc:  # noqa: BLE001 - never fatal
        logger.warning("Could not read the organisation name: %s", _redact(str(exc)))
        return None
    if not name:
        logger.warning("The customer record carries no organisation name.")
        return None
    return str(name)


def org_folder(name):
    """Turn an organisation name into one safe path segment.

    Any run of characters outside ``[A-Za-z0-9._]`` collapses to a single
    dash, so "Acme Corp - EMEA" becomes "Acme-Corp-EMEA" rather than keeping
    the original dash and the spaces either side of it. Case is kept: the
    folder is meant to be recognisable as the organisation.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._]+", "-", (name or "").strip()).strip("-._")
    return cleaned or UNKNOWN_ORG


def fetch_all(cloud):
    """Pull all five collections. One failing endpoint degrades that layer only."""
    talker_cls = _talker_class()
    base_url = resolve_cloud(cloud)

    missing = [v for v in ("ZPA_CLIENT_ID", "ZPA_CLIENT_SECRET", "ZPA_CUSTOMER_ID")
               if not os.environ.get(v)]
    if missing:
        raise ZpaError(
            "Missing credentials: " + ", ".join(missing) + "\n"
            "Export them into the environment, or pass --creds-file (see README)."
        )

    try:
        talker = talker_cls(
            # ZpaTalker annotates customer_id as int, but only ever
            # interpolates it into a URL, so the string form is what we want.
            customer_id=os.environ["ZPA_CUSTOMER_ID"],
            cloud=base_url,
            client_id=os.environ["ZPA_CLIENT_ID"],
            client_secret=os.environ["ZPA_CLIENT_SECRET"],
        )
    except Exception as exc:  # noqa: BLE001 - becomes a readable message
        raise ZpaError(
            "Sign-in to " + base_url + " failed: " + _redact(str(exc)) + "\n"
            "A 401 here means ZPA rejected the key itself. Check that it has "
            "not expired, that the client ID and secret come from the same "
            "key, and that --cloud matches the tenant."
        ) from exc

    if not getattr(talker, "header", None):
        raise ZpaError("Sign-in to " + base_url + " returned no bearer token.")

    org = fetch_org_name(talker, os.environ["ZPA_CUSTOMER_ID"])
    if org:
        logger.info("Organisation: %s", org)

    raw = {}
    failures = {}
    for key, method_name in COLLECTIONS:
        try:
            result = getattr(talker, method_name)()
            if isinstance(result, dict):
                result = [result]
            # ZpaTalker passes response.json() straight through, so entries
            # are already dicts; drop anything malformed rather than abort.
            raw[key] = [i for i in (result or []) if isinstance(i, dict)]
            logger.info("  %-16s %5d", key, len(raw[key]))
        except Exception as exc:  # noqa: BLE001 - keep going, report at the end
            raw[key] = []
            failures[key] = _redact(str(exc))
            logger.warning("  %-16s failed: %s", key, failures[key])

    if len(failures) == len(COLLECTIONS):
        raise ZpaError(
            "Every collection failed to fetch. Check credentials, the --cloud "
            "value, and that the API role has read access.\n  "
            + "\n  ".join(f"{k}: {v}" for k, v in failures.items())
        )
    raw["_failures"] = failures
    # Carried in the dump so --from-json lands in the same folder later.
    raw["_org"] = org
    return raw


def _redact(text):
    secret = os.environ.get("ZPA_CLIENT_SECRET")
    if secret and secret in text:
        text = text.replace(secret, "***REDACTED***")
    return text


# --------------------------------------------------------------------------- #
# Graph construction
# --------------------------------------------------------------------------- #

def _fmt(value, kind="text"):
    """Render a field value for the detail panel, or None to omit the row."""
    if value is None or value == "" or value == [] or value == {}:
        return None
    if kind == "bool":
        if isinstance(value, str):
            truthy = value.strip().lower() in ("true", "1", "yes")
        else:
            truthy = bool(value)
        return "Yes" if truthy else "No"
    if kind == "count":
        return str(len(value)) if isinstance(value, (list, dict)) else str(value)
    if kind == "list":
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return str(value)
    if kind == "ports":
        return _fmt_ports(value)
    return str(value)


def _fmt_ports(value):
    """ZPA gives port ranges either as [{from,to}] or as a flat [from, to, ...]."""
    if not isinstance(value, list) or not value:
        return None
    parts = []
    if all(isinstance(v, dict) for v in value):
        for entry in value:
            lo, hi = entry.get("from"), entry.get("to")
            parts.append(str(lo) if str(lo) == str(hi) else f"{lo}-{hi}")
    else:
        flat = [str(v) for v in value]
        for i in range(0, len(flat) - 1, 2):
            lo, hi = flat[i], flat[i + 1]
            parts.append(lo if lo == hi else f"{lo}-{hi}")
        if len(flat) % 2:
            parts.append(flat[-1])
    return ", ".join(parts) if parts else None


def _details(type_key, rec):
    rows = []
    for label, names, kind in DETAIL_SPECS[type_key]:
        text = _fmt(pick(rec, *names), kind)
        if text is not None:
            rows.append([label, text])
    return rows


class GraphBuilder:
    def __init__(self):
        self.nodes = {}
        self.edges = set()

    def _nid(self, type_key, obj_id):
        return f"{TYPE_BY_KEY[type_key]['prefix']}:{obj_id}"

    def placeholder(self, type_key, obj_id):
        """Reference an object we have not (yet) seen a real record for."""
        nid = self._nid(type_key, obj_id)
        if nid not in self.nodes:
            self.nodes[nid] = {
                "id": nid,
                "type": type_key,
                "level": LEVEL_BY_KEY[type_key],
                "rawId": str(obj_id),
                "name": f"(not returned) {obj_id}",
                "enabled": True,
                "missing": True,
                "detail": [],
            }
        return nid

    def real(self, type_key, rec):
        """Record an object we actually fetched, upgrading any placeholder."""
        obj_id = rec.get("id")
        if obj_id is None:
            return None
        nid = self.placeholder(type_key, obj_id)
        node = self.nodes[nid]
        node["name"] = str(pick(rec, "name", default=f"(unnamed) {obj_id}"))
        node["enabled"] = _truthy(pick(rec, "enabled", default=True))
        node["missing"] = False
        node["detail"] = _details(type_key, rec)
        return nid

    def link(self, src_type, src_id, dst_type, dst_id):
        if src_id in (None, "") or dst_id in (None, ""):
            return
        self.edges.add((
            self.placeholder(src_type, src_id),
            self.placeholder(dst_type, dst_id),
        ))


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def build_graph(raw):
    """Turn the five raw collections into nodes + edges.

    Edges are read from both ends of every relationship and unioned, because
    ZPA embeds child objects inconsistently depending on which endpoint
    returned the record.
    """
    g = GraphBuilder()

    connectors = raw.get("connectors", [])
    conn_groups = raw.get("connectorGroups", [])
    server_groups = raw.get("serverGroups", [])
    app_segments = raw.get("appSegments", [])
    seg_groups = raw.get("segmentGroups", [])

    for rec in connectors:
        g.real("connector", rec)
    for rec in conn_groups:
        g.real("connector_group", rec)
    for rec in server_groups:
        g.real("server_group", rec)
    for rec in app_segments:
        g.real("app_segment", rec)
    for rec in seg_groups:
        g.real("segment_group", rec)

    # Connector -> Connector Group
    for rec in connectors:
        g.link("connector", rec.get("id"),
               "connector_group", rec.get("appConnectorGroupId"))
    for grp in conn_groups:
        for cid in child_ids(grp, "connectors"):
            g.link("connector", cid, "connector_group", grp.get("id"))

    # Connector Group -> Server Group
    for sgr in server_groups:
        for gid in child_ids(sgr, "appConnectorGroups"):
            g.link("connector_group", gid, "server_group", sgr.get("id"))
    for grp in conn_groups:
        for sid in child_ids(grp, "serverGroups"):
            g.link("connector_group", grp.get("id"), "server_group", sid)

    # Server Group -> App Segment
    for app in app_segments:
        for sid in child_ids(app, "serverGroups"):
            g.link("server_group", sid, "app_segment", app.get("id"))
    for sgr in server_groups:
        for aid in child_ids(sgr, "applications"):
            g.link("server_group", sgr.get("id"), "app_segment", aid)

    # App Segment -> Segment Group
    for app in app_segments:
        g.link("app_segment", app.get("id"),
               "segment_group", app.get("segmentGroupId"))
    for grp in seg_groups:
        for aid in child_ids(grp, "applications"):
            g.link("app_segment", aid, "segment_group", grp.get("id"))

    nodes = sorted(g.nodes.values(), key=lambda n: (n["level"], n["name"].lower()))
    edges = [{"from": a, "to": b} for a, b in sorted(g.edges)]

    counts = {t["key"]: 0 for t in TYPES}
    for node in nodes:
        counts[node["type"]] += 1

    return {
        "types": TYPES,
        "nodes": nodes,
        "edges": edges,
        "counts": counts,
        "missing": sum(1 for n in nodes if n["missing"]),
    }


# --------------------------------------------------------------------------- #
# Vendored library
# --------------------------------------------------------------------------- #

def _urlopen_verified(url, timeout):
    """Fetch over TLS, falling back to certifi's root certificates.

    Framework Python builds on macOS ship without a CA store until their
    certificate setup step has been completed, so the default context can
    fail where curl succeeds.
    """
    import ssl

    try:
        return urllib.request.urlopen(url, timeout=timeout)
    except urllib.error.URLError as exc:
        if not isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise
        try:
            import certifi
        except ImportError:
            raise ZpaError(
                "TLS certificate verification failed: this Python has no CA "
                "store.\nEither complete the certificate setup step shipped "
                "with your Python, or fetch the file yourself and place it "
                "at:\n  " + str(VENDOR_FILE) + "\nSource: " + VIS_NETWORK_URL
            ) from exc
        context = ssl.create_default_context(cafile=certifi.where())
        return urllib.request.urlopen(url, timeout=timeout, context=context)


def download_vendor(dest=VENDOR_FILE):
    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading %s", VIS_NETWORK_URL)
    with _urlopen_verified(VIS_NETWORK_URL, timeout=60) as response:
        payload = response.read()
    if len(payload) < 100_000:
        raise ZpaError(
            f"Received only {len(payload)} bytes, which is too small to be the "
            "vis-network library. Check outbound network access and retry."
        )
    dest.write_bytes(payload)
    logger.info("Wrote %s (%s bytes)", dest, format(len(payload), ","))
    return dest


def read_vendor(path=VENDOR_FILE):
    if not path.is_file():
        raise ZpaError(
            f"Missing {path}.\n"
            "Re-run once with --download-vendor (needs internet) to fetch it."
        )
    js = path.read_text(encoding="utf-8")
    # A literal close-script tag anywhere in the library source would
    # terminate the inline <script> block early.
    return js.replace(LT_SLASH + "script", LT_SLASH_ESCAPED + "script")


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def render_html(graph, meta, vendor_js, template_path=None):
    template_path = Path(template_path or TEMPLATE_FILE)
    template = template_path.read_text(encoding="utf-8")

    payload = dict(graph)
    payload["meta"] = meta
    # Every '<' in JSON is inside a string, so rewriting the close-tag opener
    # to its valid JSON escape can neither break the document nor the parse.
    data_json = json.dumps(payload, indent=None, sort_keys=False) \
        .replace(LT_SLASH, LT_SLASH_ESCAPED)

    return (template
            .replace(VENDOR_PLACEHOLDER, vendor_js)
            .replace(DATA_PLACEHOLDER, data_json))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv=None):
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
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    if args.download_vendor:
        download_vendor()
        return 0

    if args.creds_file:
        load_credential_file(args.creds_file)

    if args.from_json:
        source_label = f"file:{args.from_json}"
        raw = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
    else:
        source_label = f"live:{args.cloud}"
        logger.info("Fetching from ZPA (%s)", args.cloud)
        raw = fetch_all(args.cloud)

    failures = raw.get("_failures") or {}
    # A dump written by an earlier run carries the name, so re-rendering it
    # lands in the same folder as the run that produced it.
    org = raw.get("_org")
    out_dir = Path(args.out_dir)
    if not args.no_org_subfolder:
        out_dir = out_dir.joinpath(org_folder(org))
    out_dir.mkdir(parents=True, exist_ok=True)

    graph = build_graph(raw)

    json_path = out_dir.joinpath("zpa_raw.json")
    # Re-rendering from a dump should not rewrite the dump it read.
    if not args.from_json:
        json_path.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")

    meta = {
        "generated": _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "source": source_label,
        "org": org or "",
        "failures": failures,
    }
    html = render_html(graph, meta, read_vendor())
    html_path = out_dir.joinpath("zpa_graph.html")
    html_path.write_text(html, encoding="utf-8")

    summary = "  ".join(
        f"{t['label']}: {graph['counts'][t['key']]}" for t in TYPES
    )
    logger.info("")
    logger.info("%s", summary)
    logger.info("Links: %d%s", len(graph["edges"]),
                "   Dangling refs: " + str(graph["missing"]) if graph["missing"] else "")
    if not args.from_json:
        logger.info("Wrote %s", json_path)
    logger.info("Wrote %s  (%s bytes, opens offline)", html_path,
                format(html_path.stat().st_size, ","))
    if failures:
        logger.warning("Some collections failed and are missing from the graph:")
        for key, message in failures.items():
            logger.warning("  %s: %s", key, message)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ZpaError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Interrupted.")
        sys.exit(130)
