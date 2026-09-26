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

ALL_CLIENT_ROLES = (
    ROLE_UPLOAD,
    ROLE_DOWNLOAD,
    ROLE_WASM_UPLOAD,
    ROLE_WASM_DOWNLOAD,
    ROLE_POINTER,
)
#: Roles that hold their own funded per-VM wallet. Every pointer write is a paid
#: single-node quote, so a pointer client mints, tops up and drains a wallet
#: exactly as an uploader does -- which is what makes it an "upload-type" role
#: even though it uploads no files.
UPLOAD_ROLES = (ROLE_UPLOAD, ROLE_WASM_UPLOAD, ROLE_POINTER)
DOWNLOAD_ROLES = (ROLE_DOWNLOAD, ROLE_WASM_DOWNLOAD)
WASM_ROLES = (ROLE_WASM_UPLOAD, ROLE_WASM_DOWNLOAD)
NATIVE_ROLES = (ROLE_UPLOAD, ROLE_DOWNLOAD, ROLE_POINTER)
POINTER_ROLES = (ROLE_POINTER,)

#: ant-core refuses a browser-path file larger than this (a DECIMAL gigabyte,
#: `MAX_BROWSER_FILE_BYTES`), on upload before touching the network and on
#: download once the data map is resolved. The manifest validator uses it to
#: refuse an over-cap WASM uploader, and `clients provision` hands it to the
#: WASM downloader as `MAX_DOWNLOAD_BYTES` so it skips over-cap entries in the
#: shared address pool instead of fetching and rejecting them. Defined here,
#: not in `config.models`, because the provisioner must not import the schema.
WASM_MAX_FILE_BYTES = 1_000_000_000

ROLE_UNITS = {
    ROLE_UPLOAD: "ant-uploader",
    ROLE_DOWNLOAD: "ant-downloader",
    ROLE_WASM_UPLOAD: "ant-wasm-uploader",
    ROLE_WASM_DOWNLOAD: "ant-wasm-downloader",
    ROLE_POINTER: "ant-pointer",
}

ROLE_LABELS = {
    ROLE_UPLOAD: "Uploader",
    ROLE_DOWNLOAD: "Downloader",
    ROLE_WASM_UPLOAD: "WASM uploader",
    ROLE_WASM_DOWNLOAD: "WASM downloader",
    ROLE_POINTER: "Pointer client",
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
