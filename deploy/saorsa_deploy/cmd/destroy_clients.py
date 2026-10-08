import os
import shutil
import sys
import time
from pathlib import Path

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console

from saorsa_deploy.client_roles import (
    ALL_CLIENT_ROLES,
    ROLE_DOWNLOAD,  # noqa: F401  (re-exported; used by callers and tests)
    ROLE_LABELS,
    ROLE_UPLOAD,
    is_upload,
    unit_for_role,
)
from saorsa_deploy.evm import ARBITRUM_SEPOLIA
from saorsa_deploy.providers import CLIENT_PROVIDERS
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import (
    delete_client_addresses_prefix,
    delete_client_state,
    delete_terraform_state,
    list_terraform_state_keys,
    load_client_state,
    load_terraform_state,
    terraform_state_attributes,
    update_client_state,
)
from saorsa_deploy.terraform import TerraformRunConfig, run_terraform_destroy
from saorsa_deploy.wallet import (
    address_for_secret_key,
    drain_wallet,
    get_eth_balance,
    get_token_balance,
    get_web3,
)

VALID_ROLES = ALL_CLIENT_ROLES


def cmd_destroy_clients(args):
    """Execute the clients destroy command: tear down client VMs."""
    console = Console()

    console.print(f"[bold]Loading client state for '{args.name}'...[/bold]")
    clients: list[dict] = []
    state_ssh_key_path: str | None = None
    has_persisted_state = False
    try:
        client_state = load_client_state(args.name)
        clients = client_state.get("clients", []) or []
        state_ssh_key_path = client_state.get("ssh_key_path")
        has_persisted_state = True
    except RuntimeError:
        console.print(
            "[yellow]No client state in S3 — scanning for orphan client tfstate files...[/yellow]"
        )
        clients = _recover_clients_from_tfstate(args.name, console)

    if not clients:
        console.print(
            f"[yellow]No client infrastructure found for '{args.name}'. "
            "Nothing to destroy.[/yellow]"
        )
        return

    console.print(f"[dim]Found {len(clients)} client VM(s) to destroy.[/dim]")
    for c in clients:
        role = c.get("role", ROLE_UPLOAD)
        console.print(
            f"  [dim]{role} #{c['client_index']}: {c.get('ip', 'N/A')} "
            f"({c.get('provider', 'digitalocean')}:{c['region']}, "
            f"{c['vm_size']})[/dim]"
        )
    console.print()

    if not args.force:
        confirm = input(
            f"This will destroy all client VMs for '{args.name}'. Type 'yes' to confirm: "
        )
        if confirm.strip().lower() != "yes":
            console.print("[yellow]Aborted.[/yellow]")
            sys.exit(0)
        console.print()

    # Drain per-uploader wallets back to the funding wallet *before* tearing
    # down infra. We do this even if VMs are gone — wallet keys live in S3
    # client state, and the on-chain funds are independent of the VMs. If the
    # drain fails we abort, leaving the VMs and state intact so the operator
    # can fix the issue and retry.
    uploaders_with_wallet = [
        c for c in clients if is_upload(c.get("role", ROLE_UPLOAD)) and c.get("wallet_secret_key")
    ]
    if uploaders_with_wallet:
        funding_secret_key = getattr(args, "wallet_secret_key", None) or os.environ.get(
            "ANT_DEPLOY_SECRET_KEY"
        )
        if not funding_secret_key:
            console.print(
                "[bold red]Error:[/bold red] Per-uploader wallets are present "
                "but no funding wallet secret key was provided. Use "
                "--wallet-secret-key or set ANT_DEPLOY_SECRET_KEY so funds "
                "can be drained back."
            )
            sys.exit(1)
        # SSH key precedence: an explicit --ssh-key-path on this command wins;
        # otherwise use the key recorded in client state at provision time;
        # only then fall back to the historical default. Using the default
        # blindly is what made the stop step fail with an auth error when the
        # VMs were provisioned with a non-default key.
        stop_ssh_key_path = (
            getattr(args, "ssh_key_path", None) or state_ssh_key_path or "~/.ssh/id_rsa"
        )
        if getattr(args, "skip_stop_services", False):
            console.print("[dim]Skipping ant-uploader stop step (--skip-stop-services).[/dim]")
        else:
            _stop_uploader_services(
                uploaders_with_wallet,
                stop_ssh_key_path,
                console,
            )
        _drain_uploader_wallets(
            uploaders_with_wallet,
            funding_secret_key,
            console,
            skip_failures=getattr(args, "skip_drain_failures", False),
        )

    # OVH destroy needs the project UUID for the provider to authenticate and
    # target the right project, same as create.
    ovh_service_name = None
    if any((c.get("provider") or "digitalocean") == "ovh" for c in clients):
        ovh_service_name = os.environ.get("OVH_CLOUD_PROJECT_SERVICE")
        if not ovh_service_name:
            console.print(
                "[bold red]Error:[/bold red] OVH client(s) present but "
                "OVH_CLOUD_PROJECT_SERVICE is not set; cannot destroy them. "
                "Export it (the OVH Public Cloud project ID) and retry."
            )
            sys.exit(1)

    resources_dir = get_resources_dir()
    workspace_base = Path.cwd() / ".saorsa" / "workspaces"

    configs = []
    for spec in clients:
        role = spec.get("role", ROLE_UPLOAD)
        info = CLIENT_PROVIDERS[spec.get("provider") or "digitalocean"]
        tf_source = resources_dir / info.tf_dir
        workspace_dir = (
            workspace_base / f"{info.name}-client-{args.name}-{role}"
            f"-{spec['region']}-{spec['client_index']}"
        )
        state_key = (
            f"{info.state_key_prefix}-{args.name}-{role}"
            f"-{spec['region']}-{spec['client_index']}.tfstate"
        )
        variables = {
            "name": args.name,
            "role": role,
            "region": spec["region"],
            "client_index": str(spec["client_index"]),
            info.size_var: spec.get("vm_size", "s-2vcpu-4gb"),
        }
        if info.name == "ovh":
            variables["service_name"] = ovh_service_name
        config = TerraformRunConfig(
            provider=info.name,
            region=spec["region"],
            tf_source_dir=tf_source,
            workspace_dir=workspace_dir,
            state_key=state_key,
            variables=variables,
        )
        configs.append(config)

    console.print(f"[bold]Destroying {len(configs)} client VM(s)...[/bold]")

    failures = []
    remaining_clients = list(clients)
    for spec, config in zip(clients, configs):
        result = run_terraform_destroy(config)
        if not result.success:
            console.print(
                f"[red]Failed to destroy {config.region} "
                f"(client #{config.variables.get('client_index', '?')}):[/red]"
            )
            console.print(f"  {result.stderr[:500]}")
            failures.append(config)
        else:
            console.print(
                f"[green]  Destroyed client in {config.region} "
                f"(#{config.variables.get('client_index', '?')})[/green]"
            )
            # Clean up Terraform state
            try:
                delete_terraform_state(config.state_key)
            except Exception:
                pass
            # Prune the destroyed client from persisted state immediately, so
            # a retry after a later failure doesn't reprocess (stop/drain/
            # destroy) VMs already gone. Skip when state was recovered from
            # orphan tfstate scans — there's no persisted client state to prune.
            if has_persisted_state:
                remaining_clients.remove(spec)
                try:
                    update_client_state(args.name, {"clients": remaining_clients})
                except Exception as e:
                    console.print(
                        f"[yellow]Warning: failed to prune destroyed client from "
                        f"state: {e}[/yellow]"
                    )

    # Clean up workspace directories
    for config in configs:
        if config.workspace_dir.exists():
            shutil.rmtree(config.workspace_dir, ignore_errors=True)

    if failures:
        console.print(f"[bold red]{len(failures)} client VM(s) failed to destroy.[/bold red]")
        sys.exit(1)

    # Clean up client state
    try:
        delete_client_state(args.name)
        console.print("[dim]Client state deleted from S3.[/dim]")
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to delete client state: {e}[/yellow]")

    # Wipe the per-testnet addresses S3 folder so a future testnet with the
    # same name doesn't see stale uploader addresses.
    try:
        deleted = delete_client_addresses_prefix(args.name)
        if deleted:
            console.print(
                f"[dim]Deleted {deleted} address file(s) from "
                f"s3://.../saorsa-deploy/addresses/{args.name}/[/dim]"
            )
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to delete addresses prefix: {e}[/yellow]")

    console.print()
    console.print(
        f"[bold green]All {len(clients)} client VM(s) destroyed successfully.[/bold green]"
    )


