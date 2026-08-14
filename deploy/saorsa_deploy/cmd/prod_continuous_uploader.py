"""Deploy and destroy the daily production write test (V2-837).

Stands up one uploader VM and one or more verifier VMs in other regions, plus a
paired InfluxDB. The uploader claims files from the production pool as it
uploads them; the verifiers read every address back and compare hashes.

Two things distinguish this from the other prod client commands:

* **Nothing is claimed at deploy time.** Every other pool consumer draws its
  files before any infrastructure exists, because it needs them all up front.
  This one is open-ended, so the VM claims per cycle instead. What happens here
  is only an availability *preflight* — enough to refuse a deploy against an
  empty pool without spending a claim.
* **The VMs need a database credential**, supplied through the environment
  variable the manifest names (see `prod pool client-role`).
"""

import json
import os
import sys
from types import SimpleNamespace

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all, disconnect_all
from pyinfra.api.operation import add_op
from pyinfra.api.operations import run_ops
from pyinfra.operations import server
from rich.console import Console
from saorsa_db.models import DeploymentStatus, PoolFileKind

from saorsa_deploy.binary_descriptor import build_binary_descriptor
from saorsa_deploy.cmd.prod_continuous_downloader import (
    _monitoring_has_influx,
    _monitoring_stack_exists,
    _resolve_ant_binary,
)
from saorsa_deploy.prod_bootstraps import (
    PROD_BOOTSTRAPS_URL,
    ProdBootstrapsError,
    fetch_prod_bootstraps,
)
from saorsa_deploy.provisioning.prod_continuous_uploader import (
    UPLOADER_SERVICE,
    VERIFIER_SERVICE,
    ProdContinuousUploaderProvisioner,
    ProdUploadVerifierProvisioner,
)
from saorsa_deploy.provisioning.saorsa_db_install import build_saorsa_db_wheel
from saorsa_deploy.registry import (
    get_file_pool_repository,
    record_deployment_created,
    record_event,
    record_status,
)
from saorsa_deploy.ssh import clear_known_hosts, wait_for_ssh
from saorsa_deploy.state import load_client_state, load_monitoring_state

MONITORING_REGION = "lon1"


def _client_stack_exists(name: str) -> bool:
    try:
        state = load_client_state(name)
        return bool(state.get("clients"))
    except RuntimeError:
        return False


def _resolve_pool_database_url(args, console: Console) -> str:
    """The scoped credential the VMs use, taken from the named env var.

    Never a manifest value and never persisted into deploy_args — the same rule
    the wallet key follows.
    """
    var = getattr(args, "pool_database_url_env", None) or "POOL_DATABASE_URL"
    url = os.environ.get(var, "").strip()
    if not url:
        console.print(
            f"[bold red]Error:[/bold red] {var} is not set. The uploader and "
            "verifier VMs claim pool files and record results directly in the "
            "registry, so they need a connection string.\n\n"
            "Create a least-privilege role and print its URL with:\n"
            "  [bold]saorsa-deploy prod pool client-role[/bold]\n\n"
            "No infrastructure was created."
        )
        sys.exit(1)
    return url


def _parse_pool_request(raw: str, console: Console) -> dict:
    """Accept `tiny:2,small:1` (CLI) or a JSON object (orchestrator)."""
    from saorsa_deploy.pool_selection import parse_request_token

    raw = (raw or "").strip()
    if not raw:
        console.print(
            "[bold red]Error:[/bold red] --pool is required, e.g. "
            "--pool tiny:2,small:1"
        )
        sys.exit(1)
    try:
        if raw.startswith("{"):
            return {k: int(v) for k, v in json.loads(raw).items()}
        return {
            size_class.value: count
            for size_class, count in parse_request_token(raw).items()
        }
    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]Error:[/bold red] invalid --pool: {exc}")
        sys.exit(1)


