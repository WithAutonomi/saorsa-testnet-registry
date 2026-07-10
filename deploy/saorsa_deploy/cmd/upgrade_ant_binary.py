"""Force-upgrade the `ant` binary in place on a running prod downloader VM.

Targets the two client kinds that run the ``prod_downloader.py`` service —
``prod-downloaders`` (one-shot) and ``prod-continuous-downloader`` — over
SSH, without touching Terraform infrastructure. The binary install op used
by ``deploy`` (:meth:`ProdDownloaderProvisioner._add_install_ant_op`) is
presence-gated (skips if ``ant`` is already installed), so a plain re-deploy
never picks up a new release. This command bypasses that gate via
:func:`build_ant_binary_upgrade_command`.
"""

import sys

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.binary_descriptor import build_binary_descriptor
from saorsa_deploy.cmd.provision_clients import _resolve_ant_binary
from saorsa_deploy.provisioning.prod_downloader import (
    SERVICE_NAME as PROD_DOWNLOADER_SERVICE_NAME,
)
from saorsa_deploy.provisioning.prod_downloader import (
    build_ant_binary_upgrade_command,
)
from saorsa_deploy.registry import (
    get_repository,
    record_deploy_args_update,
    record_event,
)
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_client_state

_VALID_KINDS = {"prod-downloaders", "prod-continuous-downloader"}


def _resolve_kind(deploy_args) -> str:
    if not isinstance(deploy_args, dict):
        return ""
    return deploy_args.get("kind") or ""


def _validate_kind(name: str, console: Console) -> None:
    """Best-effort kind check: reject a known-wrong kind, warn and proceed
    when the registry (or the deployment row in it) is unavailable — the
    core mechanism below only needs S3 client state and SSH.
    """
    repo = get_repository()
    if repo is None:
        console.print(
            "[yellow]Warning:[/yellow] DATABASE_URL not set — skipping kind "
            "validation, proceeding on client state alone."
        )
        return

    deployment = repo.get_active_by_name(name) or repo.get_by_name(name)
    if deployment is None:
        console.print(
            f"[yellow]Warning:[/yellow] no registry row for '{name}' — "
            "skipping kind validation."
        )
        return

    kind = _resolve_kind(deployment.deploy_args)
    if kind and kind not in _VALID_KINDS:
        console.print(
            f"[bold red]Error:[/bold red] '{name}' is a '{kind}' deployment. "
            f"upgrade-ant-binary only supports: {', '.join(sorted(_VALID_KINDS))}."
        )
        sys.exit(1)


def cmd_upgrade_ant_binary(args) -> None:
    console = Console()

    _validate_kind(args.name, console)

    try:
        client_state = load_client_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    host_ips = [c["ip"] for c in client_state.get("clients", []) if c.get("ip")]
    if not host_ips:
        console.print("[bold red]Error:[/bold red] No client IPs found in state.")
        sys.exit(1)

    binary_url, binary_is_archive = _resolve_ant_binary(args, console)
    if binary_url is None:
        from saorsa_deploy.binary_source import get_ant_release_url

        console.print("Fetching latest ant release from GitHub...")
        binary_url = get_ant_release_url()
        console.print(f"  Release URL: {binary_url}")

    record_event(
        args.name, "ant_binary_upgrade_started", {"binary_url": binary_url}
    )

    clear_known_hosts(host_ips, console)
    hosts_data = [
        (ip, {"ssh_user": "root", "ssh_key": args.ssh_key_path}) for ip in host_ips
    ]
    state = State(inventory=Inventory((hosts_data, {})), config=Config())
    connect_all(state)

    commands = [build_ant_binary_upgrade_command(binary_url, binary_is_archive)]
    if args.restart:
        commands.append(f"systemctl restart {PROD_DOWNLOADER_SERVICE_NAME}")

    try:
        add_op(
            state,
            server.shell,
            name="Upgrade ant binary"
            + (" and restart service" if args.restart else ""),
            commands=commands,
        )
        run_ops(state)
    finally:
        disconnect_all(state)

    failed = state.failed_hosts
    total = len(host_ips)
    succeeded = total - len(failed)
    console.print()
    console.print(
        f"[bold]ant binary upgrade complete: {succeeded}/{total} host(s) "
        f"succeeded[/bold]"
    )
    if args.restart:
        console.print(f"  Service '{PROD_DOWNLOADER_SERVICE_NAME}' restarted.")
    else:
        console.print(
            f"  Service NOT restarted (--no-restart). Binary is updated on "
            f"disk; restart '{PROD_DOWNLOADER_SERVICE_NAME}' manually to run it."
        )

    if failed:
        for host in failed:
            console.print(f"  [red]Failed: {host.name}[/red]")
        record_event(
            args.name,
            "ant_binary_upgrade_failed",
            {"failed_hosts": [h.name for h in failed]},
        )
        sys.exit(1)

    record_deploy_args_update(
        args.name,
        {
            "ant_binary": build_binary_descriptor(
                getattr(args, "branch_name", None),
                getattr(args, "repo_owner", None),
                getattr(args, "ant_version", None),
            ),
        },
    )
    record_event(
        args.name, "ant_binary_upgrade_completed", {"binary_url": binary_url}
    )
