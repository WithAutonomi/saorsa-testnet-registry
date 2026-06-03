import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from rich.console import Console

from saorsa_deploy.provisioning.telegraf import TelegrafProvisioner
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import load_deployment_state, load_elastic_state


def read_ips_from_file(file_path: str) -> list[str]:
    """Read IP addresses from a text file, one per line.

    Blank lines and lines starting with '#' are ignored.
    """
    with open(file_path) as f:
        ips = []
        for line in f:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                ips.append(stripped)
    if not ips:
        raise ValueError(f"No IP addresses found in {file_path}")
    return ips


def read_inventory(file_path: str) -> list[dict]:
    """Read all VM entries (bootstrap + node) from an inventory JSON file.

    Each returned dict has:
      - ``ip``, ``ssh_user``, ``ssh_key`` — connection info
      - ``name`` (optional) — inventory name, used for hostnamectl
      - ``role`` — one of ``GENESIS`` (bootstrap_index == 1),
        ``BOOTSTRAP`` (bootstrap_index >= 2), or ``WAN_NODE`` (node VM)
    """
    with open(file_path) as f:
        data = json.load(f)
    entries = []
    for vm in data.get("bootstrap_vms", []):
        idx = vm.get("bootstrap_index", 0)
        role = "GENESIS" if idx == 1 else "BOOTSTRAP"
        entry = {
            "ip": vm["ip"],
            "ssh_user": vm.get("ssh_user", "root"),
            "ssh_key": vm.get("ssh_key", "~/.ssh/id_rsa"),
            "role": role,
        }
        if "name" in vm:
            entry["name"] = vm["name"]
        entries.append(entry)
    for vm in data.get("node_vms", []):
        entry = {
            "ip": vm["ip"],
            "ssh_user": vm.get("ssh_user", "root"),
            "ssh_key": vm.get("ssh_key", "~/.ssh/id_rsa"),
            "role": "WAN_NODE",
        }
        if "name" in vm:
            entry["name"] = vm["name"]
        entries.append(entry)
    if not entries:
        raise ValueError(f"No bootstrap_vms or node_vms found in {file_path}")
    return entries


def group_by_role_and_credentials(
    entries: list[dict],
) -> dict[tuple, list[str]]:
    """Group inventory entries by (role, ssh_user, ssh_key), returning IPs per group."""
    groups: dict[tuple, list[str]] = defaultdict(list)
    for entry in entries:
        key = (entry["role"], entry["ssh_user"], entry["ssh_key"])
        groups[key].append(entry["ip"])
    return dict(groups)


def group_by_credentials(entries: list[dict]) -> dict[tuple, list[str]]:
    """Group inventory entries by (ssh_user, ssh_key), returning IPs per group."""
    groups = defaultdict(list)
    for entry in entries:
        key = (entry["ssh_user"], entry["ssh_key"])
        groups[key].append(entry["ip"])
    return dict(groups)


def set_hostnames(entries: list[dict], console: Console) -> None:
    """Set the system hostname on VMs that have a 'name' field in the inventory.

    Runs per-host because each VM needs a different hostname. Skips entries
    without a 'name' field or where the hostname already matches.
    """
    named = [e for e in entries if "name" in e]
    if not named:
        return

    console.print(
        f"[bold]Setting hostnames on {len(named)} VM(s)...[/bold]"
    )
    failed = []
    for entry in named:
        ip = entry["ip"]
        name = entry["name"]
        ssh_user = entry["ssh_user"]
        ssh_key = str(Path(entry["ssh_key"]).expanduser())
        sudo_prefix = "sudo " if ssh_user != "root" else ""
        cmd = (
            f"current=$(hostname) && "
            f'[ "$current" = "{name}" ] && echo "HOSTNAME:SKIP" || '
            f"({sudo_prefix}hostnamectl set-hostname {name} && "
            f'echo "HOSTNAME:SET")'
        )
        ssh_cmd = [
            "ssh", "-i", ssh_key,
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10",
            f"{ssh_user}@{ip}",
            cmd,
        ]
        try:
            result = subprocess.run(
                ssh_cmd, capture_output=True, text=True, timeout=30,
            )
            output = result.stdout.strip()
            if result.returncode != 0:
                console.print(
                    f"  [red]Failed {ip} ({name}):[/red] {result.stderr.strip()}"
                )
                failed.append(ip)
            elif "HOSTNAME:SKIP" in output:
                console.print(f"  [dim]{ip} already has hostname {name}[/dim]")
            else:
                console.print(f"  [green]{ip}[/green] → {name}")
        except subprocess.TimeoutExpired:
            console.print(f"  [red]Timeout setting hostname on {ip}[/red]")
            failed.append(ip)
    if failed:
        console.print(
            f"[yellow]Warning: hostname setting failed on "
            f"{len(failed)} host(s)[/yellow]"
        )
    console.print()


