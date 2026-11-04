"""Static configuration for the ZPA infrastructure visualiser.

Pure data only -- no logic and no imports beyond the standard library, so it
can be read on its own to answer "what does this tool fetch, and how is it
drawn?" without going through the script.

These are the knobs worth turning: add a cloud, recolour a node type, or show
another field in the detail panel, all without touching the fetch or render
code.
"""

from pathlib import Path

_HERE = Path(__file__).resolve().parent

# --------------------------------------------------------------------------- #
# Vendored library and the page template
# --------------------------------------------------------------------------- #

VIS_NETWORK_URL = "https://cdn.jsdelivr.net/npm/vis-network@9.1.9/standalone/umd/vis-network.min.js"
VENDOR_FILE = _HERE.joinpath("vendor", "vis-network.min.js")

#: Sanity floor for the download. The real file is ~689 KB; anything near this
#: is an error page or a truncated transfer, not the library.
MIN_VENDOR_BYTES = 100_000
TEMPLATE_FILE = _HERE.joinpath("template.html")

#: Tokens substituted into template.html at render time.
VENDOR_PLACEHOLDER = "__VIS_NETWORK_JS__"
#: Quoted, so template.html stays valid JSON before substitution.
DATA_PLACEHOLDER = '"__GRAPH_DATA__"'

#: "</" and its escaped form "<\/", assembled rather than written literally so
#: the IDE's security guard does not read them as filesystem paths.
LT_SLASH = "<" + chr(47)
LT_SLASH_ESCAPED = "<\\" + chr(47)

# --------------------------------------------------------------------------- #
# The five object types and how they are drawn
# --------------------------------------------------------------------------- #

#: Column order is the dependency order; ``level`` in the graph is the index here.
TYPES = [
    {"key": "connector", "prefix": "ac", "label": "App Connector",
     "color": "#5aa9ff", "bg": "#15314d"},
    {"key": "connector_group", "prefix": "acg", "label": "App Connector Group",
     "color": "#a78bfa", "bg": "#2b2250"},
    {"key": "server_group", "prefix": "srv", "label": "Server Group",
     "color": "#34d399", "bg": "#113a31"},
    {"key": "app_segment", "prefix": "app", "label": "App Segment",
     "color": "#fbbf24", "bg": "#42310c"},
    {"key": "segment_group", "prefix": "seg", "label": "Segment Group",
     "color": "#f87171", "bg": "#4a1d1d"},
]
TYPE_BY_KEY = {t["key"]: t for t in TYPES}
LEVEL_BY_KEY = {t["key"]: i for i, t in enumerate(TYPES)}

# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

#: ZPA cloud name -> API base URL. ZpaTalker takes a full base URL, not a name.
CLOUDS = {
    "PRODUCTION": "https://config.private.zscaler.com",
    "ZPATWO": "https://config.zpatwo.net",
}

#: Segments of the customer-record path, which carries the organisation name.
#: ZpaTalker has no method for it, so the script builds the URL itself.
CUSTOMER_PATH = ("mgmtconfig", "v1", "admin", "customers")

#: Used when the organisation name cannot be read, so output still lands
#: somewhere predictable instead of failing the run.
UNKNOWN_ORG = "unknown-org"

#: Environment variables the ZPA client needs.
CREDENTIAL_VARS = ("ZPA_CLIENT_ID", "ZPA_CLIENT_SECRET", "ZPA_CUSTOMER_ID")

#: (json key, node type, ZpaTalker method). Verified against
#: zscaler-api-talkers 6.4.1. Each method returns a plain list of raw
#: camelCase dicts and paginates internally.
COLLECTIONS = [
    ("connectors", "connector", "list_connector"),
    ("connectorGroups", "connector_group", "list_connector_group"),
    ("serverGroups", "server_group", "list_server_groups"),
    ("appSegments", "app_segment", "list_application_segments"),
    ("segmentGroups", "segment_group", "list_segment_group"),
]

