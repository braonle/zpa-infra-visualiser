# ZPA infrastructure visualiser

Pulls App Connectors, App Connector Groups, Server Groups, App Segments and
Segment Groups from the ZPA API and renders the dependency chain as a single,
self-contained interactive HTML page.

```
App Connector → App Connector Group → Server Group → App Segment → Segment Group
```

The page is one file with the graph library and the data inlined. It opens over
`file://` on a disconnected laptop — no CDN, no web server, no network calls.

## Setup

```sh
python3 -m venv .venv
source .venv/bin/activate
(.venv) pip install -r requirements.txt
(.venv) zpa_visualise.py --download-vendor
```

`--download-vendor` fetches `vis-network.min.js` (689 KB) into `vendor/` once.
After that the tool never needs a CDN again, so commit `vendor/` if you want a
fresh clone to be offline-ready immediately. Install the requirements first:
`pip_system_certs` points Python's default SSL context at the operating system
trust store, which covers framework Python on macOS shipping without a CA
store and keeps an inspecting proxy's root CA trusted. The script therefore
builds no SSL context of its own.

## Credentials

The script uses the **legacy ZPA API**: a client ID, client secret and customer
ID from a ZPA API role with read access to the five object types.

```sh
export ZPA_CLIENT_ID=...
export ZPA_CLIENT_SECRET=...
export ZPA_CUSTOMER_ID=...
```

Or keep them in a file and point the script at it:

```sh
.venv/bin/python zpa_visualise.py --creds-file env
```

`--creds-file` parses `KEY=VALUE` lines **literally** — the text after the
first `=` is the value, with no expansion or splitting.

**Do not `source` the file.** A shell mangles secrets containing punctuation,
and the only symptom is an opaque 401:

- double-quoted, the shell expands `$`, dropping everything it reads as a
  variable name;
- unquoted, zsh treats `[`, `?` and `*` as a glob pattern and abandons the
  assignment, leaving a stub of the value behind.

Because naming a file is an explicit instruction, `--creds-file` **overrides**
anything already exported, and warns when it replaces a differing value. That
matters: a secret mangled by an earlier `source` of the same file would
otherwise shadow the correct one for the rest of the shell session.

## Usage

```sh
# Live pull.
.venv/bin/python zpa_visualise.py --creds-file env

# Clouds: PRODUCTION (default) or ZPATWO. A full base URL also works.
.venv/bin/python zpa_visualise.py --cloud ZPATWO

# Re-render an earlier dump. No API call, works fully offline.
.venv/bin/python zpa_visualise.py --from-json Acme-Corp-EMEA/zpa_raw.json

# -v adds debug logging, including every HTTP request the library makes.
.venv/bin/python zpa_visualise.py --creds-file env -v
```

## Output layout

Results go into a folder named after the organisation, read from the tenant's
customer record, created in the current directory:

```
Acme-Corp-EMEA/
├─ zpa_graph.html
└─ zpa_raw.json
```

So several tenants can be pulled side by side without overwriting each other.
Any run of characters outside `[A-Za-z0-9._]` in the name collapses to a
single dash, and the name is confined to one path segment. If the name cannot
be read — a restricted API role may be refused the customer record — the run
warns and uses `unknown-org` rather than failing.

The name is stored in `zpa_raw.json`, so `--from-json` puts a re-render back
in the same folder. `--out-dir` changes where the organisation folder is
created, and `--no-org-subfolder` writes straight into it without one.

These files hold live tenant configuration and are gitignored by filename,
since the folder name is not known in advance.

All progress output goes to stderr via `logging`, so stdout stays clean and
the run can be piped or redirected without mixing streams.

Keeping `zpa_raw.json` per run gives you dated snapshots you can re-render or
diff later.

## Using the page

**Layout.** Five columns, left to right, in dependency order. The layout is
computed once and then frozen, so a node you drag stays where you put it.
*Re-layout* recomputes it; *Fit* zooms to what is visible.

**Filtering.** Everything is shown initially. To narrow to what you are
reviewing:

- **Per-type checkboxes** — untick anything you do not need. *All*, *None* and
  *Invert* act on every object of that type, including rows the search is
  currently hiding.
- **Search** filters the sidebar lists by name or ZPA ID. *Keep only matches*
  reduces the graph to exactly those; *Drop matches* removes them.
- **Isolate** is the quickest route for a review. Click one or more nodes on the
  canvas (shift-click to add) and press *Isolate selected*, or just double-click
  a node. With scope *Full chain* this keeps everything transitively connected —
  "show me every connector behind this app segment, and everything else those
  connectors serve". With *Direct neighbours only* it keeps just one hop.
- **Hide disabled objects** and **Hide unconnected nodes** are independent
  toggles layered on top.
- *Reset* clears all of it.

**Detail panel.** Click a node for its properties, its ZPA ID, and clickable
lists of what it depends on and what depends on it, so you can walk the chain.

## Notes

**Dangling references.** Relationships are read from both ends and unioned,
because ZPA embeds child objects inconsistently depending on which endpoint
returned the record. If an object is referenced but its own endpoint never
returned it, it is drawn as a dashed grey placeholder rather than dropped — that
is usually either a stale reference or a gap in the API role's permissions, and
either way it is worth seeing. The run prints a `Dangling refs` count.

**Errors are deliberately unhandled.** Failures from the HTTP and ZPA
libraries propagate with their original type and traceback — a rejected
sign-in surfaces as the library's own
`ValueError: 401 :Session is not authenticated or timed out`, pointing at the
line that raised it. Nothing is caught and summarised, and a failed collection
aborts the run rather than rendering a partial graph. Only the script's own
preconditions (missing credentials, unknown cloud, absent vendor file) exit
with a tidy one-line message.

**Large tenants.** Sidebar lists render at most 500 rows per type; use search to
narrow. The graph itself is not capped.

**Out of scope.** Servers inside Server Groups, access policies, and
microtenants are not fetched.

**The library, and a bug worked around.** API calls go through
[`zscaler_api_talkers`](https://github.com/sergitopereira/zscaler_api_talkers),
a community library rather than the vendor SDK. In 6.4.1 its pagination helper
`_obtain_all_results(self, url, url_params)` declares `url_params` with no
default, while all 17 call sites pass only `url` — so every list-all method
raises `TypeError` out of the box. `_talker_class()` subclasses `ZpaTalker` and
supplies the default, which fixes all five methods without patching the
installed package. If upstream fixes this, the subclass stays harmless.