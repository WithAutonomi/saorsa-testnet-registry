"""Shell builders for the WASM (browser-path) client bundle.

Extracted from :meth:`AntClientProvisioner._add_wasm_ops`, which held all of it
inline. The prod client kinds need the same four pieces — Node, the bundle, the
``ant-wasm`` CLI, and an env file — but none of the testnet tier's roles, S3
address pool or long-running systemd unit, so the pieces had to come out of the
testnet provisioner before a second caller could have them.

Everything here returns a shell string and touches nothing: the callers wrap
them in pyinfra ops, and the tests assert on the rendered text. The testnet
tier's env file and both ``ant-wasm-*`` units render byte-identically to the
pre-extraction output (asserted by ``test_wasm_client_provisioning.py``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

WASM_CLIENT_DIR = "/opt/ant-wasm-client"
WASM_CLIENT_DATA_DIR = "/var/lib/ant-wasm-client"
WASM_CLIENT_LOG_DIR = "/var/log/ant-wasm-client"
WASM_CLIENT_ENV_PATH = "/etc/ant-wasm-client.env"

#: ant-client's CI builds and tests the WASM package on Node 22, and the bundle
#: ships the `node-datachannel` prebuilt addon compiled against it.
WASM_NODE_MAJOR = "22"

#: The single-operation CLI wrapper, a drop-in for `ant` as far as an invoking
#: service is concerned (`ant-wasm file download <addr> -o <path>`). It exists
#: because the prod client services already shell out to a transfer binary and
#: capture its streams; giving the browser path the same shape means the prod
#: Python services, their units, their per-op logs and their restart policies
#: are unchanged by the transport.
ANT_WASM_CLI_PATH = "/usr/local/bin/ant-wasm"

#: Entry point the CLI wrapper runs, and the long-running testnet harness entry.
WASM_CLI_ENTRY = f"{WASM_CLIENT_DIR}/dist/cli.js"
WASM_SERVICE_ENTRY = f"{WASM_CLIENT_DIR}/dist/main.js"

#: Build provenance written into the tarball root by `build ant-wasm`.
WASM_BUILD_INFO_PATH = f"{WASM_CLIENT_DIR}/build-info.json"


def build_node_install_command(node_major: str = WASM_NODE_MAJOR) -> str:
    """Install Node from NodeSource unless the required MAJOR is already there.

    Gated on the major version, not on ``command -v node``. Ubuntu 24.04 ships
    ``nodejs`` 18.19.1, and on an image that already carries it a presence gate
    skips the install and leaves the bundle running on the wrong major — where
    ``getrandom``'s Node-ESM path panics inside ant-core's constructor
    (``could not initialize thread_rng``) and every transfer fails. Measured on
    two DigitalOcean VMs created minutes apart from the same size and region: one
    had no node and got 22.23.3, the other had 18.19.1 and was skipped, and only
    the second failed. The bundles were byte-identical.

    NodeSource's setup script plus ``apt-get install nodejs`` upgrades an existing
    wrong-major install, so this is also the repair path, and it stays idempotent
    on a re-provision.
    """
    return (
        "FOUND=$(node --version 2>/dev/null | "
        "sed -n 's/^v\\([0-9][0-9]*\\)\\..*/\\1/p'); "
        f'if [ "$FOUND" = "{node_major}" ]; then echo \'NODE:SKIP\'; else '
        f"curl -fsSL https://deb.nodesource.com/setup_{node_major}.x | bash - && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nodejs && "
        f'echo "NODE:INSTALLED:$(node --version) (was ${{FOUND:-none}})"; fi'
    )


def build_bundle_install_command(
    bundle_url: str,
    *,
    data_dirs: Iterable[str] = (WASM_CLIENT_DATA_DIR, WASM_CLIENT_LOG_DIR),
    require_cli: bool = False,
) -> str:
    """Fetch, unpack and verify the WASM client bundle.

    Always re-fetched rather than presence-gated on the directory: the bundle is
    namespaced by deployment name and the builder overwrites it, so a
    re-provision after a rebuild must pick up the new one. (The ant binary
    install IS presence-gated, which is exactly why `deployments
    upgrade-ant-binary` had to exist.)

    ``require_cli`` additionally asserts ``dist/cli.js`` is present. The prod
    kinds set it, because a bundle built before the CLI existed would otherwise
    install cleanly and then fail on every single transfer.
    """
    dirs = " ".join([WASM_CLIENT_DIR, *data_dirs])
    checks = [
        f"test -f {WASM_CLIENT_DIR}/dist/main.js",
        f"test -f {WASM_CLIENT_DIR}/pkg/ant_core_bg.wasm",
    ]
    if require_cli:
        checks.append(f"test -f {WASM_CLI_ENTRY}")
    return (
        f"rm -rf {WASM_CLIENT_DIR} && mkdir -p {dirs} && "
        f"wget -q '{bundle_url}' -O /tmp/ant-wasm-client.tar.gz && "
        f"tar xzf /tmp/ant-wasm-client.tar.gz -C {WASM_CLIENT_DIR} && "
        f"rm -f /tmp/ant-wasm-client.tar.gz && " + " && ".join(checks)
    )


def build_ant_wasm_cli_command() -> str:
    """Install the ``ant-wasm`` CLI wrapper.

    A two-line shell wrapper rather than a symlink into the bundle: the entry
    point is an ESM module that has to be run by node, and `exec` keeps the
    signal semantics the calling service depends on — an OOM kill must reach the
    node process and surface to the parent as a signal exit.
    """
    script = f'#!/bin/sh\nexec /usr/bin/node {WASM_CLI_ENTRY} "$@"\n'
    return (
        f"cat > {ANT_WASM_CLI_PATH} << 'ANT_WASM_EOF'\n"
        f"{script}ANT_WASM_EOF\n"
        f"chmod +x {ANT_WASM_CLI_PATH}"
    )


def render_env_file(env: Mapping[str, object], *, quoted: Iterable[str] = ()) -> str:
    """Render an env dict as an ``EnvironmentFile`` body, in insertion order.

    ``quoted`` names the keys whose value is wrapped in double quotes. It is
    explicit rather than inferred because the quoting is part of the file's
    bytes, and the testnet tier's file has to keep rendering exactly as it did.
    """
    quote = set(quoted)
    lines = [f'{key}="{value}"' if key in quote else f"{key}={value}" for key, value in env.items()]
    return "\n".join(lines) + "\n"


def build_systemd_unit(
    description: str,
    *,
    exec_start: str,
    env_path: str = WASM_CLIENT_ENV_PATH,
    working_directory: str = WASM_CLIENT_DIR,
    restart: str = "on-failure",
    restart_sec: int = 10,
) -> str:
    """systemd unit for a long-running WASM client service.

    Used by the testnet tier only. The prod kinds keep their own native Python
    services and units — the transport changes which binary the service spawns,
    not what the service is.
    """
    return f"""\
[Unit]
Description={description}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile={env_path}
Environment=NODE_ENV=production
WorkingDirectory={working_directory}
ExecStart={exec_start}
Restart={restart}
RestartSec={restart_sec}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""