def _stop_uploader_services(
    uploaders: list[dict],
    ssh_key_path: str,
    console: Console,
) -> None:
    """Best-effort: stop each wallet-holding VM's service before draining.

    Every upload-type role pays from its own per-VM wallet in a loop, so draining
    while it's live races a spender that keeps bumping the wallet's nonce ("nonce
    too low"). Stopping it first gives the drain a stable nonce and a final
    balance to sweep. VMs that can't be reached (e.g. already torn down on a
    retry) are skipped — the drain still works against on-chain state without
    them.

    The unit comes from each VM's OWN role. This stopped only ``ant-uploader``,
    so a WASM uploader or a pointer client was drained while it was still
    spending — precisely the race the stop exists to avoid.
    """
    with_units = [
        (u["ip"], unit_for_role(u.get("role", ROLE_UPLOAD))) for u in uploaders if u.get("ip")
    ]
    host_ips = [ip for ip, _ in with_units]
    if not host_ips:
        return
    units = sorted({unit for _, unit in with_units})
    console.print(
        f"[bold]Stopping {', '.join(units)} on {len(host_ips)} wallet-holding "
        "VM(s) before draining...[/bold]"
    )
    clear_known_hosts(host_ips, console)
    hosts_data = [(ip, {"ssh_user": "root", "ssh_key": ssh_key_path}) for ip in host_ips]
    state = State(inventory=Inventory((hosts_data, {})), config=Config())
    try:
        connect_all(state)
        unit_by_ip = dict(with_units)
        for host in state.inventory.get_active_hosts():
            unit = unit_by_ip.get(host.name)
            if unit is None:
                continue
            add_op(
                state,
                server.shell,
                name=f"Stop {unit} service",
                commands=[
                    f"systemctl stop {unit} 2>/dev/null || true; "
                    f"systemctl disable {unit} 2>/dev/null || true"
                ],
                host=[host],
            )
        run_ops(state)
    except Exception as e:  # noqa: BLE001 — stopping is best-effort
        console.print(
            f"[yellow]Warning: could not stop uploader service(s); draining anyway: {e}[/yellow]"
        )
        return
    finally:
        try:
            disconnect_all(state)
        except Exception:
            pass
    unreachable = [h.name for h in state.failed_hosts]
    if unreachable:
        console.print(
            f"[yellow]Could not reach {len(unreachable)} uploader VM(s) to stop "
            "(already gone?); draining against on-chain state anyway.[/yellow]"
        )
    # Let any in-flight payment tx land before we read pending nonces.
    time.sleep(5)


