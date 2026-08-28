"""In-place ant-node binary swap for a running node VM.

Replaces ``/usr/local/bin/ant-node`` with a different build and restarts every
``ant-node-<i>`` service on the host, without touching the unit files, the data
directories or any other provisioning. The services come back on the new binary
with the same ``--root-dir``, so from the node's point of view this is
indistinguishable from an automatic upgrade (``--stop-on-upgrade`` exit +
``Restart=on-success``), only deterministic and at a moment we choose.

That is what a storage-format or migration test needs: run the fleet on a
released version long enough to accumulate data, then move it to the branch
under test at a chosen time. The auto-upgrade route cannot do that without
compiling shortened timers into the *starting* binary and publishing fake
releases; this cannot be defeated by someone else publishing a release
mid-run either.

Optionally a node config file (TOML, the shape ``ant-node --config`` reads) is
installed at the same time. ``--config`` is a CLI flag with no environment
alias, so the only way to hand it to a service written by ``nodes provision``
is to re-declare ``ExecStart``: a systemd drop-in per service resets
``ExecStart=`` and re-states the line from the main unit file with
``--config <path>`` appended. The main unit is never edited, so re-running
with a different file simply regenerates the drop-ins, and a NAT unit's
``ip netns exec`` prefix survives because the whole original line is reused.
Applied at swap time rather than provision time on purpose: the *old* binary
would honour some of the same keys (``disk_reserve_mb``, for one) and a test
usually wants them to bite only on the new build.

The shell is deliberately defensive:

* The new binary is downloaded to a temp path **beside** the target (same
  filesystem), so the final ``mv`` is an atomic ``rename``. A download into
  ``/tmp`` would cross filesystems and copy, leaving a window where the path
  holds a half-written file that a restarting service could exec.
* ``--version`` is run on the downloaded file before anything is replaced. A
  truncated download or a binary built for the wrong libc fails there, with
  the old binary still in place and the services untouched.
* The running services keep the old inode until they restart, so the rename
  itself never disturbs them; only the explicit ``systemctl restart`` does.
"""

from __future__ import annotations

import base64

from pyinfra.api.operation import add_op
from pyinfra.operations import server

from saorsa_deploy.binary_source import RELEASE_ASSET_NAME
from saorsa_deploy.provisioning.bootstrap import BINARY_INSTALL_PATH

# Markers grepped from the op's stdout by the caller.
MARKER_DONE = "ANT_SWAP:DONE"
MARKER_BAD_BINARY = "ANT_SWAP:BAD_BINARY"
MARKER_NOT_ACTIVE = "ANT_SWAP:NOT_ACTIVE"
MARKER_NO_EXECSTART = "ANT_SWAP:NO_EXECSTART"

# Where the node config lands on the host, and the drop-in that points the
# services at it. One file per host: every service on a VM gets the same config.
NODE_CONFIG_PATH = "/etc/ant-node/config.toml"
NODE_CONFIG_DROPIN = "10-node-config.conf"


def service_names(node_count: int) -> list[str]:
    """The ``ant-node-<i>`` unit names ``nodes provision`` writes on each VM."""
    if not isinstance(node_count, int) or isinstance(node_count, bool) or node_count < 1:
        raise ValueError(f"node_count must be a positive int, got {node_count!r}")
    return [f"ant-node-{i + 1}" for i in range(node_count)]


def _node_config_block(node_config: str, services: list[str]) -> str:
    """Shell that installs the config file and one ExecStart drop-in per service.

    The original ``ExecStart=`` is read from the *main* unit file, not from
    ``systemctl show``, whose rendering of it is not a command line. The main
    unit never carries ``--config`` (only the drop-in does), so the result is
    idempotent and always ``<original> --config <path>``.
    """
    encoded = base64.b64encode(node_config.encode()).decode()
    per_service = "\n".join(
        f"""\
UNIT="/etc/systemd/system/{svc}.service"
EXEC=$(grep -m1 '^ExecStart=' "$UNIT" | cut -d= -f2- || true)
if [ -z "$EXEC" ]; then
    echo "{MARKER_NO_EXECSTART} {svc}"
    exit 1
fi
mkdir -p "$UNIT.d"
printf '[Service]\\nExecStart=\\nExecStart=%s --config {NODE_CONFIG_PATH}\\n' "$EXEC" \\
    > "$UNIT.d/{NODE_CONFIG_DROPIN}\""""
        for svc in services
    )
    return f"""\
mkdir -p "$(dirname {NODE_CONFIG_PATH})"
echo {encoded} | base64 -d > "{NODE_CONFIG_PATH}"
{per_service}
systemctl daemon-reload
"""


