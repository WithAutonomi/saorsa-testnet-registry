"""Client-VM roles and the systemd unit each one runs.

Roles were previously only string literals, and the unit name was derived from
one by string concatenation (``f"ant-{role}er"``) — which happens to spell
``ant-uploader``/``ant-downloader`` and nothing else. Adding the WASM roles
breaks that trick (it would spell ``ant-wasm-uploader`` only by luck and
``ant-wasm-downloadeer`` in the obvious variants), so the mapping is explicit
and lives in one place.
"""

ROLE_UPLOAD = "upload"
ROLE_DOWNLOAD = "download"
# WASM (browser-path) clients: a Node.js program driving ant-core compiled to
# WASM over WebRTC-Direct. Distinct roles rather than a flag on the native ones
# because the role names the VM, its Terraform workspace and its S3 state key.
ROLE_WASM_UPLOAD = "wasm-upload"
ROLE_WASM_DOWNLOAD = "wasm-download"
# Pointer clients: a Python service driving `ant pointer create|update|get|
# resolve` for the life of the run, so that a paid mutable reference is
# exercised on a testnet before it reaches production (V2-1339). A role of its
# own for the same reason the WASM roles are: the role names the VM, its
# Terraform workspace and its S3 state key, and a pointer client is not a
# file-transfer client with a flag set.
ROLE_POINTER = "pointer"
# Browser downloaders: N headless Chromium instances per VM, all reading ONE
# chosen "hot" file on a shared wall-clock schedule (V2-1617). A role of its own
# for the same reasons the WASM roles are -- the role names the VM, its
# Terraform workspace and its S3 state key -- and a different instrument again:
# the WASM roles measure one Node.js process drawing random files, which cannot
# say anything about many real browsers reading the same record at the same
# moment (ADR-0023, hot-record read spreading).
ROLE_BROWSER_DOWNLOAD = "browser-download"

ALL_CLIENT_ROLES = (
    ROLE_UPLOAD,
    ROLE_DOWNLOAD,
    ROLE_WASM_UPLOAD,
    ROLE_WASM_DOWNLOAD,
    ROLE_POINTER,
    ROLE_BROWSER_DOWNLOAD,
)
#: Roles that hold their own funded per-VM wallet. Every pointer write is a paid
#: single-node quote, so a pointer client mints, tops up and drains a wallet
#: exactly as an uploader does -- which is what makes it an "upload-type" role
#: even though it uploads no files.
UPLOAD_ROLES = (ROLE_UPLOAD, ROLE_WASM_UPLOAD, ROLE_POINTER)
DOWNLOAD_ROLES = (ROLE_DOWNLOAD, ROLE_WASM_DOWNLOAD, ROLE_BROWSER_DOWNLOAD)
WASM_ROLES = (ROLE_WASM_UPLOAD, ROLE_WASM_DOWNLOAD)
NATIVE_ROLES = (ROLE_UPLOAD, ROLE_DOWNLOAD, ROLE_POINTER)
POINTER_ROLES = (ROLE_POINTER,)
#: Roles whose transfers run inside a real browser (headless Chromium under
#: Playwright). Deliberately NOT part of :data:`WASM_ROLES`: both run the same
#: ant-core WASM bundle, but `is_wasm` selects the Node.js harness, its env file
#: and its unit, so folding this role in would provision a browser VM as a
#: `wasm-download` one and leave `clients start` looking for a unit nobody wrote.
BROWSER_ROLES = (ROLE_BROWSER_DOWNLOAD,)

#: ant-core refuses a browser-path file larger than this (a DECIMAL gigabyte,
#: `MAX_BROWSER_FILE_BYTES`), on upload before touching the network and on
#: download once the data map is resolved. The manifest validator uses it to
#: refuse an over-cap WASM uploader, and `clients provision` hands it to the
#: WASM downloader as `MAX_DOWNLOAD_BYTES` so it skips over-cap entries in the
#: shared address pool instead of fetching and rejecting them. Defined here,
#: not in `config.models`, because the provisioner must not import the schema.
WASM_MAX_FILE_BYTES = 1_000_000_000

#: A node admits at most this many WebRTC sessions from one source IPv4 address
#: (`max_connections_per_ip` in saorsa-transport's `src/webrtc/direct.rs`, a
#: `>=` check), and one browser client holds one session per node -- so a fifth
#: Chromium instance on a VM is refused by every node it dials. The cap is a
#: property of the network, not of the VM, which is why it is here beside the
#: file cap rather than in the manifest schema.
BROWSER_MAX_INSTANCES = 4
BROWSER_DEFAULT_INSTANCES = 4
#: Rounds start on wall-clock multiples of this, so every instance on every VM
#: in the cohort reads the same file at the same moment -- which is the whole
#: measurement.
BROWSER_DEFAULT_ROUND_PERIOD_SECS = 600
#: Below this a round is shorter than a cold browser client's own startup and
#: the cohort stops being synchronised in any useful sense.
BROWSER_MIN_ROUND_PERIOD_SECS = 120
BROWSER_DEFAULT_DOWNLOAD_TIMEOUT_SECS = 300
#: Four Chromium instances each holding ~4x a 50 MiB file in WASM memory need
#: more than 4 GB, so the browser role's default is a size up from every other
#: client role's.
BROWSER_DEFAULT_VM_SIZE = "s-4vcpu-8gb"

#: The hot-file manifest, written by `clients select-hot-files` into the
#: deployment's address prefix and read by every browser instance at every round
#: start. One object, written once by the operator: VMs choosing independently
#: from a pool that grows under them would disagree about which file was hot.
HOT_FILES_OBJECT_NAME = "browser-hot-files.json"

ROLE_UNITS = {
    ROLE_UPLOAD: "ant-uploader",
    ROLE_DOWNLOAD: "ant-downloader",
    ROLE_WASM_UPLOAD: "ant-wasm-uploader",
    ROLE_WASM_DOWNLOAD: "ant-wasm-downloader",
    ROLE_POINTER: "ant-pointer",
    ROLE_BROWSER_DOWNLOAD: "ant-browser-downloader",
}

ROLE_LABELS = {
    ROLE_UPLOAD: "Uploader",
    ROLE_DOWNLOAD: "Downloader",
    ROLE_WASM_UPLOAD: "WASM uploader",
    ROLE_WASM_DOWNLOAD: "WASM downloader",
    ROLE_POINTER: "Pointer client",
    ROLE_BROWSER_DOWNLOAD: "Browser downloader",
}

#: Ordering used when a client list is rendered or persisted, so a resumed state
#: file is indistinguishable from one written by a clean run.
ROLE_ORDER = {role: i for i, role in enumerate(ALL_CLIENT_ROLES)}


def unit_for_role(role: str) -> str:
    """systemd unit that runs the given client role."""
    try:
        return ROLE_UNITS[role]
    except KeyError:
        raise ValueError(f"unknown client role: {role!r}")


def is_wasm(role: str) -> bool:
    return role in WASM_ROLES


def is_upload(role: str) -> bool:
    """Whether this role carries a funded wallet and publishes addresses.

    True for the pointer role too: it pays for every write. Callers that mean
    "uploads files" must test the role directly -- a pointer client has no
    ``file_size_mb`` to speak of.
    """
    return role in UPLOAD_ROLES


def is_pointer(role: str) -> bool:
    return role in POINTER_ROLES


def is_browser(role: str) -> bool:
    """Whether this role drives transfers through a real headless browser."""
    return role in BROWSER_ROLES