def _wallet_label(client: dict) -> str:
    """`<Role> #<index>` for a wallet-holding client. See the note at its use."""
    role = client.get("role", ROLE_UPLOAD)
    return f"{ROLE_LABELS.get(role, role)} #{client['client_index']}"


def _drain_uploader_wallets(
    uploaders: list[dict],
    funding_secret_key: str,
    console: Console,
    skip_failures: bool = False,
) -> None:
    """Sweep ANT and ETH from every per-uploader wallet back to the funding
    wallet on Arbitrum Sepolia. Raises SystemExit on any failure so the destroy
    aborts before terraform tears down the VMs — unless ``skip_failures`` is
    set, in which case abandoned balances are reported and teardown continues.
    """
    funding_address = address_for_secret_key(funding_secret_key)
    rpc_url = ARBITRUM_SEPOLIA["rpc_url"]
    token_address = ARBITRUM_SEPOLIA["payment_token_address"]
    console.print(
        f"[bold]Draining {len(uploaders)} per-uploader wallet(s) "
        f"→ {funding_address} on Arbitrum Sepolia...[/bold]"
    )
    w3 = get_web3(rpc_url)
    failures: list[tuple[int, str, str, int, int]] = []
    for client in uploaders:
        secret = client["wallet_secret_key"]
        addr = client.get("wallet_address") or address_for_secret_key(secret)
        ant_units = 0
        eth_wei = 0
        try:
            ant_units = get_token_balance(w3, token_address, addr)
            eth_wei = get_eth_balance(w3, addr)
            # The role, not a literal "Uploader": `client_index` restarts at 1
            # per role, so a native uploader and a pointer client in the same
            # region both read "#1" against two different wallets. This is the
            # audit trail for money coming back, so it has to name the VM.
            console.print(
                f"  {_wallet_label(client)} {addr}: {ant_units} ANT-units / {eth_wei} wei"
            )
            if ant_units == 0 and eth_wei == 0:
                continue
            txs = drain_wallet(w3, secret, token_address, funding_address)
            console.print(f"    drained ant_tx={txs['ant_tx']} eth_tx={txs['eth_tx']}")
        except Exception as e:
            console.print(f"    [red]drain failed: {e}[/red]")
            failures.append((_wallet_label(client), addr, str(e), ant_units, eth_wei))
    if failures:
        if skip_failures:
            abandoned_ant = sum(f[3] for f in failures)
            abandoned_wei = sum(f[4] for f in failures)
            console.print(
                f"[bold yellow]Drain failed for {len(failures)} uploader(s); "
                f"continuing because --skip-drain-failures is set.[/bold yellow]"
            )
            for label, addr, err, ant_units, eth_wei in failures:
                console.print(
                    f"  [yellow]abandoned[/yellow] {label} {addr}: "
                    f"{ant_units} ANT-units / {eth_wei} wei  ({err})"
                )
            console.print(
                f"[yellow]Total abandoned: {abandoned_ant} ANT-units / {abandoned_wei} wei[/yellow]"
            )
        else:
            console.print(
                f"[bold red]Drain failed for {len(failures)} uploader(s). "
                f"Aborting destroy so funds aren't lost — fix and retry, or "
                f"pass --skip-drain-failures to abandon the affected dust and "
                f"proceed.[/bold red]"
            )
            sys.exit(1)
    else:
        console.print("[green]All wallets drained.[/green]")
    console.print()