def _preflight_pool(args, request: dict, console: Console) -> None:
    """Refuse to build anything if the pool cannot serve even one cycle.

    Deliberately does *not* claim: this deployment claims per cycle, and a
    preflight claim would either be spent by the check or need releasing, both
    of which are worse than a read-only look. It also prints the runway, since
    the useful question about a permanent daily consumer is not "does it work
    today" but "how long before it stops".
    """

    repo = get_file_pool_repository()
    if repo is None:
        console.print(
            "[yellow]Warning:[/yellow] no DATABASE_URL configured, so pool "
            "availability could not be checked before deploying."
        )
        return

    stats = repo.pool_stats().get(PoolFileKind.URL.value, {}).get("by_class", {})
    shortfalls = []
    runways = []
    for class_name, per_cycle in request.items():
        if not per_cycle:
            continue
        available = stats.get(class_name, {}).get("available", 0)
        if available < per_cycle:
            shortfalls.append((class_name, per_cycle, available))
        else:
            runways.append((class_name, available // per_cycle, available))

    if shortfalls:
        console.print(
            "[bold red]Error:[/bold red] the url pool cannot serve even one "
            "cycle of this request:"
        )
        for class_name, wanted, available in shortfalls:
            console.print(
                f"  {class_name}: {wanted} requested per cycle, "
                f"{available} available"
            )
        console.print(
            "\nAdd more with [bold]saorsa-deploy prod pool add "
            "--from-listing internet-archive:<item>[/bold], or free a stale "
            "claim with [bold]prod pool claims --stale 7d[/bold].\n"
            "No infrastructure was created."
        )
        sys.exit(1)

    if runways:
        cycles = min(r[1] for r in runways)
        limiting = min(runways, key=lambda r: r[1])
        interval = getattr(args, "sleep_interval_secs", 86400) or 86400
        days = cycles * (interval / 86400)
        console.print(
            f"[bold]Pool runway:[/bold] {cycles} cycles "
            f"(~{days:.0f} days at this cadence), limited by "
            f"[bold]{limiting[0]}[/bold] ({limiting[2]} available)."
        )
        console.print(
            "[dim]The canary draws from the same url inventory, so this "
            "shortens as canaries run.[/dim]"
        )


def _ensure_monitoring(args, console: Console) -> None:
    from saorsa_deploy.cmd.monitoring_infra import cmd_monitoring_infra
    from saorsa_deploy.cmd.provision_monitoring import cmd_provision_monitoring

    retention_days = getattr(args, "influx_retention_days", 0) or 0
    if _monitoring_stack_exists(args.name):
        if _monitoring_has_influx(args.name):
            console.print(
                f"[dim]Monitoring stack '{args.name}' already exists — "
                "skipping.[/dim]"
            )
            return
        console.print(
            f"[yellow]Monitoring VM for '{args.name}' exists but has no "
            "InfluxDB creds — re-provisioning.[/yellow]"
        )
        cmd_provision_monitoring(
            SimpleNamespace(
                name=args.name,
                ssh_key_path=args.ssh_key_path,
                retention_days=retention_days,
            )
        )
        return

    provider = getattr(args, "monitoring_provider", "digitalocean")
    if provider == "digitalocean":
        region = getattr(args, "monitoring_region", None) or MONITORING_REGION
        droplet_size = getattr(args, "monitoring_droplet_size", None) or "s-2vcpu-4gb"
    else:
        region = getattr(args, "monitoring_region", None)
        droplet_size = getattr(args, "monitoring_droplet_size", None)

    console.print(f"[bold cyan]Step 1/5: creating monitoring VM ({provider})[/bold cyan]")
    cmd_monitoring_infra(
        SimpleNamespace(
            name=args.name,
            provider=provider,
            region=region,
            volume_size=getattr(args, "monitoring_volume_size", None) or 50,
            droplet_size=droplet_size,
            volume_type=getattr(args, "monitoring_volume_type", None),
            force=False,
            grafana=getattr(args, "grafana", False),
        )
    )
    console.print("[bold cyan]Step 2/5: provisioning InfluxDB[/bold cyan]")
    cmd_provision_monitoring(
        SimpleNamespace(
            name=args.name,
            ssh_key_path=args.ssh_key_path,
            retention_days=retention_days,
        )
    )


def _ensure_client_infra(args, console: Console) -> None:
    """Create the uploader VM and every verifier VM in one clients-infra call.

    The uploader is an `--uploader` spec and the verifiers are `--downloader`
    specs, which is exactly what they are at the infrastructure layer: the
    roles recorded in client state are what the provisioning step later keys
    on to decide which service each VM gets.
    """
    from saorsa_deploy.cmd.infra_clients import cmd_infra_clients

    if _client_stack_exists(args.name):
        console.print(
            f"[dim]Client stack '{args.name}' already exists — skipping.[/dim]"
        )
        return

    provider = getattr(args, "provider", None) or "digitalocean"
    cache_token = f"bootstrap_cache={str(getattr(args, 'bootstrap_cache', True)).lower()}"
    uploader_specs = [build_uploader_infra_spec(provider, args.spec, cache_token)]
    downloader_specs = [
        build_verifier_infra_spec(provider, verifier, cache_token)
        for verifier in (args.verifier or [])
    ]

    console.print(
        f"[bold cyan]Step 3/5: creating 1 uploader + "
        f"{len(downloader_specs)} verifier VM(s)[/bold cyan]"
    )
    cmd_infra_clients(
        SimpleNamespace(
            name=args.name,
            uploader=uploader_specs,
            downloader=downloader_specs,
            force=False,
        )
    )


def build_uploader_infra_spec(provider: str, spec: str, cache_token: str) -> str:
    """Turn `region[:vm_size]` into what `clients infra` expects of an uploader.

    The two client roles do NOT share a spec grammar. A downloader is
    `provider:region[:vm_size]`, but an uploader is
    `provider:region:file_size_mb[:vm_size]` — that third positional is a
    legacy field for the testnet uploader, which generated random files of that
    size. This kind uploads real pool files, so it passes 0, exactly as
    `prod uploaders` does.

    Pure, and tested, because the failure mode is a spec that only breaks after
    the monitoring VM has been created and is billing.
    """
    region, _, vm_size = spec.partition(":")
    parts = [provider, region, "0"]
    if vm_size:
        parts.append(vm_size)
    parts.append(cache_token)
    return ":".join(parts)


def build_verifier_infra_spec(
    default_provider: str, verifier: str, cache_token: str
) -> str:
    """Turn a `[provider:]region[:vm_size]` verifier into a downloader spec.

    The provider is optional per verifier and falls back to the uploader's, so
    `lon1:s-1vcpu-2gb` and `ovh:GRA9:b3-8` both work. Told apart by whether the
    first field names a known provider, rather than by counting fields — a
    two-field `ovh:GRA9` and a two-field `lon1:s-1vcpu-2gb` are otherwise
    indistinguishable, and guessing wrong builds the VM in the wrong cloud.
    """
    from saorsa_deploy.cmd.infra_clients import resolve_client_provider

    head, _, remainder = verifier.partition(":")
    try:
        resolve_client_provider(head)
        provider, rest = head, remainder
    except ValueError:
        provider, rest = default_provider, verifier
    if not rest:
        raise ValueError(
            f"verifier spec {verifier!r} names a provider but no region"
        )
    return f"{provider}:{rest}:{cache_token}"


def _load_influx(name: str, console: Console) -> dict:
    try:
        state = load_monitoring_state(name)
    except RuntimeError:
        console.print(
            "[yellow]No monitoring state found — metric emission disabled.[/yellow]"
        )
        return {}
    return {
        "influx_url": state.get("influxdb_url", "") or "",
        "influx_token": state.get("influxdb_token", "") or "",
        "influx_org": state.get("influxdb_org", "ant") or "ant",
        "influx_bucket": state.get("influxdb_bucket", "testnet-metrics")
        or "testnet-metrics",
    }


def _split_clients(name: str, console: Console) -> tuple[list, list]:
    try:
        client_state = load_client_state(name)
    except RuntimeError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)
    uploaders, verifiers = [], []
    for client in client_state.get("clients", []):
        entry = {
            "ip": client.get("ip"),
            "region": client.get("region", ""),
            "provider": client.get("provider", ""),
        }
        if client.get("role", "upload") == "upload":
            uploaders.append(entry)
        else:
            verifiers.append(entry)
    if not uploaders:
        console.print("[bold red]Error:[/bold red] no uploader VM in client state.")
        sys.exit(1)
    return uploaders, verifiers


def _provision(args, deployment_id: int, pool_request: dict, console: Console) -> None:
    console.print("[bold cyan]Step 4/5: provisioning services[/bold cyan]")
    uploaders, verifiers = _split_clients(args.name, console)

    console.print("[bold]Fetching production bootstrap peers...[/bold]")
    try:
        bootstrap_peers = fetch_prod_bootstraps()
    except ProdBootstrapsError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)
    console.print(f"  Using {len(bootstrap_peers)} production peers.")

    binary_url, binary_is_archive = _resolve_ant_binary(
        SimpleNamespace(
            name=args.name,
            branch_name=args.branch_name,
            repo_owner=args.repo_owner,
            ant_version=args.ant_version,
        ),
        console,
    )

    for host in uploaders + verifiers:
        clear_known_hosts(host["ip"])
        wait_for_ssh(host["ip"], args.ssh_key_path)

    # Built once and reused for both roles rather than per provisioner run.
    wheel_path = build_saorsa_db_wheel()
    console.print(f"  saorsa-db wheel: {wheel_path.name}")

    influx = _load_influx(args.name, console)
    shared = dict(
        deployment_id=deployment_id,
        deployment_name=args.name,
        pool_database_url=args.pool_database_url,
        bootstrap_peers=bootstrap_peers,
        ssh_key_path=args.ssh_key_path,
        binary_url=binary_url,
        binary_is_archive=binary_is_archive,
        log_level=getattr(args, "log_level", None) or "info",
        rust_log=getattr(args, "rust_log", None),
        bootstrap_cache=getattr(args, "bootstrap_cache", True),
        log_retention_days=getattr(args, "log_retention_days", 7),
        wheel_path=wheel_path,
        console=console,
        **influx,
    )

    ProdContinuousUploaderProvisioner(
        hosts=uploaders,
        pool_request=pool_request,
        wallet_secret_key=args.secret_key,
        sleep_interval_secs=getattr(args, "sleep_interval_secs", 86400),
        pool_min_size=getattr(args, "pool_min_size", None),
        pool_max_size=getattr(args, "pool_max_size", None),
        payment_type=getattr(args, "payment", None),
        readback_verify=getattr(args, "readback_verify", True),
        **shared,
    ).execute()

    if verifiers:
        ProdUploadVerifierProvisioner(
            hosts=verifiers,
            verify_interval_secs=getattr(args, "verify_interval_secs", 3600),
            **shared,
        ).execute()


