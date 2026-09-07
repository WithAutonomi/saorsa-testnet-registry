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

ALL_CLIENT_ROLES = (ROLE_UPLOAD, ROLE_DOWNLOAD, ROLE_WASM_UPLOAD, ROLE_WASM_DOWNLOAD)
UPLOAD_ROLES = (ROLE_UPLOAD, ROLE_WASM_UPLOAD)
DOWNLOAD_ROLES = (ROLE_DOWNLOAD, ROLE_WASM_DOWNLOAD)
WASM_ROLES = (ROLE_WASM_UPLOAD, ROLE_WASM_DOWNLOAD)
NATIVE_ROLES = (ROLE_UPLOAD, ROLE_DOWNLOAD)

ROLE_UNITS = {
    ROLE_UPLOAD: "ant-uploader",
    ROLE_DOWNLOAD: "ant-downloader",
    ROLE_WASM_UPLOAD: "ant-wasm-uploader",
    ROLE_WASM_DOWNLOAD: "ant-wasm-downloader",
}

ROLE_LABELS = {
    ROLE_UPLOAD: "Uploader",
    ROLE_DOWNLOAD: "Downloader",
    ROLE_WASM_UPLOAD: "WASM uploader",
    ROLE_WASM_DOWNLOAD: "WASM downloader",
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
    return role in UPLOAD_ROLES
