import sys

from rich.console import Console
from rich.panel import Panel

from saorsa_deploy.cmd.obs_provider import (
    default_elastic_size,
    default_region,
    require_ovh_service_name,
    resolve_provider,
    state_key_abbrev,
)
from saorsa_deploy.contabo import require_contabo_env
from saorsa_deploy.executor import execute_terraform_runs
from saorsa_deploy.paths import state_dir
from saorsa_deploy.registry import record_deployment_created
from saorsa_deploy.resources import get_resources_dir
from saorsa_deploy.state import (
    check_terraform_state_exists,
    delete_terraform_state,
    load_elastic_state,
    save_elastic_state,
    update_elastic_state,
)
from saorsa_deploy.terraform import TerraformRunConfig


def cmd_elastic_infra(args):
    """Create Elasticsearch VM infrastructure using Terraform."""
    console = Console()

    provider = resolve_provider(args)
    region = getattr(args, "region", None) or default_region(provider)
    volume_size = getattr(args, "volume_size", None) or 100
    droplet_size = getattr(args, "droplet_size", None) or default_elastic_size(provider)
    volume_type = getattr(args, "volume_type", None) or "high-speed"
    keep_on_destroy = bool(getattr(args, "keep_on_destroy", False))
    public_ingest = bool(getattr(args, "public_ingest", False))
    reserved_ip = getattr(args, "reserved_ip", None)
    standalone = bool(getattr(args, "standalone", False))

    if reserved_ip and not public_ingest:
        console.print(
            "[bold red]Error:[/bold red] --reserved-ip requires --public-ingest "
            "(a reserved IP is only attached for an internet-facing instance)."
        )
        sys.exit(1)
    if public_ingest and provider != "digitalocean":
        # The reserved-IP and firewall work is modelled in the DigitalOcean
        # module only. Proceeding elsewhere would create an instance with 9200
        # open to the world and no stable address — the opposite of what the
        # flag asks for.
        console.print(
            "[bold red]Error:[/bold red] --public-ingest is only supported on "
            f"provider 'digitalocean' (got '{provider}')."
        )
        sys.exit(1)

    resources_dir = get_resources_dir()
    tf_source = resources_dir / f"{provider}-elastic"
    abbrev = state_key_abbrev(provider)
    workspace_base = state_dir() / "workspaces"
    workspace_dir = workspace_base / f"{provider}-elastic-{args.name}-{region}"
    state_key = f"saorsa-deploy/{abbrev}-elastic-{args.name}-{region}.tfstate"

    # Idempotent reuse: if we already have an elastic VM recorded for this
    # deployment in the same region, skip the terraform apply and treat the
    # existing infrastructure as the result. Mirrors the behaviour of
    # `bootstraps infra` so re-running a deploy runbook after a mid-pipeline
    # failure doesn't trip on its own previously-provisioned VM.
    try:
        existing = load_elastic_state(args.name)
    except RuntimeError:
        existing = None

    if existing and existing.get("vm_ip"):
        existing_region = existing.get("region", region)
        if existing_region != region:
            console.print(
                f"[bold red]Error:[/bold red] Elasticsearch VM for '{args.name}' "
                f"already exists in region '{existing_region}', but the request "
                f"is for '{region}'. Run `elastic destroy --name {args.name} "
                f"--force` first if you want to recreate in a different region."
            )
            sys.exit(1)
        console.print(
            f"[bold]Reusing existing Elasticsearch VM for '{args.name}' "
            f"({existing['vm_ip']}, {existing_region}).[/bold]"
        )
        # The reuse branch returns before any state write, so the keep-on-destroy
        # intent has to be merged in here too — otherwise it is silently dropped
        # on every re-run and on a resumed `up`, and the deployment's teardown
        # would destroy a stack the manifest asked to keep. The manifest is the
        # desired state, so an omitted flag resets the stored value; report the
        # flip so it is never a silent behaviour change.
        if bool(existing.get("keep_on_destroy", False)) != keep_on_destroy:
            try:
                update_elastic_state(args.name, {"keep_on_destroy": keep_on_destroy})
                console.print(
                    "[dim]Recorded keep_on_destroy="
                    f"{str(keep_on_destroy).lower()}: `deployments down` will "
                    f"{'leave this stack standing' if keep_on_destroy else 'destroy it'}"
                    ".[/dim]"
                )
            except Exception as e:
                console.print(
                    f"[yellow]Warning: Failed to update Elasticsearch state: {e}[/yellow]"
                )
        return

    # Stale tfstate without matching deployment state == orphan from a prior
    # failed/destroyed run. Refuse and tell the user to clean it up explicitly
    # — `--force` is intentionally not offered here because deleting the
    # tfstate without destroying the cloud resources leaks droplets/volumes.
    if check_terraform_state_exists(state_key):
        if not getattr(args, "force", False):
            console.print("[bold red]Error:[/bold red] Stale Terraform state found:")
            console.print(f"  {state_key}")
            console.print("Use --force to delete and proceed.")
            sys.exit(1)
        console.print(f"[yellow]Warning: Deleting stale Terraform state: {state_key}[/yellow]")
        delete_terraform_state(state_key)

    if provider == "ovh":
        service_name = require_ovh_service_name(console)
        variables = {
            "name": args.name,
            "region": region,
            "service_name": service_name,
            "flavor_name": droplet_size,
            "attached_volume_size": str(volume_size),
            "volume_type": volume_type,
        }
    elif provider == "contabo":
        require_contabo_env(console)
        image_id = getattr(args, "contabo_image_id", None)
        if not image_id:
            console.print(
                "[bold red]Error:[/bold red] Contabo elastic requires "
                "--contabo-image-id (an Ubuntu image UUID; resolve via "
                "`cntb get images`)."
            )
            sys.exit(1)
        # Contabo is root-disk only (no volume). droplet_size carries a Contabo
        # product id here (from obs_provider defaults or --droplet-size).
        variables = {
            "name": args.name,
            "region": region,
            "product_id": droplet_size,
            "image_id": image_id,
            "ssh_public_key_path": getattr(
                args, "contabo_ssh_public_key_path", "~/.ssh/id_rsa.pub"
            ),
        }
    else:
        variables = {
            "name": args.name,
            "region": region,
            "volume_size": str(volume_size),
            "droplet_size": droplet_size,
        }
        if public_ingest:
            variables["public_ingest"] = "true"
            if reserved_ip:
                variables["reserved_ip"] = reserved_ip

    config = TerraformRunConfig(
        provider=provider,
        region=region,
        tf_source_dir=tf_source,
        workspace_dir=workspace_dir,
        state_key=state_key,
        variables=variables,
    )

    console.print(
        f"[bold]Creating Elasticsearch VM for '{args.name}' on "
        f"{provider}/{region} (size: {droplet_size}, volume: {volume_size}GB)..."
        f"[/bold]"
    )
    console.print()

    results = execute_terraform_runs([config])

    if not results[0].success:
        console.print("[bold red]Elasticsearch infrastructure creation failed.[/bold red]")
        console.print(results[0].stderr)
        sys.exit(1)

    elastic_ip = results[0].outputs.get("elastic_ip", "")
    console.print(f"[bold green]Elasticsearch VM created: {elastic_ip}[/bold green]")

    try:
        save_elastic_state(
            name=args.name,
            region=region,
            vm_ip=elastic_ip,
            provider=provider,
            keep_on_destroy=keep_on_destroy,
        )
        # Recorded separately because save_elastic_state has a fixed signature.
        # `elastic provision` reads public_ingest back from here, so the two
        # halves of a two-step deploy cannot disagree about the shape of the
        # instance.
        if public_ingest:
            update_elastic_state(
                args.name,
                {
                    "public_ingest": True,
                    "reserved_ip": results[0].outputs.get("elastic_reserved_ip")
                    or reserved_ip
                    or "",
                    "droplet_ip": results[0].outputs.get("elastic_droplet_ip", ""),
                },
            )
        console.print("[dim]Elasticsearch state saved to S3.[/dim]")
        if keep_on_destroy:
            console.print(
                "[dim]keep_on_destroy is set: `deployments down` will leave this "
                "stack standing. Clean it up with `elastic destroy --name "
                f"{args.name} --force`.[/dim]"
            )
    except Exception as e:
        console.print(f"[yellow]Warning: Failed to save Elasticsearch state: {e}[/yellow]")

    if standalone:
        record_deployment_created(
            args.name,
            {
                "kind": "elasticsearch",
                "provider": provider,
                "region": region,
                "droplet_size": droplet_size,
                "volume_size": volume_size,
                "public_ingest": public_ingest,
            },
        )

    console.print()
    if public_ingest:
        # The DNS record is the one manual step, and it has to happen between
        # infra and provision: the certificate is issued during provisioning and
        # cannot be until the name points here.
        console.print(
            Panel(
                f"  Point the DNS A record at this instance before provisioning:\n\n"
                f"    [bold]<your-hostname>  ->  {elastic_ip}[/bold]\n\n"
                "  In Cloudflare the record must be [bold]DNS only[/bold] (grey\n"
                "  cloud). A proxied record terminates TLS at Cloudflare, so the\n"
                "  certificate cannot be issued here.\n\n"
                "  This address is reserved: it survives a droplet rebuild, so\n"
                "  the record only ever needs setting once.",
                title="Next: DNS",
                border_style="yellow",
            )
        )
    console.print(
        f"[dim]Next step: saorsa-deploy elastic provision "
        f"--name {args.name} --ssh-key-path ~/.ssh/id_rsa"
        + (" --public-ingest --ingest-hostname <fqdn>" if public_ingest else "")
        + "[/dim]"
    )