def _systemctl(name: str, hosts: list, ssh_key_path: str, label: str, commands: list):
    if not hosts:
        return
    inventory = Inventory(
        ([(h["ip"], {"ssh_user": "root", "ssh_key": ssh_key_path}) for h in hosts], {})
    )
    state = State(inventory, Config(SUDO=False))
    connect_all(state)
    add_op(state, server.shell, name=label, commands=commands)
    run_ops(state)
    disconnect_all(state)


def _start(args, console: Console) -> None:
    console.print("[bold cyan]Step 5/5: starting services[/bold cyan]")
    uploaders, verifiers = _split_clients(args.name, console)
    _systemctl(
        args.name, uploaders, args.ssh_key_path,
        "Start the uploader service",
        [f"systemctl enable --now {UPLOADER_SERVICE}"],
    )
    _systemctl(
        args.name, verifiers, args.ssh_key_path,
        "Start the verifier services",
        [f"systemctl enable --now {VERIFIER_SERVICE}"],
    )


def cmd_prod_continuous_uploader_deploy(args) -> None:
    console = Console()
    pool_request = _parse_pool_request(getattr(args, "pool", ""), console)
    args.pool_database_url = _resolve_pool_database_url(args, console)

    secret_key = getattr(args, "secret_key", None) or os.environ.get(
        "ARBITRUM_ONE_SECRET_KEY", ""
    )
    if not secret_key:
        console.print(
            "[bold red]Error:[/bold red] no wallet key. Pass --secret-key or "
            "set ARBITRUM_ONE_SECRET_KEY — uploads spend real ETH on Arbitrum "
            "One.\n\nNo infrastructure was created."
        )
        sys.exit(1)
    args.secret_key = secret_key

    # Before anything is built or spent.
    _preflight_pool(args, pool_request, console)

    deploy_args = {
        "kind": "prod-continuous-uploader",
        "prod_bootstraps_url": PROD_BOOTSTRAPS_URL,
        "monitoring_name": args.name,
        "spec": args.spec,
        "verifiers": list(args.verifier or []),
        "pool_request": pool_request,
        "pool_min_size": getattr(args, "pool_min_size", None),
        "pool_max_size": getattr(args, "pool_max_size", None),
        "sleep_interval_secs": getattr(args, "sleep_interval_secs", 86400),
        "verify_interval_secs": getattr(args, "verify_interval_secs", 3600),
        "provider": getattr(args, "provider", None) or "digitalocean",
        "monitoring_provider": getattr(args, "monitoring_provider", None)
        or "digitalocean",
        "influx_retention_days": getattr(args, "influx_retention_days", 7),
        "log_retention_days": getattr(args, "log_retention_days", 7),
        "bootstrap_cache": getattr(args, "bootstrap_cache", True),
        "payment_type": getattr(args, "payment", None),
        "log_level": getattr(args, "log_level", None) or "info",
        "rust_log": getattr(args, "rust_log", None),
        "readback_verify": getattr(args, "readback_verify", True),
        "purpose": args.purpose,
        "ant_binary": build_binary_descriptor(
            args.branch_name, args.repo_owner, args.ant_version,
            debug=getattr(args, "debug", False),
        ),
        # Deliberately absent: the database URL and the wallet key.
    }

    deployment_id = record_deployment_created(args.name, deploy_args)
    if deployment_id is None:
        console.print(
            "[bold red]Error:[/bold red] could not register this deployment. "
            "The VMs record every upload and verification against a deployment "
            "row, so there is nothing for them to write to without it. Check "
            "DATABASE_URL.\n\nNo infrastructure was created."
        )
        sys.exit(1)
    record_event(args.name, "prod_continuous_uploader_deploy_started")

    try:
        _ensure_monitoring(args, console)
        _ensure_client_infra(args, console)
        _provision(args, deployment_id, pool_request, console)
        _start(args, console)
    except SystemExit:
        record_event(args.name, "prod_continuous_uploader_deploy_failed")
        record_status(args.name, DeploymentStatus.FAILED)
        raise
    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]Deploy failed:[/bold red] {exc}")
        record_event(
            args.name,
            "prod_continuous_uploader_deploy_failed",
            {"error": str(exc)},
        )
        record_status(args.name, DeploymentStatus.FAILED)
        raise

    record_event(args.name, "prod_continuous_uploader_deploy_completed")
    record_status(args.name, DeploymentStatus.RUNNING)

    console.print()
    console.print("[bold green]Daily write test is running.[/bold green]")
    console.print(
        f"  Uploads {pool_request} every "
        f"{getattr(args, 'sleep_interval_secs', 86400)}s; "
        f"{len(args.verifier or [])} verifier region(s) read every address back."
    )
    console.print(
        f"  Results: [bold]saorsa-deploy deployments print-recorded-results "
        f"--name {args.name}[/bold]"
    )