def _recover_clients_from_tfstate(name: str, console: Console) -> list[dict]:
    """Reconstruct client specs from S3-backed tfstate when client state is missing.

    Used when an earlier clients infra run crashed before saving client state,
    leaving orphan VMs that clients destroy otherwise couldn't see. Scans every
    provider's client-tfstate prefix so multi-provider deployments recover too.
    """
    recovered: list[dict] = []
    for info in CLIENT_PROVIDERS.values():
        prefix = f"{info.state_key_prefix}-{name}-"
        keys = list_terraform_state_keys(prefix)
        for key in keys:
            # Key shape:
            # {state_key_prefix}-{name}-{role}-{region}-{client_index}.tfstate
            suffix = key[len(prefix) : -len(".tfstate")]
            # The role must be matched as a whole known value, not taken as the
            # first "-"-separated token: `wasm-upload` contains a hyphen, so
            # splitting would read its role as "wasm" and silently skip the key
            # as unparseable — leaving a real VM running and billing.
            # Longest first, so `wasm-upload` is never shadowed by `upload`.
            role = next(
                (
                    r
                    for r in sorted(VALID_ROLES, key=len, reverse=True)
                    if suffix.startswith(f"{r}-")
                ),
                None,
            )
            parts = suffix.split("-")
            if role is None or not parts[-1].isdigit() or len(parts) < 3:
                console.print(f"[yellow]Skipping unparseable tfstate key: {key}[/yellow]")
                continue
            client_index = int(parts[-1])
            region = suffix[len(role) + 1 : -(len(parts[-1]) + 1)]

            vm_size = "s-2vcpu-4gb"
            try:
                tfstate = load_terraform_state(key)
                if info.tf_size_attr:
                    vm = terraform_state_attributes(
                        tfstate, info.tf_resource_type, info.tf_resource_name
                    )
                    if vm and vm.get(info.tf_size_attr):
                        vm_size = vm[info.tf_size_attr]
            except Exception:
                # Empty/corrupt tfstate (e.g. the init-only files from a failed
                # create) — nothing to destroy; skip.
                continue

            entry = {
                "role": role,
                "provider": info.name,
                "region": region,
                "client_index": client_index,
                "vm_size": vm_size,
                "ip": "",
            }
            if is_upload(role):
                entry["file_size_mb"] = 0
            recovered.append(entry)

    recovered.sort(key=lambda c: (c["provider"], c["role"], c["region"], c["client_index"]))
    console.print(f"[dim]Recovered {len(recovered)} client(s) from tfstate.[/dim]")
    return recovered
