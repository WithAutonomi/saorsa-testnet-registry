import sys

from rich.console import Console

from saorsa_deploy.binary_descriptor import build_binary_descriptor
from saorsa_deploy.client_roles import is_wasm
from saorsa_deploy.evm import ARBITRUM_SEPOLIA
from saorsa_deploy.prod_bootstraps import ProdBootstrapsError, fetch_prod_bootstraps
from saorsa_deploy.provisioning.client import AntClientProvisioner
from saorsa_deploy.provisioning.network_profile import (
    NetworkProfileError,
    resolve_network,
)
from saorsa_deploy.registry import record_deploy_args_update
from saorsa_deploy.ssh import clear_known_hosts
from saorsa_deploy.state import (
    S3_ADDRESSES_BUCKET,
    load_client_state,
    load_deployment_state,
    update_client_state,
    update_client_wallet,
)
from saorsa_deploy.wallet import (
    address_for_secret_key,
    create_wallet,
    get_eth_balance,
    get_token_balance,
    get_token_decimals,
    get_web3,
    to_token_units,
    transfer_eth,
    transfer_token,
)


def _resolve_influx_config(args, console):
    """Resolve InfluxDB configuration from args or monitoring state.

    Three-tier resolution:
    1. Explicit --influx-url and --influx-token args
    2. --monitoring-name (load from monitoring state in S3)
    3. No InfluxDB (returns empty strings)
    """
    if args.influx_url and args.influx_token:
        console.print(f"  InfluxDB: {args.influx_url} (explicit)")
        return (
            args.influx_url,
            args.influx_token,
            getattr(args, "influx_org", "ant"),
            getattr(args, "influx_bucket", "testnet-metrics"),
        )

    monitoring_name = getattr(args, "monitoring_name", None)
    if monitoring_name:
        try:
            from saorsa_deploy.state import load_monitoring_state

            mon_state = load_monitoring_state(monitoring_name)
            influx_url = mon_state.get("influxdb_url", "")
            influx_token = mon_state.get("influxdb_token", "")
            if influx_url and influx_token:
                console.print(
                    f"  InfluxDB: {influx_url} (from monitoring state '{monitoring_name}')"
                )
                return (
                    influx_url,
                    influx_token,
                    mon_state.get("influxdb_org", "ant"),
                    mon_state.get("influxdb_bucket", "testnet-metrics"),
                )
            else:
                console.print(
                    f"[yellow]Warning: Monitoring state '{monitoring_name}' "
                    f"has no InfluxDB config. Proceeding without metrics.[/yellow]"
                )
        except RuntimeError as e:
            console.print(f"[yellow]Warning: {e} Proceeding without metrics.[/yellow]")

    console.print("  InfluxDB: disabled (no --influx-url/--influx-token or --monitoring-name)")
    return ("", "", "ant", "testnet-metrics")


def _resolve_elastic_config(clients, name, console):
    """Resolve Elasticsearch creds for client log forwarding.

    Returns ``(url, username, password)``. Empty strings when no client opted
    into ``forward_logs``. When at least one did, the deployment's own elastic
    state must exist (testnet log-forwarding ES) — exits if it doesn't.
    """
    wants = [c for c in clients if c.get("forward_logs")]
    if not wants:
        return "", "", ""
    from saorsa_deploy.state import load_elastic_state

    try:
        es_state = load_elastic_state(name)
    except RuntimeError:
        console.print(
            "[bold red]Error:[/bold red] forward_logs is set on one or more "
            f"clients but no Elasticsearch instance exists for '{name}'. "
            "Enable elastic (elastic.enabled: true) so client logs have an "
            "index to forward to."
        )
        sys.exit(1)
    url = es_state.get("elasticsearch_url", "")
    username = es_state.get("elasticsearch_username", "")
    password = es_state.get("elasticsearch_password", "")
    if not (url and username and password):
        console.print(
            "[bold red]Error:[/bold red] Elasticsearch state for "
            f"'{name}' is missing connection details — run elastic provision."
        )
        sys.exit(1)
    console.print(
        f"  Client log forwarding: {url} (index telegraf-ant-clients-"
        f"{name.lower()}-*)"
    )
    return url, username, password