def cmd_prod_continuous_uploader_destroy(args) -> None:
    from saorsa_deploy.cmd.destroy_clients import cmd_destroy_clients
    from saorsa_deploy.cmd.destroy_monitoring import cmd_destroy_monitoring

    console = Console()
    record_event(args.name, "prod_continuous_uploader_destroy_started")
    record_status(args.name, DeploymentStatus.DESTROYING)

    # Stop the services first so nothing claims a file mid-teardown and leaves
    # it stranded as an open claim on a VM that is about to disappear.
    try:
        uploaders, verifiers = _split_clients(args.name, console)
        _systemctl(
            args.name, uploaders + verifiers, args.ssh_key_path,
            "Stop the write-test services",
            [
                f"systemctl stop {UPLOADER_SERVICE} || true",
                f"systemctl disable {UPLOADER_SERVICE} || true",
                f"systemctl stop {VERIFIER_SERVICE} || true",
                f"systemctl disable {VERIFIER_SERVICE} || true",
            ],
        )
    except (SystemExit, RuntimeError) as exc:
        console.print(
            f"[yellow]Could not stop services cleanly ({exc}) — continuing "
            "with teardown.[/yellow]"
        )

    _warn_about_open_claims(args.name, console)

    try:
        cmd_destroy_clients(SimpleNamespace(name=args.name, force=True))
        if not getattr(args, "keep_monitoring", False):
            cmd_destroy_monitoring(SimpleNamespace(name=args.name, force=True))
        else:
            console.print("[dim]Keeping the monitoring stack.[/dim]")
    except Exception as exc:  # noqa: BLE001
        record_event(
            args.name, "prod_continuous_uploader_destroy_failed",
            {"error": str(exc)},
        )
        record_status(args.name, DeploymentStatus.FAILED)
        raise

    record_event(args.name, "prod_continuous_uploader_destroy_completed")
    record_status(args.name, DeploymentStatus.DESTROYED)
    console.print("[bold green]Daily write test destroyed.[/bold green]")


