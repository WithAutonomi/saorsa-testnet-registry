"""Ensure operator SSH public keys are present on every provisioned VM.

The keys live in ``resources/operator_authorized_keys.pub`` (one per line; blank
lines and ``#`` comments are ignored). They are appended idempotently to root's
``authorized_keys`` during every tier's provisioning pass — bootstrap, genesis,
node, and client — so operators retain SSH access regardless of which cloud key
the VM was originally created with.

The op is a single best-effort ``server.shell`` command: it creates ``~/.ssh``
if missing and appends each key only when absent (``grep -qxF`` guard), so it is
safe to re-run and never duplicates entries.
"""

from pyinfra.api.operation import add_op
from pyinfra.operations import server

from saorsa_deploy.resources import get_resources_dir

OP_NAME = "Ensure operator SSH keys"
_KEYS_FILE = "operator_authorized_keys.pub"


def load_operator_authorized_keys() -> list[str]:
    """Return the configured operator public keys (comments/blanks stripped)."""
    path = get_resources_dir() / _KEYS_FILE
    if not path.exists():
        return []
    keys = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        keys.append(line)
    return keys


def build_authorized_keys_command(keys: list[str]) -> str:
    """Build an idempotent shell command that ensures ``keys`` are authorized for root."""
    parts = [
        "install -d -m 700 /root/.ssh",
        "touch /root/.ssh/authorized_keys",
        "chmod 600 /root/.ssh/authorized_keys",
    ]
    for key in keys:
        # Operator keys never contain single quotes; single-quote to avoid any
        # shell interpretation of the '+'/'/' characters in the base64 blob.
        quoted = "'" + key + "'"
        parts.append(
            f"grep -qxF {quoted} /root/.ssh/authorized_keys "
            f"|| echo {quoted} >> /root/.ssh/authorized_keys"
        )
    return " && ".join(parts)


def add_operator_authorized_keys_op(state, keys: list[str] | None = None) -> None:
    """Register the operator-key op on ``state`` for all active hosts.

    No-op when no keys are configured, so the provisioning pass is unaffected if
    the key file is empty or removed.
    """
    if keys is None:
        keys = load_operator_authorized_keys()
    if not keys:
        return
    add_op(
        state,
        server.shell,
        name=OP_NAME,
        commands=[build_authorized_keys_command(keys)],
    )