def build_swap_binary_command(
    download_url: str,
    is_archive: bool,
    node_count: int,
    restart: bool = True,
    install_path: str = BINARY_INSTALL_PATH,
    node_config: str | None = None,
) -> str:
    """Return the shell that swaps the binary and restarts the node services.

    ``is_archive`` selects the GitHub-release shape (a ``.tar.gz`` holding
    ``ant-node``) over the custom-build shape (a bare executable), matching
    ``binary_source``. With ``restart=False`` the binary is replaced but the
    services are left running the old one until something restarts them.
    ``node_config`` is the TOML text to install and point every service at;
    ``None`` leaves the units exactly as they are.
    """
    if not download_url:
        raise ValueError("download_url is required")
    services = service_names(node_count)
    services_str = " ".join(services)
    staging = f"{install_path}.swap.$$"
    asset = f"/tmp/{RELEASE_ASSET_NAME}.swap.$$"
    extract_dir = "/tmp/ant-node-swap.$$"

    if is_archive:
        fetch = (
            f'wget -q "{download_url}" -O "{asset}" && '
            f'mkdir -p "{extract_dir}" && '
            f'tar -xzf "{asset}" -C "{extract_dir}" && '
            f'mv "{extract_dir}/ant-node" "$STAGING" && '
            f'rm -rf "{asset}" "{extract_dir}"'
        )
    else:
        fetch = f'wget -q "{download_url}" -O "$STAGING"'

    config_block = _node_config_block(node_config, services) if node_config is not None else ""

    if restart:
        restart_block = f"""\
systemctl restart {services_str}
if ! systemctl is-active --quiet {services_str}; then
    echo "{MARKER_NOT_ACTIVE} old=$OLD_VER new=$NEW_VER"
    exit 1
fi
"""
    else:
        restart_block = ""

    done = (
        f"{MARKER_DONE} old=$OLD_VER new=$NEW_VER "
        f"restarted={'yes' if restart else 'no'} "
        f"config={'applied' if node_config is not None else 'unchanged'}"
    )
    return f"""\
set -euo pipefail
STAGING="{staging}"
trap 'rm -f "$STAGING"' EXIT
{fetch}
chmod +x "$STAGING"
if ! NEW_VER=$("$STAGING" --version 2>/dev/null | head -n1); then
    echo "{MARKER_BAD_BINARY}"
    exit 1
fi
OLD_VER=$("{install_path}" --version 2>/dev/null | head -n1 || echo unknown)
mv -f "$STAGING" "{install_path}"
trap - EXIT
{config_block}{restart_block}echo "{done}"
"""


def add_swap_binary_op(
    state,
    download_url: str,
    is_archive: bool,
    node_count: int,
    restart: bool = True,
    node_config: str | None = None,
):
    """Register the swap as a single pyinfra shell op on every host in ``state``.

    The script is base64-encoded and piped through ``bash -s`` (the same shape
    as ``set_reserved_blocks``): pyinfra's default shell is ``sh``, which has no
    ``pipefail``, and the script relies on it to catch a ``--version`` that fails
    behind ``head``.
    """
    script = build_swap_binary_command(
        download_url, is_archive, node_count, restart=restart, node_config=node_config
    )
    encoded = base64.b64encode(script.encode()).decode()
    return add_op(
        state,
        server.shell,
        name="Swap ant-node binary and restart services",
        commands=[f"echo {encoded} | base64 -d | bash -s"],
    )
