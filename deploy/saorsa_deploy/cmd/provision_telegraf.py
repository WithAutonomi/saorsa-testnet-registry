import sys

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


def cmd_provision_telegraf(args):
    """Provision Telegraf for Elasticsearch log forwarding on all VMs."""
    console = Console()

    if args.host_ips_file:
        # External hosts mode: read IPs from file, no deployment state needed
        try:
            all_ips = read_ips_from_file(args.host_ips_file)
        except (OSError, ValueError) as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)

        console.print(
            f"[bold]Using {len(all_ips)} host IP(s) from "
            f"{args.host_ips_file}...[/bold]"
        )

        # Resolve Elasticsearch credentials: explicit args required,
        # or loaded from elastic state if --name is also provided
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
                    "[bold red]Error:[/bold red] No Elasticsearch credentials provided "
                    "and no elastic state found for '--name'.\n"
                    "Provide --elasticsearch-url, --elasticsearch-username, "
                    "and --elasticsearch-password."
                )
                sys.exit(1)

        if not es_url or not es_username or not es_password:
            console.print(
                "[bold red]Error:[/bold red] Elasticsearch URL, username, and password "
                "are all required. Provide them via CLI args, or also pass --name "
                "to load from elastic state."
            )
            sys.exit(1)

        if args.node_version:
            node_version = args.node_version
        elif args.branch_commit:
            node_version = f"{args.repo_owner}/{args.branch_commit}"
        else:
            node_version = "unknown"

        branch_commit = args.branch_commit or "unknown"
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
                host_role="WAN_NODE",
                ssh_key_path=args.ssh_key_path,
                ssh_user=args.ssh_user,
                testnet_name=testnet_name,
                elasticsearch_url=es_url,
                elasticsearch_username=es_username,
                elasticsearch_password=es_password,
                branch_commit=branch_commit,
                node_version=node_version,
                console=console,
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

        bootstrap_ip = state.get("bootstrap_ip")
        if not bootstrap_ip:
            console.print(
                "[bold red]Error:[/bold red] No bootstrap IP found in "
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

        # Resolve Elasticsearch credentials
        es_url = args.elasticsearch_url
        es_username = args.elasticsearch_username
        es_password = args.elasticsearch_password

        if not es_url:
            console.print(
                "[dim]Loading Elasticsearch credentials from elastic "
                "state...[/dim]"
            )
            try:
                elastic_state = load_elastic_state(args.name)
                es_url = elastic_state.get("elasticsearch_url", "")
                es_username = elastic_state.get("elasticsearch_username", "")
                es_password = elastic_state.get("elasticsearch_password", "")
            except RuntimeError:
                console.print(
                    "[bold red]Error:[/bold red] No Elasticsearch credentials "
                    "provided and no elastic state found for this deployment.\n"
                    "Either run elastic-infra + provision-elastic first, "
                    "or provide --elasticsearch-url, --elasticsearch-username, "
                    "and --elasticsearch-password."
                )
                sys.exit(1)

        if not es_url or not es_username or not es_password:
            console.print(
                "[bold red]Error:[/bold red] Elasticsearch URL, username, and "
                "password are all required. Provide them via CLI args or "
                "elastic state."
            )
            sys.exit(1)

        if args.node_version:
            node_version = args.node_version
        elif args.branch_commit:
            node_version = f"{args.repo_owner}/{args.branch_commit}"
        else:
            node_version = "unknown"

        branch_commit = args.branch_commit or "unknown"

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
            "console": console,
        }

        # Provision genesis VM
        console.print(
            f"[bold]Provisioning Telegraf on genesis node "
            f"({bootstrap_ip})...[/bold]"
        )
        clear_known_hosts([bootstrap_ip], console)
        try:
            TelegrafProvisioner(
                host_ips=[bootstrap_ip],
                host_role="GENESIS",
                **common_kwargs,
            ).execute()
        except Exception as e:
            console.print(
                f"[bold red]Telegraf provisioning failed on genesis:"
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
            "Either --name or --host-ips-file must be provided."
        )
        sys.exit(1)

    console.print()
    console.print(
        "[bold green]Telegraf provisioned successfully on all hosts.[/bold green]"
    )