def _resolve_es_credentials(args, console):
    """Resolve Elasticsearch credentials from args or elastic state."""
    es_url = args.elasticsearch_url
    es_username = args.elasticsearch_username
    es_password = args.elasticsearch_password

    if not es_url and args.name:
        console.print(
            "[dim]Loading Elasticsearch credentials from elastic state...[/dim]"
        )
        try:
            elastic_state = load_elastic_state(args.name)
            es_url = elastic_state.get("elasticsearch_url", "")
            es_username = elastic_state.get("elasticsearch_username", "")
            es_password = elastic_state.get("elasticsearch_password", "")
        except RuntimeError:
            console.print(
                "[bold red]Error:[/bold red] No Elasticsearch credentials "
                "provided and no elastic state found.\n"
                "Provide --elasticsearch-url, --elasticsearch-username, "
                "and --elasticsearch-password."
            )
            sys.exit(1)

    if not es_url or not es_username or not es_password:
        console.print(
            "[bold red]Error:[/bold red] Elasticsearch URL, username, and "
            "password are all required. Provide them via CLI args, or also "
            "pass --name to load from elastic state."
        )
        sys.exit(1)

    return es_url, es_username, es_password


def _resolve_version_info(args):
    """Resolve node version and branch commit from args."""
    if args.node_version:
        node_version = args.node_version
    elif args.branch_commit:
        node_version = f"{args.repo_owner}/{args.branch_commit}"
    else:
        node_version = "unknown"
    branch_commit = args.branch_commit or "unknown"
    return node_version, branch_commit


def _resolve_telegraf_tuning(args):
    """Extract the optional Telegraf tuning knobs as a dict of kwargs.

    Returns only the keys that the caller actually provided, so the
    ``TelegrafProvisioner`` defaults keep working when callers (or tests)
    pass a ``SimpleNamespace`` without these fields.
    """
    kwargs: dict = {}
    drop = getattr(args, "drop_log_levels", None)
    if drop is not None:
        # Empty string -> keep every line; otherwise comma-split and upper.
        parsed = [lvl.strip().upper() for lvl in drop.split(",") if lvl.strip()]
        kwargs["drop_log_levels"] = parsed
    buffer_limit = getattr(args, "buffer_limit", None)
    if buffer_limit is not None:
        kwargs["metric_buffer_limit"] = buffer_limit
    max_undelivered = getattr(args, "max_undelivered_lines", None)
    if max_undelivered is not None:
        kwargs["max_undelivered_lines"] = max_undelivered
    return kwargs


