"""Contabo infrastructure via the REST API (not Terraform).

Creates/adopts Contabo instances directly (see ``contabo_api``) and writes the
SAME S3 state the Terraform path would have — deployment ``vm_ips``/``vm_ids`` for
nodes, monitoring/elastic state for the observability boxes — so provisioning,
inventory and destroy are unchanged. Instances are matched/adopted by the same
display-name convention the retired Terraform modules used, so an instance left
behind by a timed-out ``terraform apply`` is adopted rather than duplicated.

Bookkeeping for teardown (Contabo has no tfstate) is stashed alongside the state:
``contabo_node`` on the deployment state; ``contabo_instance_id`` /
``contabo_secret_id`` on the monitoring/elastic state.
"""

from rich.console import Console

from saorsa_deploy.cmd.obs_provider import default_elastic_size, default_influxdb_size
from saorsa_deploy.contabo import require_contabo_env
from saorsa_deploy.contabo_api import ContaboClient
from saorsa_deploy.state import (
    delete_deployment_state,
    delete_elastic_state,
    delete_monitoring_state,
    load_deployment_state,
    load_elastic_state,
    load_monitoring_state,
    save_elastic_state,
    save_monitoring_state,
    update_deployment_state,
    update_elastic_state,
    update_monitoring_state,
)

_DEFAULT_NODE_PRODUCT = "V95"  # VPS 20 SSD — sensible fallback when --vm-size unset


def _create_or_adopt(
    client: ContaboClient,
    console: Console,
    *,
    display_name: str,
    product_id: str,
    image_id: str,
    region: str,
    ssh_secret_id: int,
) -> tuple[int, str]:
    """Return (instance_id, ipv4), adopting an existing same-named instance."""
    inst = client.find_instance(display_name)
    if inst is not None:
        instance_id = int(inst["instanceId"])
        console.print(
            f"[dim]Adopting existing Contabo instance {instance_id} ({display_name}).[/dim]"
        )
    else:
        instance_id = client.create_instance(
            product_id=product_id,
            image_id=image_id,
            region=region,
            ssh_secret_id=ssh_secret_id,
            display_name=display_name,
        )
        console.print(
            f"[cyan]Created Contabo instance {instance_id} ({display_name}, "
            f"{product_id}/{region}) — awaiting provisioning (slow order)...[/cyan]"
        )
    ip = client.wait_until_running(instance_id)
    console.print(f"[green]Instance {instance_id} running at {ip}.[/green]")
    return instance_id, ip


# --------------------------------------------------------------------------- #
# infra (create / adopt)
# --------------------------------------------------------------------------- #
def ensure_contabo_node_infra(args, console: Console) -> None:
    """Create/adopt the Contabo node VM(s) and record deployment state."""
    try:
        state = load_deployment_state(args.name)
    except RuntimeError:
        state = {}
    if state.get("vm_ips"):
        console.print(f"[dim]Node VMs for '{args.name}' already recorded — skipping.[/dim]")
        return

    require_contabo_env(console)
    client = ContaboClient(console)
    region = args.region
    product = args.vm_size or _DEFAULT_NODE_PRODUCT
    secret_id = client.ensure_ssh_secret(
        f"{args.name.lower()}-{region.lower()}-node",
        args.contabo_ssh_public_key_path,
    )

    ips: list[str] = []
    ids: list[str] = []
    for i in range(args.vm_count):
        instance_id, ip = _create_or_adopt(
            client,
            console,
            display_name=f"{args.name}-ant-node-{region}-{i + 1}",
            product_id=product,
            image_id=args.contabo_image_id,
            region=region,
            ssh_secret_id=secret_id,
        )
        ips.append(ip)
        ids.append(str(instance_id))

    region_key = f"contabo/{region}"
    update_deployment_state(
        args.name,
        {
            "regions": [["contabo", region]],
            "vm_ips": {region_key: ips},
            "vm_ids": {region_key: ids},
            "contabo_node": {
                "region": region,
                "secret_id": secret_id,
                "instance_ids": ids,
            },
        },
    )


