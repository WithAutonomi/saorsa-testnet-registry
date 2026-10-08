"""Force-upgrade the native `ant` binary in place on a running prod client VM.

Covers every prod client kind that executes the native `ant` binary —
``prod-downloaders`` and ``prod-continuous-downloader`` (which run
``prod_downloader.py``), and ``prod-continuous-uploader`` (whose uploader VM
runs ``prod_continuous_uploader.py`` and whose verifier VMs run
``prod_upload_verifier.py``) — over SSH, without touching Terraform
infrastructure. The binary install ops used by ``deploy`` are presence-gated
(they skip if ``ant`` is already installed), so a plain re-deploy never picks up
a new release. This command bypasses that gate via
:func:`build_ant_binary_upgrade_command`.

``prod-continuous-uploader`` is the one multi-role kind, so hosts are grouped by
the unit they actually run and each group gets its own restart. Its uploader VM
is skipped entirely when the deployment runs the browser path
(``deploy_args.client_kind == "wasm"``): there is no ``ant`` on it to upgrade,
but its verifier VMs are deliberately native and do want one.
"""

import sys
from dataclasses import dataclass, field

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.binary_descriptor import build_binary_descriptor
from saorsa_deploy.client_roles import ROLE_UPLOAD
from saorsa_deploy.cmd.provision_clients import _resolve_ant_binary
from saorsa_deploy.provisioning.prod_continuous_uploader import (
    UPLOADER_SERVICE,
    VERIFIER_SERVICE,
)
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

_VALID_KINDS = {
    "prod-downloaders",
    "prod-continuous-downloader",
    "prod-continuous-uploader",
}

#: The single-role kinds, every VM of which runs `prod_downloader.py`.
_DOWNLOADER_KINDS = {"prod-downloaders", "prod-continuous-downloader"}


@dataclass
class HostGroup:
    """Hosts that run one systemd unit, and so take one restart together."""

    #: What to call this set in the output, e.g. "uploader", "verifier".
    label: str
    service: str
    host_ips: list[str]
    failed_hosts: list[str] = field(default_factory=list)


def group_hosts_by_unit(
    kind: str, clients: list[dict], client_kind: str = "native"
) -> list[HostGroup]:
    """Split a deployment's client VMs into one group per systemd unit.

    The downloader kinds are single-role: every VM runs `prod_downloader.py`
    under `ant-prod-downloader`, which is what this command did for every kind
    before `prod-continuous-uploader` was supported.

    `prod-continuous-uploader` carries two roles that both execute the native
    `ant` binary but under different units, so restarting one hard-coded unit
    name would miss both. Its uploader VM is dropped when the deployment runs
    the browser path: `ProdContinuousUploaderProvisioner._add_install_ant_op`
    returns early for `client_kind: wasm`, so there is no `ant` on that VM —
    while the verifier VMs stay native by design and do want the upgrade.

    Pure, so the grouping is testable without SSH: the failure mode is a
    restart aimed at a unit that does not exist on the host it is sent to.
    """
    ips = [c["ip"] for c in clients if c.get("ip")]
    if kind not in _VALID_KINDS or kind in _DOWNLOADER_KINDS:
        # An unknown kind falls through to here deliberately — see
        # `_resolve_deployment`: with no registry the command still runs on
        # client state alone, as it always has.
        return [HostGroup("downloader", PROD_DOWNLOADER_SERVICE_NAME, ips)] if ips else []

    groups: list[HostGroup] = []
    uploader_ips = [
        c["ip"] for c in clients if c.get("ip") and c.get("role", ROLE_UPLOAD) == ROLE_UPLOAD
    ]
    verifier_ips = [
        c["ip"] for c in clients if c.get("ip") and c.get("role", ROLE_UPLOAD) != ROLE_UPLOAD
    ]
    if uploader_ips and client_kind != "wasm":
        groups.append(HostGroup("uploader", UPLOADER_SERVICE, uploader_ips))
    if verifier_ips:
        groups.append(HostGroup("verifier", VERIFIER_SERVICE, verifier_ips))
    return groups


def _resolve_kind(deploy_args) -> str:
    if not isinstance(deploy_args, dict):
        return ""
    return deploy_args.get("kind") or ""


def _resolve_client_kind(deploy_args) -> str:
    """The transport, defaulting to native as the provisioner's own does."""
    if not isinstance(deploy_args, dict):
        return "native"
    return deploy_args.get("client_kind") or "native"


def _resolve_deployment(name: str, console: Console) -> tuple[str, str]:
    """Return ``(kind, client_kind)``, rejecting a known-wrong kind.

    Best-effort: a known-wrong kind is refused, but an unavailable registry (or
    a deployment missing from it) only warns, because the core mechanism below
    needs nothing but S3 client state and SSH. In that case the caller is left
    with the single-unit downloader grouping this command has always used —
    which is right for three of the four permanent prod deployments and wrong
    only for an uploader, so the warning says so.
    """
    repo = get_repository()
    if repo is None:
        console.print(
            "[yellow]Warning:[/yellow] DATABASE_URL not set — skipping kind "
            "validation, proceeding on client state alone and assuming a "
            f"downloader deployment ('{PROD_DOWNLOADER_SERVICE_NAME}')."
        )
        return "", "native"

    deployment = repo.get_active_by_name(name) or repo.get_by_name(name)
    if deployment is None:
        console.print(
            f"[yellow]Warning:[/yellow] no registry row for '{name}' — "
            "skipping kind validation and assuming a downloader deployment "
            f"('{PROD_DOWNLOADER_SERVICE_NAME}')."
        )
        return "", "native"

    kind = _resolve_kind(deployment.deploy_args)
    if kind and kind not in _VALID_KINDS:
        console.print(
            f"[bold red]Error:[/bold red] '{name}' is a '{kind}' deployment. "
            f"upgrade-ant-binary only supports: {', '.join(sorted(_VALID_KINDS))}."
        )
        sys.exit(1)
    return kind, _resolve_client_kind(deployment.deploy_args)


