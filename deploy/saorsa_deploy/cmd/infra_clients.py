import os
import sys
from pathlib import Path

from rich.console import Console

from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.providers import CLIENT_PROVIDERS, resolve_client_provider
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import (
    check_terraform_state_exists,
    delete_terraform_state,
    load_client_state,
    save_client_state,
    update_client_state,
)
from saorsa_deploy.terraform import TerraformRunConfig

DEFAULT_VM_SIZE = "s-2vcpu-4gb"
ROLE_UPLOAD = "upload"
ROLE_DOWNLOAD = "download"


_VALID_PAYMENT_VALUES = ("merkle", "no-merkle")
_VALID_UPLOADER_KEYS = ("payment", "ant", "eth", "clock_skew")
_VALID_DOWNLOADER_KEYS = ("clock_skew",)
DEFAULT_ANT_FUNDING = "100"
DEFAULT_ETH_FUNDING = "0.5"


def _parse_clock_skew(spec: str, value: str) -> bool:
    """Validate and parse a ``clock_skew=`` spec option into a bool."""
    lowered = value.strip().lower()
    if lowered in ("true", "1", "yes"):
        return True
    if lowered in ("false", "0", "no"):
        return False
    raise ValueError(
        f"Invalid spec '{spec}': clock_skew must be true or false, got '{value}'."
    )


def _parse_positive_decimal(spec: str, key: str, value: str) -> str:
    """Validate a numeric uploader-spec option and return it as a normalized string."""
    from decimal import Decimal, InvalidOperation

    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError(
            f"Invalid uploader spec '{spec}': '{key}' must be a number, got '{value}'."
        )
    if amount <= 0:
        raise ValueError(
            f"Invalid uploader spec '{spec}': '{key}' must be > 0, got '{value}'."
        )
    if amount == amount.to_integral_value():
        return str(int(amount))
    return format(amount, "f").rstrip("0").rstrip(".") or "0"


def parse_uploader_spec(spec: str) -> dict:
    """Parse an uploader spec.

    Format:
    ``provider:region:file_size_mb[:vm_size][:payment=MODE][:ant=N][:eth=N][:clock_skew=true]``

    ``provider`` is mandatory and must be one of digital_ocean, vultr,
    hetzner, ovh. Region names and VM sizes vary per provider and are passed
    through verbatim — the user is responsible for supplying valid strings.

    Returns a dict with keys: role=upload, provider (canonical internal name),
    region, file_size_mb, vm_size, payment_type (None unless ``payment=merkle``
    or ``payment=no-merkle``), ant_funding (str numeric, default "100"),
    eth_funding (str numeric, default "0.5").
    """
    raw_parts = spec.split(":")
    positional: list[str] = []
    options: dict[str, str] = {}
    for part in raw_parts:
        if "=" in part:
            key, value = part.split("=", 1)
            if key not in _VALID_UPLOADER_KEYS:
                raise ValueError(
                    f"Invalid uploader spec '{spec}': unknown key '{key}'."
                )
            if key in options:
                raise ValueError(
                    f"Invalid uploader spec '{spec}': duplicate key '{key}'."
                )
            options[key] = value
        else:
            positional.append(part)

    if len(positional) < 3 or len(positional) > 4:
        raise ValueError(
            f"Invalid uploader spec '{spec}'. "
            f"Expected format: provider:region:file_size_mb[:vm_size]"
            f"[:payment=MODE][:ant=N][:eth=N]"
        )
    try:
        provider = resolve_client_provider(positional[0]).name
    except ValueError as e:
        raise ValueError(f"Invalid uploader spec '{spec}': {e}")
    region = positional[1]
    if not region:
        raise ValueError(f"Invalid uploader spec '{spec}': region cannot be empty.")
    try:
        file_size_mb = int(positional[2])
    except ValueError:
        raise ValueError(
            f"Invalid file size '{positional[2]}' in uploader spec '{spec}'. "
            f"Must be an integer (MB)."
        )
    vm_size = positional[3] if len(positional) == 4 else DEFAULT_VM_SIZE

    payment_type: str | None = options.get("payment")
    if payment_type is not None and payment_type not in _VALID_PAYMENT_VALUES:
        raise ValueError(
            f"Invalid uploader spec '{spec}': payment must be one of "
            f"{', '.join(_VALID_PAYMENT_VALUES)}."
        )

    ant_funding = (
        _parse_positive_decimal(spec, "ant", options["ant"])
        if "ant" in options
        else DEFAULT_ANT_FUNDING
    )
    eth_funding = (
        _parse_positive_decimal(spec, "eth", options["eth"])
        if "eth" in options
        else DEFAULT_ETH_FUNDING
    )
    clock_skew = (
        _parse_clock_skew(spec, options["clock_skew"])
        if "clock_skew" in options
        else False
    )

    return {
        "role": ROLE_UPLOAD,
        "provider": provider,
        "region": region,
        "file_size_mb": file_size_mb,
        "vm_size": vm_size,
        "payment_type": payment_type,
        "ant_funding": ant_funding,
        "eth_funding": eth_funding,
        "clock_skew": clock_skew,
    }