def ensure_contabo_monitoring_infra(args, console: Console) -> None:
    """Create/adopt the Contabo InfluxDB VM and record monitoring state."""
    require_contabo_env(console)
    client = ContaboClient(console)
    region = args.monitoring_region or "EU"
    product = args.monitoring_droplet_size or default_influxdb_size("contabo")
    secret_id = client.ensure_ssh_secret(
        f"{args.name.lower()}-{region.lower()}-monitoring",
        args.contabo_ssh_public_key_path,
    )
    instance_id, ip = _create_or_adopt(
        client,
        console,
        display_name=f"{args.name}-ant-monitoring-{region}",
        product_id=product,
        image_id=args.contabo_image_id,
        region=region,
        ssh_secret_id=secret_id,
    )
    # Grafana is off for this kind — InfluxDB only.
    save_monitoring_state(
        name=args.name,
        region=region,
        vm_ip=ip,
        grafana_ip="",
        provider="contabo",
        grafana_enabled=False,
    )
    update_monitoring_state(
        args.name,
        {"contabo_instance_id": str(instance_id), "contabo_secret_id": secret_id},
    )


def ensure_contabo_elastic_infra(args, console: Console) -> None:
    """Create/adopt the Contabo Elasticsearch VM and record elastic state."""
    require_contabo_env(console)
    client = ContaboClient(console)
    region = args.elastic_region or "EU"
    product = args.elastic_droplet_size or default_elastic_size("contabo")
    secret_id = client.ensure_ssh_secret(
        f"{args.name.lower()}-{region.lower()}-elastic",
        args.contabo_ssh_public_key_path,
    )
    instance_id, ip = _create_or_adopt(
        client,
        console,
        display_name=f"{args.name}-ant-elastic-{region}",
        product_id=product,
        image_id=args.contabo_image_id,
        region=region,
        ssh_secret_id=secret_id,
    )
    save_elastic_state(name=args.name, region=region, vm_ip=ip, provider="contabo")
    update_elastic_state(
        args.name,
        {"contabo_instance_id": str(instance_id), "contabo_secret_id": secret_id},
    )


# --------------------------------------------------------------------------- #
# destroy (cancel via API) — each returns True if it handled a Contabo stack
# --------------------------------------------------------------------------- #
def destroy_contabo_nodes(name: str, console: Console) -> bool:
    try:
        state = load_deployment_state(name)
    except RuntimeError:
        return False
    cn = state.get("contabo_node")
    if not cn:
        return False
    client = ContaboClient(console)
    for iid in cn.get("instance_ids", []):
        console.print(f"[cyan]Cancelling Contabo node instance {iid}...[/cyan]")
        client.cancel_instance(int(iid))
    if cn.get("secret_id"):
        client.delete_secret(int(cn["secret_id"]))
    try:
        delete_deployment_state(name)
    except Exception as e:
        console.print(f"[yellow]Warning: failed to delete deployment state: {e}[/yellow]")
    return True


def _destroy_contabo_obs(name: str, console: Console, *, load, delete, label: str) -> bool:
    try:
        state = load(name)
    except RuntimeError:
        return False
    if state.get("provider") != "contabo":
        return False
    client = ContaboClient(console)
    iid = state.get("contabo_instance_id")
    if iid:
        console.print(f"[cyan]Cancelling Contabo {label} instance {iid}...[/cyan]")
        client.cancel_instance(int(iid))
    sid = state.get("contabo_secret_id")
    if sid:
        client.delete_secret(int(sid))
    try:
        delete(name)
    except Exception as e:
        console.print(f"[yellow]Warning: failed to delete {label} state: {e}[/yellow]")
    return True


def destroy_contabo_monitoring(name: str, console: Console) -> bool:
    return _destroy_contabo_obs(
        name,
        console,
        load=load_monitoring_state,
        delete=delete_monitoring_state,
        label="monitoring",
    )


def destroy_contabo_elastic(name: str, console: Console) -> bool:
    return _destroy_contabo_obs(
        name,
        console,
        load=load_elastic_state,
        delete=delete_elastic_state,
        label="elastic",
    )