def _validate_kind(name: str, console: Console) -> None:
    """Kept as the narrow kind check, for callers that want only that."""
    _resolve_deployment(name, console)


def _upgrade_group(
    group: HostGroup, commands: list[str], ssh_key_path: str, console: Console
) -> None:
    """Install on one group's hosts, recording failures rather than raising.

    One unreachable host must not stop the others: a partial upgrade with an
    accurate record of which hosts took it beats a halt that leaves the fleet
    on a mix of binaries with nothing saying which is which.
    """
    console.print()
    console.print(
        f"[bold]{group.label}[/bold]: {len(group.host_ips)} host(s), unit {group.service}"
    )
    clear_known_hosts(group.host_ips, console)
    hosts_data = [(ip, {"ssh_user": "root", "ssh_key": ssh_key_path}) for ip in group.host_ips]
    state = State(inventory=Inventory((hosts_data, {})), config=Config())
    try:
        connect_all(state)
        add_op(
            state,
            server.shell,
            name=f"Upgrade ant binary for the {group.label} role",
            commands=commands,
        )
        run_ops(state)
    except Exception as exc:  # noqa: BLE001 — one group must not stop the rest
        console.print(f"  [red]Failed: {exc}[/red]")
        group.failed_hosts = list(group.host_ips)
        return
    finally:
        try:
            disconnect_all(state)
        except Exception:  # noqa: BLE001
            pass

    group.failed_hosts = [host.name for host in (state.failed_hosts or set())]


def cmd_upgrade_ant_binary(args) -> None:
    console = Console()

    kind, client_kind = _resolve_deployment(args.name, console)
    if kind in _DOWNLOADER_KINDS and client_kind == "wasm":
        # Warned, not refused: accepting a wholly-WASM download deployment is
        # behaviour that predates this command understanding roles, and
        # narrowing it is not what the issue asked for. But a WASM downloader's
        # provisioner installs the bundle *instead of* `ant`, so the install
        # below writes a binary nothing on the VM ever executes and the restart
        # costs a cycle for nothing.
        console.print(
            f"[yellow]Warning:[/yellow] '{args.name}' runs the browser path "
            "(client_kind: wasm), so no service on it executes the native "
            "`ant` binary. Use `deployments upgrade-wasm-client` to change "
            "what it actually runs."
        )

    try:
        client_state = load_client_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    clients = client_state.get("clients", [])
    groups = group_hosts_by_unit(kind, clients, client_kind)
    if not groups:
        if clients and kind == "prod-continuous-uploader" and client_kind == "wasm":
            console.print(
                "[bold red]Error:[/bold red] every VM in this deployment runs "
                "the browser path, so there is no native `ant` binary to "
                "upgrade. Use `deployments upgrade-wasm-client` instead."
            )
        else:
            console.print("[bold red]Error:[/bold red] No client IPs found in state.")
        sys.exit(1)

    binary_url, binary_is_archive = _resolve_ant_binary(args, console)
    if binary_url is None:
        from saorsa_deploy.binary_source import get_ant_release_url

        console.print("Fetching latest ant release from GitHub...")
        binary_url = get_ant_release_url()
        console.print(f"  Release URL: {binary_url}")

    record_event(args.name, "ant_binary_upgrade_started", {"binary_url": binary_url})

    install_command = build_ant_binary_upgrade_command(binary_url, binary_is_archive)
    for group in groups:
        commands = [install_command]
        if args.restart:
            commands.append(f"systemctl restart {group.service}")
        _upgrade_group(group, commands, args.ssh_key_path, console)

    total = sum(len(g.host_ips) for g in groups)
    failed = [h for g in groups for h in g.failed_hosts]
    console.print()
    console.print(
        f"[bold]ant binary upgrade complete: {total - len(failed)}/{total} host(s) succeeded[/bold]"
    )
    for group in groups:
        took = len(group.host_ips) - len(group.failed_hosts)
        state_note = (
            f"{group.service} restarted"
            if args.restart
            else f"{group.service} NOT restarted (--no-restart)"
        )
        console.print(f"  {group.label}: {took}/{len(group.host_ips)} host(s) — {state_note}")
    if not args.restart:
        console.print("  Binary is updated on disk; restart each unit above manually to run it.")

    if failed:
        for host in failed:
            console.print(f"  [red]Failed: {host}[/red]")
        record_event(
            args.name,
            "ant_binary_upgrade_failed",
            {"failed_hosts": failed},
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
    record_event(args.name, "ant_binary_upgrade_completed", {"binary_url": binary_url})