def parse_downloader_spec(spec: str) -> dict:
    """Parse a downloader spec: 'provider:region[:vm_size][:clock_skew=true]'.

    ``provider`` is mandatory (digital_ocean, vultr, hetzner, ovh). Returns a
    dict with keys: role=download, provider (canonical internal name), region,
    vm_size, clock_skew (bool, default False).
    """
    raw_parts = spec.split(":")
    positional: list[str] = []
    options: dict[str, str] = {}
    for part in raw_parts:
        if "=" in part:
            key, value = part.split("=", 1)
            if key not in _VALID_DOWNLOADER_KEYS:
                raise ValueError(
                    f"Invalid downloader spec '{spec}': unknown key '{key}'."
                )
            if key in options:
                raise ValueError(
                    f"Invalid downloader spec '{spec}': duplicate key '{key}'."
                )
            options[key] = value
        else:
            positional.append(part)

    if len(positional) < 2 or len(positional) > 3:
        raise ValueError(
            f"Invalid downloader spec '{spec}'. "
            f"Expected format: provider:region[:vm_size][:clock_skew=true]"
        )
    try:
        provider = resolve_client_provider(positional[0]).name
    except ValueError as e:
        raise ValueError(f"Invalid downloader spec '{spec}': {e}")
    region = positional[1]
    if not region:
        raise ValueError(f"Invalid downloader spec '{spec}': region cannot be empty.")
    vm_size = positional[2] if len(positional) == 3 else DEFAULT_VM_SIZE
    clock_skew = (
        _parse_clock_skew(spec, options["clock_skew"])
        if "clock_skew" in options
        else False
    )
    return {
        "role": ROLE_DOWNLOAD,
        "provider": provider,
        "region": region,
        "vm_size": vm_size,
        "clock_skew": clock_skew,
    }


def _spec_signature(spec: dict) -> tuple:
    """Identity tuple for matching requested vs. recorded client specs."""
    return (
        spec.get("role"),
        spec.get("provider"),
        spec.get("region"),
        spec.get("client_index"),
        spec.get("vm_size"),
        spec.get("file_size_mb"),
        spec.get("payment_type"),
        spec.get("ant_funding"),
        spec.get("eth_funding"),
        bool(spec.get("clock_skew")),
    )


def _client_specs_match(existing: list[dict], requested: list[dict]) -> bool:
    if len(existing) != len(requested):
        return False
    return sorted(_spec_signature(s) for s in existing) == sorted(
        _spec_signature(s) for s in requested
    )


