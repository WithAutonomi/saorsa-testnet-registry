import json

import boto3

S3_BUCKET = "maidsafe-org-infra-tfstate"
S3_REGION = "eu-west-2"
S3_KEY_PREFIX = "saorsa-deploy/deployments"


def _get_s3_client():
    return boto3.client("s3", region_name=S3_REGION)


def save_deployment_state(
    name: str,
    regions: list[tuple[str, str]],
    terraform_variables: dict[str, str],
    bootstrap_nodes: list[dict],
    vm_ips: dict[str, list[str]],
) -> None:
    """Save deployment metadata to S3 for later use by other commands.

    Args:
        bootstrap_nodes: List of dicts with keys: ip, region, bootstrap_index.
            The first entry (index=1) is the genesis node.
    """
    state = {
        "name": name,
        "regions": [[provider, region] for provider, region in regions],
        "terraform_variables": terraform_variables,
        "bootstrap_nodes": bootstrap_nodes,
        "bootstrap_ip": bootstrap_nodes[0]["ip"] if bootstrap_nodes else "",
        "vm_ips": vm_ips,
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


S3_MONITORING_KEY_PREFIX = "saorsa-deploy/monitoring"


def save_monitoring_state(
    name: str,
    region: str,
    vm_ip: str,
    influxdb_url: str = "",
    influxdb_token: str = "",
    influxdb_org: str = "",
    influxdb_bucket: str = "",
    grafana_url: str = "",
) -> None:
    """Save monitoring stack metadata to S3."""
    state = {
        "name": name,
        "region": region,
        "vm_ip": vm_ip,
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
            "Was this monitoring stack created with the monitoring-infra command?"
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
    elasticsearch_url: str = "",
    elasticsearch_username: str = "",
    elasticsearch_password: str = "",
    elasticsearch_api_key: str = "",
    elasticsearch_index_pattern: str = "",
) -> None:
    """Save Elasticsearch stack metadata to S3."""
    state = {
        "name": name,
        "region": region,
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
            "Was this Elasticsearch stack created with the elastic-infra command?"
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
            region, file_size_mb, vm_size, ip, client_index.
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


def check_terraform_state_exists(state_key: str) -> bool:
    """Check if a Terraform state file already exists in S3."""
    client = _get_s3_client()
    try:
        client.head_object(Bucket=S3_BUCKET, Key=state_key)
        return True
    except client.exceptions.ClientError:
        return False