def cmd_provision_telegraf(args):
    """Provision Telegraf for Elasticsearch log forwarding on all VMs."""
    console = Console()

    host_role_override = getattr(args, "host_role", None)

    if args.inventory_path:
        # Inventory mode: read VMs from inventory JSON
        try:
            entries = read_inventory(args.inventory_path)
        except (OSError, ValueError, KeyError) as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)

        # --host-role forces a single role for every targeted host. Needed for
        # auxiliary inventories like bootstrap-inventory.json whose entries
        # live under node_vms (and would otherwise be labelled WAN_NODE).
        if host_role_override:
            for entry in entries:
                entry["role"] = host_role_override

        all_ips = [e["ip"] for e in entries]
        console.print(
            f"[bold]Using {len(entries)} VM(s) from inventory "
            f"{args.inventory_path}...[/bold]"
        )

        es_url, es_username, es_password = _resolve_es_credentials(
            args, console
        )
        node_version, branch_commit = _resolve_version_info(args)
        testnet_name = args.name or ""

        console.print(f"  Elasticsearch URL: {es_url}")
        console.print(f"  Node version: {node_version}")
        console.print(f"  Branch/commit: {branch_commit}")
        console.print(f"  Target hosts: {len(entries)}")
        console.print()

        groups = group_by_role_and_credentials(entries)
        clear_known_hosts(all_ips, console)
        set_hostnames(entries, console)

        for (role, ssh_user, ssh_key), ips in groups.items():
            console.print(
                f"[bold]Provisioning {len(ips)} host(s) "
                f"(role={role}, ssh_user={ssh_user})...[/bold]"
            )
            try:
                TelegrafProvisioner(
                    host_ips=ips,
                    host_role=role,
                    ssh_key_path=ssh_key,
                    ssh_user=ssh_user,
                    testnet_name=testnet_name,
                    elasticsearch_url=es_url,
                    elasticsearch_username=es_username,
                    elasticsearch_password=es_password,
                    branch_commit=branch_commit,
                    node_version=node_version,
                    from_beginning=args.from_beginning,
                    ant_log_dir=args.ant_log_dir,
                    console=console,
                    **_resolve_telegraf_tuning(args),
                ).execute()
            except Exception as e:
                console.print(
                    f"[bold red]Telegraf provisioning failed:[/bold red] {e}"
                )
                sys.exit(1)
    elif args.host_ips_file:
        # External hosts mode: read IPs from file
        try:
            all_ips = read_ips_from_file(args.host_ips_file)
        except (OSError, ValueError) as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)

        console.print(
            f"[bold]Using {len(all_ips)} host IP(s) from "
            f"{args.host_ips_file}...[/bold]"
        )

        es_url, es_username, es_password = _resolve_es_credentials(
            args, console
        )
        node_version, branch_commit = _resolve_version_info(args)
        testnet_name = args.name or ""

        console.print(f"  Elasticsearch URL: {es_url}")
        console.print(f"  Node version: {node_version}")
        console.print(f"  Branch/commit: {branch_commit}")
        console.print(f"  Target hosts: {len(all_ips)}")
        console.print(f"  SSH user: {args.ssh_user}")
        console.print()

        clear_known_hosts(all_ips, console)
        try:
            TelegrafProvisioner(
                host_ips=all_ips,
                host_role=host_role_override or "WAN_NODE",
                ssh_key_path=args.ssh_key_path,
                ssh_user=args.ssh_user,
                testnet_name=testnet_name,
                elasticsearch_url=es_url,
                elasticsearch_username=es_username,
                elasticsearch_password=es_password,
                branch_commit=branch_commit,
                node_version=node_version,
                from_beginning=args.from_beginning,
                ant_log_dir=args.ant_log_dir,
                console=console,
                **_resolve_telegraf_tuning(args),
            ).execute()
        except Exception as e:
            console.print(
                f"[bold red]Telegraf provisioning failed:[/bold red] {e}"
            )
            sys.exit(1)
    elif args.name:
        # Deployment state mode: load IPs from state
        console.print(
            f"[bold]Loading deployment state for '{args.name}'...[/bold]"
        )
        try:
            state = load_deployment_state(args.name)
        except RuntimeError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)

        bootstrap_nodes = state.get("bootstrap_nodes") or []
        bootstrap_ip = state.get("bootstrap_ip")
        if not bootstrap_nodes and not bootstrap_ip:
            console.print(
                "[bold red]Error:[/bold red] No bootstrap nodes or IP found in "
                "deployment state."
            )
            sys.exit(1)

        vm_ips = state.get("vm_ips")
        if not vm_ips:
            console.print(
                "[bold red]Error:[/bold red] No VM IPs found in "
                "deployment state."
            )
            sys.exit(1)

        es_url, es_username, es_password = _resolve_es_credentials(
            args, console
        )
        node_version, branch_commit = _resolve_version_info(args)

        console.print(f"  Elasticsearch URL: {es_url}")
        console.print(f"  Node version: {node_version}")
        console.print(f"  Branch/commit: {branch_commit}")
        console.print()

        common_kwargs = {
            "ssh_key_path": args.ssh_key_path,
            "ssh_user": args.ssh_user,
            "testnet_name": args.name,
            "elasticsearch_url": es_url,
            "elasticsearch_username": es_username,
            "elasticsearch_password": es_password,
            "branch_commit": branch_commit,
            "node_version": node_version,
            "from_beginning": args.from_beginning,
            "ant_log_dir": args.ant_log_dir,
            "console": console,
            **_resolve_telegraf_tuning(args),
        }

        # Provision bootstrap VMs (genesis + any secondary bootstraps)
        if bootstrap_nodes:
            genesis_ips = [
                n["ip"] for n in bootstrap_nodes
                if n.get("bootstrap_index") == 1 and n.get("ip")
            ]
            secondary_ips = [
                n["ip"] for n in bootstrap_nodes
                if n.get("bootstrap_index", 0) >= 2 and n.get("ip")
            ]
        else:
            genesis_ips = [bootstrap_ip] if bootstrap_ip else []
            secondary_ips = []

        for role, ips in [("GENESIS", genesis_ips), ("BOOTSTRAP", secondary_ips)]:
            if not ips:
                continue
            console.print(
                f"[bold]Provisioning Telegraf on {len(ips)} {role} "
                f"node(s): {', '.join(ips)}...[/bold]"
            )
            clear_known_hosts(ips, console)
            try:
                TelegrafProvisioner(
                    host_ips=ips,
                    host_role=role,
                    **common_kwargs,
                ).execute()
            except Exception as e:
                console.print(
                    f"[bold red]Telegraf provisioning failed on {role}:"
                    f"[/bold red] {e}"
                )
                sys.exit(1)

        # Provision regular VMs
        regular_ips = []
        for region_key in sorted(vm_ips.keys()):
            regular_ips.extend(vm_ips[region_key])

        if regular_ips:
            console.print(
                f"\n[bold]Provisioning Telegraf on {len(regular_ips)} "
                f"regular VM(s)...[/bold]"
            )
            clear_known_hosts(regular_ips, console)
            try:
                TelegrafProvisioner(
                    host_ips=regular_ips,
                    host_role="WAN_NODE",
                    **common_kwargs,
                ).execute()
            except Exception as e:
                console.print(
                    f"[bold red]Telegraf provisioning failed:[/bold red] {e}"
                )
                sys.exit(1)
    else:
        console.print(
            "[bold red]Error:[/bold red] "
            "Either --name, --host-ips-file, or --inventory-path "
            "must be provided."
        )
        sys.exit(1)

    console.print()
    console.print(
        "[bold green]Telegraf provisioned successfully on all hosts.[/bold green]"
    )