def _resolve_ant_binary(args, console):
    """Resolve the ant binary source. Returns (url, is_archive) or (None, True)."""
    if args.branch_name:
        repo_owner = getattr(args, "repo_owner", None) or "maidsafe"
        from saorsa_deploy.binary_source import (
            check_ant_custom_build_exists,
            get_ant_custom_build_url,
        )

        if not check_ant_custom_build_exists(args.name, repo_owner, args.branch_name):
            console.print(
                f"[bold red]Error:[/bold red] No custom ant build found for "
                f"{repo_owner}/{args.branch_name}. "
                f"Run 'build ant' first."
            )
            sys.exit(1)
        url = get_ant_custom_build_url(args.name, repo_owner, args.branch_name)
        console.print(f"  Ant binary: custom build ({repo_owner}/{args.branch_name})")
        return url, False

    if args.ant_version:
        from saorsa_deploy.binary_source import check_ant_release_exists, get_ant_release_url

        if not check_ant_release_exists(args.ant_version):
            console.print(
                f"[bold red]Error:[/bold red] Ant release v{args.ant_version} not found."
            )
            sys.exit(1)
        url = get_ant_release_url(args.ant_version)
        console.print(f"  Ant binary: release v{args.ant_version}")
        return url, True

    console.print("  Ant binary: latest release")
    return None, True


def _resolve_client_aws_creds(console):
    """Resolve AWS creds that client VMs use for the per-testnet S3 addresses folder.

    Reads ANT_CLIENT_AWS_ACCESS_KEY_ID and ANT_CLIENT_AWS_SECRET_ACCESS_KEY from
    the environment. These should come from the aws-client-s3 Terraform module
    outputs. Exits if missing.
    """
    import os

    access_key = os.environ.get("ANT_CLIENT_AWS_ACCESS_KEY_ID", "")
    secret_key = os.environ.get("ANT_CLIENT_AWS_SECRET_ACCESS_KEY", "")
    if not access_key or not secret_key:
        console.print(
            "[bold red]Error:[/bold red] Client AWS credentials missing. Set "
            "ANT_CLIENT_AWS_ACCESS_KEY_ID and ANT_CLIENT_AWS_SECRET_ACCESS_KEY "
            "in your environment (see .env.example)."
        )
        console.print(
            "[dim]Create the IAM user with: "
            "terraform -chdir=deploy/saorsa_deploy/resources/aws-client-s3 apply"
            "[/dim]"
        )
        sys.exit(1)
    return access_key, secret_key


def _resolve_wallet_secret_key(args, console):
    """Resolve the wallet secret key from args or environment.

    Priority: --wallet-secret-key arg > ANT_DEPLOY_SECRET_KEY env var.
    Exits if neither is provided.
    """
    key = getattr(args, "wallet_secret_key", None)
    if key:
        console.print("  Wallet key: provided via --wallet-secret-key")
        return key

    import os

    key = os.environ.get("ANT_DEPLOY_SECRET_KEY")
    if key:
        console.print("  Wallet key: from ANT_DEPLOY_SECRET_KEY env var")
        return key

    console.print(
        "[bold red]Error:[/bold red] No wallet secret key provided. "
        "Use --wallet-secret-key or set ANT_DEPLOY_SECRET_KEY."
    )
    sys.exit(1)


def _resolve_addresses_file_content(args, console) -> str | None:
    """Load the local addresses file into a string, or return None if not provided.

    Exits with an error if the path is given but unreadable / empty.
    """
    path = getattr(args, "addresses_file", None)
    if not path:
        return None
    try:
        content = open(path).read()
    except OSError as e:
        console.print(
            f"[bold red]Error:[/bold red] Cannot read --addresses-file {path}: {e}"
        )
        sys.exit(1)
    stripped = content.strip()
    if not stripped:
        console.print(
            f"[bold red]Error:[/bold red] --addresses-file {path} is empty."
        )
        sys.exit(1)
    line_count = sum(1 for line in stripped.splitlines() if line.strip())
    console.print(f"  Addresses file: {path} ({line_count} address(es))")
    return stripped + "\n"


