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
    bootstrap_ip: str,
    vm_ips: dict[str, list[str]],
) -> None:
    """Save deployment metadata to S3 for later use by other commands."""
    state = {
        "name": name,
        "regions": [[provider, region] for provider, region in regions],
        "terraform_variables": terraform_variables,
        "bootstrap_ip": bootstrap_ip,
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
