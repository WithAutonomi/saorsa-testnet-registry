import boto3
import botocore.exceptions
import requests

GITHUB_REPO = "saorsa-labs/saorsa-node"
RELEASE_ASSET_NAME = "saorsa-node-cli-linux-x64.tar.gz"

BUILDS_BUCKET = "saorsa-node-builds"
BUILDS_REGION = "eu-west-2"
BUILDS_KEY_PREFIX = "builds"


def get_release_url(version: str | None = None) -> str:
    """Get the download URL for a saorsa-node release from GitHub.

    If version is None, fetches the latest release. Otherwise fetches by tag v{version}.
    Returns the asset download URL.
    """
    if version:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/v{version}"
    else:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    release = resp.json()
    for asset in release.get("assets", []):
        if asset["name"] == RELEASE_ASSET_NAME:
            return asset["browser_download_url"]
    tag = f"v{version}" if version else "latest"
    raise RuntimeError(
        f"Could not find asset '{RELEASE_ASSET_NAME}' in {tag} release of {GITHUB_REPO}"
    )


def get_custom_build_url(repo_owner: str, branch_name: str) -> str:
    """Return the S3 URL for a custom-built binary."""
    key = f"{BUILDS_KEY_PREFIX}/{repo_owner}/{branch_name}/saorsa-node"
    return f"https://{BUILDS_BUCKET}.s3.{BUILDS_REGION}.amazonaws.com/{key}"


def check_release_exists(version: str) -> bool:
    """Check if a specific release version exists on GitHub."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/v{version}"
    resp = requests.get(url, timeout=30)
    return resp.status_code == 200


def check_custom_build_exists(repo_owner: str, branch_name: str) -> bool:
    """Check if a custom-built binary exists in S3."""
    key = f"{BUILDS_KEY_PREFIX}/{repo_owner}/{branch_name}/saorsa-node"
    s3 = boto3.client("s3", region_name=BUILDS_REGION)
    try:
        s3.head_object(Bucket=BUILDS_BUCKET, Key=key)
        return True
    except botocore.exceptions.ClientError:
        return False
