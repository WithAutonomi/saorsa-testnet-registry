import json

import boto3

S3_BUCKET = "maidsafe-org-infra-tfstate"
S3_REGION = "eu-west-2"
S3_KEY_PREFIX = "saorsa-deploy/deployments"

# Client address lists live in their own PUBLIC-READ bucket (they contain only
# testnet content addresses) so downloaders can fetch them with unsigned HTTP —
# SigV4 rejects requests from clock-skewed client VMs. Deliberately NOT the
# tfstate bucket above: that one must keep Block Public Access intact.
S3_ADDRESSES_BUCKET = "saorsa-testnet-addresses"


def _get_s3_client():
    return boto3.client("s3", region_name=S3_REGION)


def save_deployment_state(
    name: str,
    regions: list[tuple[str, str]],
    terraform_variables: dict[str, str],
    bootstrap_nodes: list[dict],
    vm_ips: dict[str, list[str]],
    vm_ids: dict[str, list[str]] | None = None,
) -> None:
    """Save deployment metadata to S3 for later use by other commands.

    Args:
        bootstrap_nodes: List of dicts with keys: ip, region, bootstrap_index.
            The first entry (index=1) is the genesis node.
        vm_ids: Optional dict keyed by "<provider>/<region>" mapping to a list of
            provider-specific instance IDs, in the same order as `vm_ips`.
    """
    state = {
        "name": name,
        "regions": [[provider, region] for provider, region in regions],
        "terraform_variables": terraform_variables,
        "bootstrap_nodes": bootstrap_nodes,
        "bootstrap_ip": bootstrap_nodes[0]["ip"] if bootstrap_nodes else "",
        "vm_ips": vm_ips,
        "vm_ids": vm_ids or {},
    }
    client = _get_s3_client()
    client.put_object(
        Bucket=S3_BUCKET,
        Key=f"{S3_KEY_PREFIX}/{name}.json",
        Body=json.dumps(state, indent=2),
        ContentType="application/json",
    )


def update_deployment_state(name: str, updates: dict) -> None:
    """Merge updates into existing deployment state in S3."""
    state = load_deployment_state(name)
    state.update(updates)
    client = _get_s3_client()
    client.put_object(
        Bucket=S3_BUCKET,
        Key=f"{S3_KEY_PREFIX}/{name}.json",
        Body=json.dumps(state, indent=2),
        ContentType="application/json",
    )


def load_deployment_state(name: str) -> dict:
    """Load deployment metadata from S3.

    Returns a dict with keys: name, regions, terraform_variables.
    Raises RuntimeError if the deployment state is not found.
    """
    client = _get_s3_client()
    try:
        resp = client.get_object(
            Bucket=S3_BUCKET,
            Key=f"{S3_KEY_PREFIX}/{name}.json",
        )
        return json.loads(resp["Body"].read())
    except client.exceptions.NoSuchKey:
        raise RuntimeError(
            f"No deployment state found for '{name}'. "
            "Was this deployment created with the infra command?"
        )


def delete_deployment_state(name: str) -> None:
    """Delete deployment metadata from S3 after a successful destroy."""
    client = _get_s3_client()
    client.delete_object(
        Bucket=S3_BUCKET,
        Key=f"{S3_KEY_PREFIX}/{name}.json",
    )


def delete_terraform_state(state_key: str) -> None:
    """Delete a Terraform state file from S3 after successful destroy."""
    client = _get_s3_client()
    client.delete_object(Bucket=S3_BUCKET, Key=state_key)


def load_terraform_state(state_key: str) -> dict:
    """Download a Terraform state file from S3 and return it parsed.

    Raises RuntimeError if the state file is missing.
    """
    client = _get_s3_client()
    try:
        resp = client.get_object(Bucket=S3_BUCKET, Key=state_key)
        return json.loads(resp["Body"].read())
    except client.exceptions.NoSuchKey:
        raise RuntimeError(f"No Terraform state at {state_key}")


def terraform_state_attributes(
    tfstate: dict, resource_type: str, resource_name: str
) -> dict | None:
    """Return the attributes dict for a single Terraform resource instance.

    Looks up ``resource_type.resource_name`` in the state. Returns ``None``
    when the resource isn't present or has no instances.
    """
    for resource in tfstate.get("resources", []):
        if resource.get("type") == resource_type and resource.get("name") == resource_name:
            instances = resource.get("instances") or []
            if instances:
                return instances[0].get("attributes", {}) or {}
    return None