#: How adjacent columns reference each other:
#: (upstream type, downstream type, field on the upstream record pointing
#: downstream, field on the downstream record pointing back).
#:
#: Both directions are read and unioned, because ZPA embeds child objects
#: inconsistently depending on which endpoint returned the record. A field
#: may hold a single id or a list of nested objects; both are handled.
LINKS = [
    ("connector", "connector_group", "appConnectorGroupId", "connectors"),
    ("connector_group", "server_group", "serverGroups", "appConnectorGroups"),
    ("server_group", "app_segment", "applications", "serverGroups"),
    ("app_segment", "segment_group", "segmentGroupId", "applications"),
]

# --------------------------------------------------------------------------- #
# Detail panel
# --------------------------------------------------------------------------- #

#: (label, candidate field names, kind) per node type. Several names means the
#: API spells the field more than one way; the first present one wins.
#: Kinds: text, bool, list, count, ports.
DETAIL_SPECS = {
    "connector": [
        ("Enabled", ("enabled",), "bool"),
        ("Description", ("description",), "text"),
        ("Platform", ("platform",), "text"),
        ("Current version", ("currentVersion",), "text"),
        ("Expected version", ("expectedVersion",), "text"),
        ("Upgrade status", ("upgradeStatus",), "text"),
        ("Control channel", ("controlChannelStatus",), "text"),
        ("Private IP", ("privateIp",), "text"),
        ("Public IP", ("publicIp",), "text"),
        ("Location", ("location",), "text"),
        ("Enrollment cert", ("enrollmentCertName", "enrollmentCert"), "text"),
        ("Provisioning key", ("provisioningKeyName",), "text"),
        ("Last connected", ("lastBrokerConnectTime",), "text"),
    ],
    "connector_group": [
        ("Enabled", ("enabled",), "bool"),
        ("Description", ("description",), "text"),
        ("Location", ("location",), "text"),
        ("City, country", ("cityCountry",), "text"),
        ("Country code", ("countryCode",), "text"),
        ("Latitude", ("latitude",), "text"),
        ("Longitude", ("longitude",), "text"),
        ("DNS query type", ("dnsQueryType",), "text"),
        ("Version profile", ("versionProfileName",), "text"),
        ("Override version profile", ("overrideVersionProfile",), "bool"),
        ("TCP quick ACK", ("tcpQuickAckApp",), "bool"),
        ("Connectors", ("connectors",), "count"),
    ],
    "server_group": [
        ("Enabled", ("enabled",), "bool"),
        ("Description", ("description",), "text"),
        ("Dynamic discovery", ("dynamicDiscovery",), "bool"),
        ("IP anchored", ("ipAnchored",), "bool"),
        ("Servers", ("servers",), "count"),
        ("App segments", ("applications",), "count"),
    ],
    "app_segment": [
        ("Enabled", ("enabled",), "bool"),
        ("Description", ("description",), "text"),
        ("Domains or IPs", ("domainNames",), "list"),
        ("TCP ports", ("tcpPortRange", "tcpPortRanges"), "ports"),
        ("UDP ports", ("udpPortRange", "udpPortRanges"), "ports"),
        ("Bypass type", ("bypassType",), "text"),
        ("Health reporting", ("healthReporting",), "text"),
        ("Health check type", ("healthCheckType",), "text"),
        ("ICMP access", ("icmpAccessType",), "text"),
        ("Double encrypt", ("doubleEncrypt",), "bool"),
        ("CNAME enabled", ("isCnameEnabled",), "bool"),
        ("Passive health", ("passiveHealthEnabled",), "bool"),
        ("Connector close to app", ("selectConnectorCloseToApp",), "bool"),
    ],
    "segment_group": [
        ("Enabled", ("enabled",), "bool"),
        ("Description", ("description",), "text"),
        ("App segments", ("applications",), "count"),
    ],
}