_ADDRESS_LEN = 64


def parse_addresses(content: str | None) -> list[str]:
    """Parse the textual addresses-file payload into a list of addresses.

    Blank lines and ``#`` comment lines are dropped. Every other line must
    be a single whitespace-separated token that looks like an Autonomi
    address (64 hex chars). Raises :class:`ValueError` if any line fails —
    this catches the common mistake of pasting a full ``ant file download
    <addr> -o <name>`` command into the file instead of bare addresses.
    """
    if not content:
        return []
    out: list[str] = []
    bad: list[tuple[int, str, str]] = []
    for lineno, raw in enumerate(content.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens = stripped.split()
        if len(tokens) != 1:
            bad.append((lineno, raw, f"expected 1 token, got {len(tokens)}"))
            continue
        token = tokens[0]
        if len(token) != _ADDRESS_LEN or any(
            c not in "0123456789abcdefABCDEF" for c in token
        ):
            bad.append(
                (lineno, raw, f"not a {_ADDRESS_LEN}-char hex address")
            )
            continue
        out.append(token.lower())
    if bad:
        sample = "\n".join(
            f"  line {ln}: {reason} — {raw!r}" for ln, raw, reason in bad[:5]
        )
        more = f"\n  ... and {len(bad) - 5} more" if len(bad) > 5 else ""
        raise ValueError(
            f"Addresses file has {len(bad)} invalid line(s):\n{sample}{more}"
        )
    return out


def _ensure_uploader_wallets(
    name: str, clients: list[dict], funding_secret_key: str, console
) -> list[dict]:
    """Make sure every uploader has a per-VM Sepolia wallet at its target
    funding level, creating wallets and topping up balances as needed.

    Two phases, both idempotent so the command can be re-run safely after
    transient RPC errors mid-funding:

    1. **Wallet creation**: any uploader without ``wallet_secret_key`` gets a
       fresh EOA, persisted to S3 client state immediately (before funding).
       This ensures funds aren't lost if a transfer fails after an address is
       generated.
    2. **Top-up**: every uploader is funded to its target ANT/ETH amounts.
       The on-chain balance is queried first; only the deficit is transferred.
       Re-runs skip wallets that are already at target.
    """
    from decimal import Decimal

    from web3 import Web3

    uploaders = [
        c for c in clients if c.get("role", "upload") == "upload"
    ]
    if not uploaders:
        return clients

    funding_address = address_for_secret_key(funding_secret_key)
    rpc_url = ARBITRUM_SEPOLIA["rpc_url"]
    token_address = ARBITRUM_SEPOLIA["payment_token_address"]
    w3 = get_web3(rpc_url)
    decimals = get_token_decimals(w3, token_address)

    # Phase 1: ensure every uploader has a wallet recorded in state.
    needs_wallet = [c for c in uploaders if not c.get("wallet_secret_key")]
    if needs_wallet:
        console.print(
            f"  Per-uploader wallets: creating {len(needs_wallet)} new "
            f"wallet(s)"
        )
        for client in needs_wallet:
            wallet_obj = create_wallet()
            console.print(
                f"    Uploader #{client['client_index']} ({client['region']}): "
                f"{wallet_obj.address}"
            )
            update_client_wallet(
                name=name,
                client_index=client["client_index"],
                wallet_address=wallet_obj.address,
                wallet_secret_key=wallet_obj.secret_key,
            )
            client["wallet_address"] = wallet_obj.address
            client["wallet_secret_key"] = wallet_obj.secret_key

    # Phase 2: top each wallet up to its target ANT/ETH.
    console.print(
        f"  Per-uploader wallets: topping up {len(uploaders)} wallet(s) "
        f"from {funding_address} on Arbitrum Sepolia"
    )
    for client in uploaders:
        addr = client["wallet_address"]
        target_ant_units = to_token_units(
            client.get("ant_funding", "100"), decimals
        )
        target_eth_wei = Web3.to_wei(
            Decimal(str(client.get("eth_funding", "0.5"))), "ether"
        )
        cur_ant = get_token_balance(w3, token_address, addr)
        cur_eth = get_eth_balance(w3, addr)
        deficit_ant = max(target_ant_units - cur_ant, 0)
        deficit_eth = max(target_eth_wei - cur_eth, 0)

        if deficit_ant == 0 and deficit_eth == 0:
            console.print(
                f"    Uploader #{client['client_index']} ({addr}): "
                f"already funded, skipping"
            )
            continue

        console.print(
            f"    Uploader #{client['client_index']} ({addr}): "
            f"sending {deficit_ant} ANT-units + {deficit_eth} wei ETH"
        )
        # ETH first so the new wallet has gas. Skip whichever leg already has
        # enough on chain — re-runs after a partial-failure resume cleanly.
        if deficit_eth > 0:
            tx = transfer_eth(w3, funding_secret_key, addr, deficit_eth)
            console.print(f"      eth_tx={tx}")
        if deficit_ant > 0:
            tx = transfer_token(
                w3, token_address, funding_secret_key, addr, deficit_ant
            )
            console.print(f"      ant_tx={tx}")

    return clients


def _resolve_wasm_bundle(args, console) -> str:
    """S3 URL of the WASM client bundle for this deployment.

    Branch-build only: no ant-core WASM package is published as a release, so
    unlike the native `ant` binary there is no version to fall back to.
    """
    from saorsa_deploy.binary_source import (
        check_ant_wasm_build_exists,
        get_ant_wasm_build_url,
    )

    branch = getattr(args, "wasm_branch_name", None) or getattr(args, "branch_name", None)
    owner = getattr(args, "wasm_repo_owner", None) or getattr(args, "repo_owner", None)
    if not branch or not owner:
        console.print(
            "[bold red]Error:[/bold red] WASM clients need --wasm-branch-name and "
            "--wasm-repo-owner (or --branch-name/--repo-owner) so the bundle "
            "built by `build ant-wasm` can be located."
        )
        sys.exit(1)
    if not check_ant_wasm_build_exists(args.name, owner, branch):
        console.print(
            f"[bold red]Error:[/bold red] no WASM client bundle found for "
            f"'{args.name}' ({owner}/{branch}). Run `saorsa-deploy build "
            f"ant-wasm --name {args.name} --branch-name {branch} "
            f"--repo-owner {owner}` first."
        )
        sys.exit(1)
    url = get_ant_wasm_build_url(args.name, owner, branch)
    console.print(f"[cyan]WASM bundle: {url}[/cyan]")
    return url


def cmd_provision_clients(args):
    """Execute the clients provision command: provision client VMs with uploader/downloader."""
    console = Console()
    prod_mode = getattr(args, "prod", False)

    console.print(f"[bold]Loading client state for '{args.name}'...[/bold]")
    try:
        client_state = load_client_state(args.name)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    clients = client_state.get("clients", [])
    if not clients:
        console.print("[bold red]Error:[/bold red] No clients found in client state.")
        sys.exit(1)

    # Record the SSH key these VMs are being provisioned with so that
    # `clients destroy` can reach them later (to stop the uploader service
    # before draining its wallet) without the operator having to remember and
    # re-pass the exact same --ssh-key-path. Best-effort: a failure here only
    # costs the convenience, not the provisioning run.
    try:
        update_client_state(args.name, {"ssh_key_path": args.ssh_key_path})
    except Exception as e:  # noqa: BLE001 — recording is best-effort
        console.print(
            f"[yellow]Warning: failed to record ssh_key_path in client "
            f"state: {e}[/yellow]"
        )

    has_uploaders = any(c.get("role", "upload") == "upload" for c in clients)

    if prod_mode and has_uploaders:
        console.print(
            "[bold red]Error:[/bold red] --prod mode does not support upload-role clients. "
            "Re-run clients infra with --downloader specs only."
        )
        sys.exit(1)

    if has_uploaders:
        wallet_secret_key = _resolve_wallet_secret_key(args, console)
        aws_access_key_id, aws_secret_access_key = _resolve_client_aws_creds(console)
    else:
        wallet_secret_key = ""
        aws_access_key_id, aws_secret_access_key = "", ""
        console.print("  Wallet key: skipped (download-only deployment)")
        console.print("  Client AWS creds: skipped (download-only deployment)")

    # Per-uploader wallet creation and funding. Testnet only — prod uses a
    # real funded Arbitrum One wallet supplied by the operator and skips
    # synthesis entirely.
    if has_uploaders and not prod_mode:
        try:
            clients = _ensure_uploader_wallets(
                args.name, clients, wallet_secret_key, console
            )
        except Exception as e:
            console.print(
                f"[bold red]Error:[/bold red] Failed to create or fund "
                f"per-uploader wallets: {e}"
            )
            sys.exit(1)
    addresses_file_content = _resolve_addresses_file_content(args, console)

    if prod_mode:
        console.print("[bold]Fetching production bootstrap peers...[/bold]")
        try:
            bootstrap_peers = fetch_prod_bootstraps()
        except ProdBootstrapsError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)
        console.print(f"  Using {len(bootstrap_peers)} production peers.")
    else:
        console.print(f"[bold]Loading deployment state for '{args.name}'...[/bold]")
        try:
            deploy_state = load_deployment_state(args.name)
        except RuntimeError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)

        bootstrap_nodes = deploy_state.get("bootstrap_nodes", [])
        bootstrap_port = deploy_state.get("bootstrap_port")
        if not bootstrap_port:
            console.print(
                "[bold red]Error:[/bold red] No bootstrap port found in deployment state. "
                "Has the nodes provision-genesis command been run?"
            )
            sys.exit(1)

        bootstrap_peers = []
        for node in bootstrap_nodes:
            port = node.get("port", bootstrap_port)
            bootstrap_peers.append(f"{node['ip']}:{port}")

        if not bootstrap_peers:
            bootstrap_ip = deploy_state.get("bootstrap_ip")
            if not bootstrap_ip:
                console.print(
                    "[bold red]Error:[/bold red] No bootstrap IP found in deployment state."
                )
                sys.exit(1)
            bootstrap_peers = [f"{bootstrap_ip}:{bootstrap_port}"]

    binary_url, binary_is_archive = _resolve_ant_binary(args, console)
    heaptrack_targets = frozenset(getattr(args, "heaptrack", None) or [])
    if heaptrack_targets:
        console.print(
            f"  Heaptrack: enabled for {', '.join(sorted(heaptrack_targets))} "
            f"(profiles → /var/log/ant-heaptrack/ on each VM)"
        )
    record_deploy_args_update(
        args.name,
        {
            "ant_binary": build_binary_descriptor(
                getattr(args, "branch_name", None),
                getattr(args, "repo_owner", None),
                getattr(args, "ant_version", None),
            ),
            "heaptrack": sorted(heaptrack_targets),
        },
    )
    influx_url, influx_token, influx_org, influx_bucket = _resolve_influx_config(args, console)
    # Client log forwarding (testnet path): reuse the deployment's own elastic
    # instance, different index. prod_mode flows forward via their own commands.
    if prod_mode:
        es_url, es_username, es_password = "", "", ""
    else:
        es_url, es_username, es_password = _resolve_elastic_config(
            clients, args.name, console
        )

    # The client state stores `network` as its unresolved token (a profile name or
    # an inline set), exactly as clock_skew stores its duration string. Resolve it
    # once here so the provisioner gets a NetworkProfile and an unknown profile
    # fails before any SSH session is opened.
    try:
        for c in clients:
            c["network"] = resolve_network(c.get("network"))
    except NetworkProfileError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    host_ips = [c["ip"] for c in clients if c.get("ip")]
    console.print(f"  Bootstrap peers: {', '.join(bootstrap_peers)}")
    console.print(f"  Client VMs: {len(host_ips)}")
    for c in clients:
        role = c.get("role", "upload")
        if role == "upload":
            console.print(
                f"    Uploader #{c['client_index']}: {c.get('ip', 'N/A')} "
                f"({c['region']}, {c.get('file_size_mb', 0)}MB)"
            )
        else:
            console.print(
                f"    Downloader #{c['client_index']}: {c.get('ip', 'N/A')} "
                f"({c['region']})"
            )
    console.print()

    clear_known_hosts(host_ips, console)

    # --- WASM (browser-path) clients ------------------------------------
    # Resolved only when the deployment actually has one, so a native-only
    # deployment neither reads the state key nor needs a bundle.
    wasm_bundle_url = None
    bootstrap_webrtc_multiaddrs: list[str] = []
    if any(is_wasm(c.get("role", "upload")) for c in clients):
        wasm_bundle_url = _resolve_wasm_bundle(args, console)
        bootstrap_webrtc_multiaddrs = deploy_state.get("bootstrap_webrtc_multiaddrs") or []
        if not bootstrap_webrtc_multiaddrs:
            console.print(
                "[bold red]Error:[/bold red] this deployment has WASM clients but "
                "no bootstrap WebRTC-Direct addresses in its state. Run "
                f"`saorsa-deploy bootstraps webrtc-addresses --name {args.name}` "
                "after the bootstrap tier is up."
            )
            sys.exit(1)
        console.print(
            f"[cyan]WASM clients seed from {len(bootstrap_webrtc_multiaddrs)} "
            f"bootstrap address(es).[/cyan]"
        )

    provisioner = AntClientProvisioner(
        clients=clients,
        bootstrap_peers=bootstrap_peers,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        addresses_s3_bucket=S3_ADDRESSES_BUCKET,
        addresses_s3_prefix=f"saorsa-deploy/addresses/{args.name}",
        ssh_key_path=args.ssh_key_path,
        console=console,
        binary_url=binary_url,
        binary_is_archive=binary_is_archive,
        influx_url=influx_url,
        influx_token=influx_token,
        influx_org=influx_org,
        influx_bucket=influx_bucket,
        wallet_secret_key=wallet_secret_key,
        addresses_file_content=addresses_file_content,
        heaptrack_targets=heaptrack_targets,
        deployment_name=args.name,
        elasticsearch_url=es_url,
        elasticsearch_username=es_username,
        elasticsearch_password=es_password,
        forward_buffer_limit=getattr(args, "forward_buffer_limit", None),
        wasm_bundle_url=wasm_bundle_url,
        bootstrap_webrtc_multiaddrs=bootstrap_webrtc_multiaddrs,
        wasm_rpc_url=ARBITRUM_SEPOLIA["rpc_url"],
    )

    try:
        provisioner.execute()
        # Install the client→Elasticsearch log forwarder on opted-in VMs (no-op
        # when no client set forward_logs). Runs after the service env exists.
        provisioner.provision_log_forwarders()
        console.print()
        console.print("[bold green]All clients provisioned successfully.[/bold green]")
    except Exception as e:
        console.print(f"[bold red]Client provisioning failed:[/bold red] {e}")
        sys.exit(1)

    # Install client host-metrics Telegraf (InfluxDB, host_role=CLIENT) as part
    # of this clients pass, so it no longer needs a separate provision-telegraf
    # step. Best-effort: a monitoring hiccup must not fail the client deploy.
    monitoring_name = getattr(args, "monitoring_name", None)
    if monitoring_name:
        from saorsa_deploy.cmd.provision_telegraf_metrics import (
            provision_client_telegraf_best_effort,
        )

        provision_client_telegraf_best_effort(
            monitoring_name,
            args.ssh_key_path,
            getattr(args, "ssh_user", "root"),
            console,
        )