S3_MONITORING_KEY_PREFIX = "saorsa-deploy/monitoring"


def save_monitoring_state(
    name: str,
    region: str,
    vm_ip: str,
    grafana_ip: str = "",
    provider: str = "digitalocean",
    influxdb_url: str = "",
    influxdb_token: str = "",
    influxdb_org: str = "",
    influxdb_bucket: str = "",
    grafana_url: str = "",
    grafana_enabled: bool = True,
) -> None:
    """Save monitoring stack metadata to S3.

    ``vm_ip`` is the InfluxDB VM; ``grafana_ip`` is the separate Grafana VM
    (empty for legacy single-VM stacks). ``provider`` records which cloud the
    stack runs on so destroy can select the right Terraform module.
    ``grafana_enabled`` records whether the optional Grafana VM was created, so
    provision/destroy can tell a deliberately Grafana-less stack apart from a
    legacy single-VM stack (which has no ``grafana_enabled`` key and defaults
    to True → co-locate Grafana on the InfluxDB VM).
    """
    state = {
        "name": name,
        "region": region,
        "provider": provider,
        "vm_ip": vm_ip,
        "grafana_ip": grafana_ip,
        "grafana_enabled": grafana_enabled,
        "influxdb_url": influxdb_url,
        "influxdb_token": influxdb_token,
        "influxdb_org": influxdb_org,
        "influxdb_bucket": influxdb_bucket,
        "grafana_url": grafana_url,
    }
    client = _get_s3_client()
    client.put_object(
        Bucket=S3_BUCKET,
        Key=f"{S3_MONITORING_KEY_PREFIX}/{name}.json",
        Body=json.dumps(state, indent=2),
        ContentType="application/json",
    )


def update_monitoring_state(name: str, updates: dict) -> None:
    """Merge updates into existing monitoring state in S3."""
    state = load_monitoring_state(name)
    state.update(updates)
    client = _get_s3_client()
    client.put_object(
        Bucket=S3_BUCKET,
        Key=f"{S3_MONITORING_KEY_PREFIX}/{name}.json",
        Body=json.dumps(state, indent=2),
        ContentType="application/json",
    )


def load_monitoring_state(name: str) -> dict:
    """Load monitoring stack metadata from S3.

    Raises RuntimeError if the monitoring state is not found.
    """
    client = _get_s3_client()
    try:
        resp = client.get_object(
            Bucket=S3_BUCKET,
            Key=f"{S3_MONITORING_KEY_PREFIX}/{name}.json",
        )
        return json.loads(resp["Body"].read())
    except client.exceptions.NoSuchKey:
        raise RuntimeError(
            f"No monitoring state found for '{name}'. "
            "Was this monitoring stack created with the monitoring infra command?"
        )


def delete_monitoring_state(name: str) -> None:
    """Delete monitoring stack metadata from S3."""
    client = _get_s3_client()
    client.delete_object(
        Bucket=S3_BUCKET,
        Key=f"{S3_MONITORING_KEY_PREFIX}/{name}.json",
    )


S3_ELASTIC_KEY_PREFIX = "saorsa-deploy/elastic"


def save_elastic_state(
    name: str,
    region: str,
    vm_ip: str,
    provider: str = "digitalocean",
    elasticsearch_url: str = "",
    elasticsearch_username: str = "",
    elasticsearch_password: str = "",
    elasticsearch_api_key: str = "",
    elasticsearch_index_pattern: str = "",
) -> None:
    """Save Elasticsearch stack metadata to S3.

    ``provider`` records which cloud the stack runs on so destroy can select
    the right Terraform module.
    """
    state = {
        "name": name,
        "region": region,
        "provider": provider,
        "vm_ip": vm_ip,
        "elasticsearch_url": elasticsearch_url,
        "elasticsearch_username": elasticsearch_username,
        "elasticsearch_password": elasticsearch_password,
        "elasticsearch_api_key": elasticsearch_api_key,
        "elasticsearch_index_pattern": elasticsearch_index_pattern,
    }
    client = _get_s3_client()
    client.put_object(
        Bucket=S3_BUCKET,
        Key=f"{S3_ELASTIC_KEY_PREFIX}/{name}.json",
        Body=json.dumps(state, indent=2),
        ContentType="application/json",
    )


