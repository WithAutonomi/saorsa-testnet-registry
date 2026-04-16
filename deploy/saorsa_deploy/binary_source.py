import boto3
import botocore.exceptions
import requests

GITHUB_REPO = "WithAutonomi/ant-node"
RELEASE_ASSET_NAME = "ant-node-cli-linux-x64.tar.gz"

BUILDS_BUCKET = "ant-node-builds"
BUILDS_REGION = "eu-west-2"
BUILDS_KEY_PREFIX = "builds"


def get_release_url(version: str | None = None) -> str:
    """Get the download URL for an ant-node release from GitHub.

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


def get_custom_build_url(name: str, repo_owner: str, branch_name: str) -> str:
    """Return the S3 URL for a custom-built binary."""
    key = f"{BUILDS_KEY_PREFIX}/{name}/{repo_owner}/{branch_name}/ant-node"
    return f"https://{BUILDS_BUCKET}.s3.{BUILDS_REGION}.amazonaws.com/{key}"


def check_release_exists(version: str) -> bool:
    """Check if a specific release version exists on GitHub."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/v{version}"
    resp = requests.get(url, timeout=30)
    return resp.status_code == 200


def check_custom_build_exists(name: str, repo_owner: str, branch_name: str) -> bool:
    """Check if a custom-built binary exists in S3."""
    key = f"{BUILDS_KEY_PREFIX}/{name}/{repo_owner}/{branch_name}/ant-node"
    s3 = boto3.client("s3", region_name=BUILDS_REGION)
    try:
        s3.head_object(Bucket=BUILDS_BUCKET, Key=key)
        return True
    except botocore.exceptions.ClientError:
        return False


# --- ant (client) binary ---

ANT_GITHUB_REPO = "WithAutonomi/ant-client"
ANT_RELEASE_ASSET_SUFFIX = "x86_64-unknown-linux-musl.tar.gz"


def get_ant_release_url(version: str | None = None) -> str:
    """Get the download URL for an ant release from GitHub.

    If version is None, fetches the latest release. Otherwise fetches by tag.
    The ant-client repo uses tags like 'ant-cli-v{version}'.
    Asset names include the version: 'ant-{version}-x86_64-unknown-linux-musl.tar.gz'.
    Returns the asset download URL.
    """
    if version:
        url = (
            f"https://api.github.com/repos/{ANT_GITHUB_REPO}"
            f"/releases/tags/ant-cli-v{version}"
        )
    else:
        url = f"https://api.github.com/repos/{ANT_GITHUB_REPO}/releases/latest"

    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    release = resp.json()
    for asset in release.get("assets", []):
        if asset["name"].endswith(ANT_RELEASE_ASSET_SUFFIX):
            return asset["browser_download_url"]
    tag = f"ant-cli-v{version}" if version else "latest"
    raise RuntimeError(
        f"Could not find asset matching '*{ANT_RELEASE_ASSET_SUFFIX}' "
        f"in {tag} release of {ANT_GITHUB_REPO}"
    )


def get_ant_custom_build_url(name: str, repo_owner: str, branch_name: str) -> str:
    """Return the S3 URL for a custom-built ant binary."""
    key = f"{BUILDS_KEY_PREFIX}/{name}/{repo_owner}/{branch_name}/ant"
    return f"https://{BUILDS_BUCKET}.s3.{BUILDS_REGION}.amazonaws.com/{key}"


def check_ant_release_exists(version: str) -> bool:
    """Check if a specific ant release version exists on GitHub."""
    url = (
        f"https://api.github.com/repos/{ANT_GITHUB_REPO}"
        f"/releases/tags/ant-cli-v{version}"
    )
    resp = requests.get(url, timeout=30)
    return resp.status_code == 200


def check_ant_custom_build_exists(
    name: str, repo_owner: str, branch_name: str
) -> bool:
    """Check if a custom-built ant binary exists in S3."""
    key = f"{BUILDS_KEY_PREFIX}/{name}/{repo_owner}/{branch_name}/ant"
    s3 = boto3.client("s3", region_name=BUILDS_REGION)
    try:
        s3.head_object(Bucket=BUILDS_BUCKET, Key=key)
        return True
    except botocore.exceptions.ClientError:
        return False