def _warn_about_open_claims(name: str, console: Console) -> None:
    """Report any claim the uploader never resolved.

    A claim can be left open if the VM died between claiming and recording.
    Warned about rather than released automatically, because a claim that might
    correspond to a real upload must never be freed by a sweep — that is how a
    file gets uploaded twice.
    """
    repo = get_file_pool_repository()
    if repo is None:
        return
    try:
        from saorsa_db.models import ClaimStatus

        deployment_repo = None
        from saorsa_deploy.registry import get_repository

        deployment_repo = get_repository()
        if deployment_repo is None:
            return
        deployment = deployment_repo.get_active_by_name(name)
        if deployment is None:
            return
        open_claims = [
            claim
            for claim in repo.list_pool_claims(deployment_id=deployment.id)
            if claim.status is ClaimStatus.CLAIMED
        ]
        if open_claims:
            console.print(
                f"[yellow]Warning:[/yellow] {len(open_claims)} pool claim(s) "
                "are still open for this deployment — the uploader was "
                "probably interrupted mid-upload."
            )
            console.print(
                "  They are NOT released automatically: a claim that might "
                "correspond to a real upload must not be freed by a sweep."
            )
            console.print(
                f"  Review with [bold]prod pool claims --stale 1h[/bold], then "
                f"release deliberately: [bold]prod pool release "
                f"{' '.join(str(c.file_id) for c in open_claims[:5])}"
                f"{' …' if len(open_claims) > 5 else ''}[/bold]"
            )
    except Exception:  # noqa: BLE001 - teardown must not fail on a warning
        pass