def update_elastic_state(name: str, updates: dict) -> None:
    """Merge updates into existing Elasticsearch state in S3."""
    state = load_elastic_state(name)
    state.update(updates)
    client = _get_s3_client()
    client.put_object(
        Bucket=S3_BUCKET,
        Key=f"{S3_ELASTIC_KEY_PREFIX}/{name}.json",
        Body=json.dumps(state, indent=2),
        ContentType="application/json",
    )


def load_elastic_state(name: str) -> dict:
    """Load Elasticsearch stack metadata from S3.

    Raises RuntimeError if the Elasticsearch state is not found.
    """
    client = _get_s3_client()
    try:
        resp = client.get_object(
            Bucket=S3_BUCKET,
            Key=f"{S3_ELASTIC_KEY_PREFIX}/{name}.json",
        )
        return json.loads(resp["Body"].read())
    except client.exceptions.NoSuchKey:
        raise RuntimeError(
            f"No Elasticsearch state found for '{name}'. "
            "Was this Elasticsearch stack created with the elastic infra command?"
        )


def delete_elastic_state(name: str) -> None:
    """Delete Elasticsearch stack metadata from S3."""
    client = _get_s3_client()
    client.delete_object(
        Bucket=S3_BUCKET,
        Key=f"{S3_ELASTIC_KEY_PREFIX}/{name}.json",
    )


S3_CLIENT_KEY_PREFIX = "saorsa-deploy/clients"


def save_client_state(name: str, clients: list[dict]) -> None:
    """Save client deployment metadata to S3.

    Args:
        name: Deployment name.
        clients: List of client specs, each a dict with keys:
            role ('upload' or 'download'), region, vm_size, ip, client_index.
            Uploader specs additionally carry file_size_mb, payment_type,
            ant_funding, eth_funding, and (after provision) wallet_address +
            wallet_secret_key for the per-uploader wallet.
    """
    state = {
        "name": name,
        "clients": clients,
    }
    client = _get_s3_client()
    client.put_object(
        Bucket=S3_BUCKET,
        Key=f"{S3_CLIENT_KEY_PREFIX}/{name}.json",
        Body=json.dumps(state, indent=2),
        ContentType="application/json",
    )


def update_client_state(name: str, updates: dict) -> None:
    """Merge updates into existing client state in S3."""
    state = load_client_state(name)
    state.update(updates)
    client = _get_s3_client()
    client.put_object(
        Bucket=S3_BUCKET,
        Key=f"{S3_CLIENT_KEY_PREFIX}/{name}.json",
        Body=json.dumps(state, indent=2),
        ContentType="application/json",
    )


def update_client_wallet(
    name: str, client_index: int, wallet_address: str, wallet_secret_key: str
) -> None:
    """Persist a generated per-uploader wallet onto its entry in client state.

    Looks up the uploader by ``client_index`` (only uploaders carry wallets).
    Raises RuntimeError if the deployment or matching uploader entry is missing.
    """
    state = load_client_state(name)
    clients = state.get("clients", [])
    matched = False
    for entry in clients:
        if entry.get("role") != "upload":
            continue
        if entry.get("client_index") == client_index:
            entry["wallet_address"] = wallet_address
            entry["wallet_secret_key"] = wallet_secret_key
            matched = True
            break
    if not matched:
        raise RuntimeError(
            f"No uploader with client_index={client_index} found in client "
            f"state for '{name}'."
        )
    client = _get_s3_client()
    client.put_object(
        Bucket=S3_BUCKET,
        Key=f"{S3_CLIENT_KEY_PREFIX}/{name}.json",
        Body=json.dumps(state, indent=2),
        ContentType="application/json",
    )


def load_client_state(name: str) -> dict:
    """Load client deployment metadata from S3.

    Raises RuntimeError if the client state is not found.
    """
    client = _get_s3_client()
    try:
        resp = client.get_object(
            Bucket=S3_BUCKET,
            Key=f"{S3_CLIENT_KEY_PREFIX}/{name}.json",
        )
        return json.loads(resp["Body"].read())
    except client.exceptions.NoSuchKey:
        raise RuntimeError(
            f"No client state found for '{name}'. "
            "Was this client deployment created with the infra-client command?"
        )