def cmd_infra_clients(args):
    """Execute the clients infra command: create client VMs via Terraform."""
    console = Console()

    uploader_specs_raw = args.uploader or []
    downloader_specs_raw = args.downloader or []

    if not uploader_specs_raw and not downloader_specs_raw:
        console.print(
            "[bold red]Error:[/bold red] At least one --uploader or --downloader "
            "spec is required."
        )
        sys.exit(1)

    client_specs: list[dict] = []
    try:
        for i, spec_str in enumerate(uploader_specs_raw, start=1):
            spec = parse_uploader_spec(spec_str)
            spec["client_index"] = i
            client_specs.append(spec)
        for i, spec_str in enumerate(downloader_specs_raw, start=1):
            spec = parse_downloader_spec(spec_str)
            spec["client_index"] = i
            client_specs.append(spec)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    console.print(f"[bold]Creating {len(client_specs)} client VM(s) for {args.name}...[/bold]")
    for spec in client_specs:
        skew_str = ", clock-skew" if spec.get("clock_skew") else ""
        if spec["role"] == ROLE_UPLOAD:
            payment = spec.get("payment_type")
            payment_str = f", payment={payment}" if payment else ""
            ant = spec.get("ant_funding", DEFAULT_ANT_FUNDING)
            eth = spec.get("eth_funding", DEFAULT_ETH_FUNDING)
            console.print(
                f"  Uploader #{spec['client_index']}: {spec['provider']}:"
                f"{spec['region']} "
                f"({spec['vm_size']}, {spec['file_size_mb']}MB uploads"
                f"{payment_str}, fund {ant} ANT + {eth} ETH{skew_str})"
            )
        else:
            console.print(
                f"  Downloader #{spec['client_index']}: {spec['provider']}:"
                f"{spec['region']} ({spec['vm_size']}{skew_str})"
            )
    console.print()

    # Idempotent reuse: if our saorsa-side client state already records every
    # VM in this request (with a populated IP), skip the terraform apply.
    # Mirrors `bootstraps infra` so re-running a deploy runbook after a
    # mid-pipeline failure doesn't trip on its own previously-provisioned VMs.
    try:
        existing_state = load_client_state(args.name)
    except RuntimeError:
        existing_state = None

    if existing_state and existing_state.get("clients"):
        existing_clients = existing_state["clients"]
        if _client_specs_match(existing_clients, client_specs) and all(
            c.get("ip") for c in existing_clients
        ):
            console.print(
                f"[bold]Reusing {len(existing_clients)} existing client VM(s) "
                f"for '{args.name}'.[/bold]"
            )
            for spec in existing_clients:
                label = "Uploader" if spec["role"] == ROLE_UPLOAD else "Downloader"
                skew_str = ", clock-skew" if spec.get("clock_skew") else ""
                console.print(
                    f"  {label} #{spec['client_index']}: {spec.get('ip', 'N/A')} "
                    f"({spec.get('provider', 'digitalocean')}:{spec['region']}, "
                    f"{spec['vm_size']}{skew_str})"
                )
            return
        console.print(
            f"[bold red]Error:[/bold red] Client state for '{args.name}' "
            f"already exists but does not match the requested spec. "
            f"Run `clients destroy --name {args.name} --force` first if you "
            f"want to recreate with a different layout."
        )
        sys.exit(1)

    # OVH targets a specific Public Cloud project; the manifest needs its UUID
    # as a Terraform variable. Mirror the migration command's env-var sourcing.
    ovh_service_name = None
    if any(spec["provider"] == "ovh" for spec in client_specs):
        ovh_service_name = os.environ.get("OVH_CLOUD_PROJECT_SERVICE")
        if not ovh_service_name:
            console.print(
                "[bold red]Error:[/bold red] One or more OVH clients were "
                "requested but OVH_CLOUD_PROJECT_SERVICE is not set. Export it "
                "(the OVH Public Cloud project ID) so the OVH manifest can "
                "target the right project."
            )
            sys.exit(1)

    resources_dir = get_resources_dir()
    workspace_base = Path.cwd() / ".saorsa" / "workspaces"

    configs = []
    for spec in client_specs:
        info = CLIENT_PROVIDERS[spec["provider"]]
        tf_source = resources_dir / info.tf_dir
        workspace_dir = (
            workspace_base
            / f"{info.name}-client-{args.name}-{spec['role']}"
            f"-{spec['region']}-{spec['client_index']}"
        )
        state_key = (
            f"{info.state_key_prefix}-{args.name}-{spec['role']}"
            f"-{spec['region']}-{spec['client_index']}.tfstate"
        )
        variables = {
            "name": args.name,
            "role": spec["role"],
            "region": spec["region"],
            "client_index": str(spec["client_index"]),
            info.size_var: spec["vm_size"],
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

    # Check for stale Terraform state files
    stale_keys = [c.state_key for c in configs if check_terraform_state_exists(c.state_key)]
    if stale_keys:
        if not getattr(args, "force", False):
            console.print("[bold red]Error:[/bold red] Stale Terraform state found:")
            for key in stale_keys:
                console.print(f"  {key}")
            console.print("Use --force to delete and proceed.")
            sys.exit(1)
        for key in stale_keys:
            console.print(f"[yellow]Warning: Deleting stale Terraform state: {key}[/yellow]")
            delete_terraform_state(key)

    # Save client state UPFRONT (with empty IPs) so that clients destroy can
    # always tear down whatever terraform may have started, even on partial
    # or total failure. IPs get merged in after the apply completes.
    upfront_specs = [{**spec, "ip": ""} for spec in client_specs]
    try:
        save_client_state(args.name, upfront_specs)
    except Exception as e:
        console.print(
            f"[bold red]Failed to save initial client state:[/bold red] {e}"
        )
        console.print(
            "[bold red]Aborting — without saved state, clients destroy "
            "couldn't clean up a partial apply.[/bold red]"
        )
        sys.exit(1)

    results = execute_terraform_runs(configs)

    # Merge whichever IPs came back (partial success included).
    for spec, result in zip(client_specs, results):
        if result.success and result.outputs.get("vm_ip"):
            spec["ip"] = result.outputs["vm_ip"]
        else:
            spec["ip"] = ""

    try:
        update_client_state(args.name, {"clients": client_specs})
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to update client state: {e}[/yellow]")

    failures = [r for r in results if not r.success]
    if failures:
        console.print(f"[bold red]{len(failures)} client VM(s) failed.[/bold red]")
        sys.exit(1)

    console.print()
    console.print(
        f"[bold green]All {len(results)} client VM(s) provisioned successfully.[/bold green]"
    )
    for spec in client_specs:
        label = "Uploader" if spec["role"] == ROLE_UPLOAD else "Downloader"
        console.print(
            f"  {label} #{spec['client_index']}: {spec.get('ip', 'N/A')} "
            f"({spec['region']}, {spec['vm_size']})"
        )