def delete_client_state(name: str) -> None:
    """Delete client deployment metadata from S3."""
    client = _get_s3_client()
    client.delete_object(
        Bucket=S3_BUCKET,
        Key=f"{S3_CLIENT_KEY_PREFIX}/{name}.json",
    )


S3_CLIENT_ADDRESSES_PREFIX = "saorsa-deploy/addresses"


def delete_client_addresses_prefix(name: str) -> int:
    """Delete every object under the per-testnet client-addresses prefix.

    Returns the number of objects deleted. Safe to call when the prefix is
    empty — returns 0.
    """
    client = _get_s3_client()
    prefix = f"{S3_CLIENT_ADDRESSES_PREFIX}/{name}/"
    paginator = client.get_paginator("list_objects_v2")
    deleted = 0
    for page in paginator.paginate(Bucket=S3_ADDRESSES_BUCKET, Prefix=prefix):
        objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if not objects:
            continue
        client.delete_objects(
            Bucket=S3_ADDRESSES_BUCKET,
            Delete={"Objects": objects},
        )
        deleted += len(objects)
    return deleted


S3_JOURNAL_KEY_PREFIX = "saorsa-deploy/journals"


def save_step_journal(name: str, journal: dict) -> None:
    """Save a ``deployments up`` step journal to S3.

    The journal records the steps that have completed so far in a manifest
    deploy run, so a re-run after a mid-sequence failure can offer to resume
    instead of re-executing everything (notably the slow binary builds).
    """
    client = _get_s3_client()
    client.put_object(
        Bucket=S3_BUCKET,
        Key=f"{S3_JOURNAL_KEY_PREFIX}/{name}.json",
        Body=json.dumps(journal, indent=2),
        ContentType="application/json",
    )


def load_step_journal(name: str) -> dict | None:
    """Load a ``deployments up`` step journal from S3.

    Returns ``None`` when no journal exists — unlike the other state loaders,
    absence is the normal case (a fresh run, or a previous run that completed
    and cleaned up after itself), not an error.
    """
    client = _get_s3_client()
    try:
        resp = client.get_object(
            Bucket=S3_BUCKET,
            Key=f"{S3_JOURNAL_KEY_PREFIX}/{name}.json",
        )
        return json.loads(resp["Body"].read())
    except client.exceptions.NoSuchKey:
        return None


def delete_step_journal(name: str) -> None:
    """Delete a ``deployments up`` step journal from S3.

    Safe to call when no journal exists (S3 deletes are idempotent).
    """
    client = _get_s3_client()
    client.delete_object(
        Bucket=S3_BUCKET,
        Key=f"{S3_JOURNAL_KEY_PREFIX}/{name}.json",
    )


def check_terraform_state_exists(state_key: str) -> bool:
    """Check if a Terraform state file already exists in S3."""
    client = _get_s3_client()
    try:
        client.head_object(Bucket=S3_BUCKET, Key=state_key)
        return True
    except client.exceptions.ClientError:
        return False


def list_terraform_state_keys(prefix: str) -> list[str]:
    """List all S3 keys matching the given prefix."""
    client = _get_s3_client()
    paginator = client.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


def list_extensions_of(source_name: str) -> list[str]:
    """Return the names of any deployments that extend ``source_name``.

    Scans every deployment state file under ``saorsa-deploy/deployments/`` and
    returns the names whose ``shared_bootstrap_from`` equals ``source_name``.
    Used by ``bootstraps destroy`` to refuse when extensions would be silently
    broken.
    """
    client = _get_s3_client()
    paginator = client.get_paginator("list_objects_v2")
    extensions = []
    for page in paginator.paginate(
        Bucket=S3_BUCKET, Prefix=f"{S3_KEY_PREFIX}/"
    ):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            try:
                resp = client.get_object(Bucket=S3_BUCKET, Key=key)
                state = json.loads(resp["Body"].read())
            except (client.exceptions.NoSuchKey, json.JSONDecodeError):
                continue
            if state.get("shared_bootstrap_from") == source_name:
                name = state.get("name")
                if name and name != source_name:
                    extensions.append(name)
    return extensions
