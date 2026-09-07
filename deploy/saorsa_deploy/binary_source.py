import os
import subprocess

import boto3
import botocore.exceptions
import requests

GITHUB_REPO = "WithAutonomi/ant-node"
RELEASE_ASSET_NAME = "ant-node-cli-linux-x64.tar.gz"

BUILDS_BUCKET = "ant-node-builds"
BUILDS_REGION = "eu-west-2"
BUILDS_KEY_PREFIX = "builds"


def _github_headers() -> dict[str, str]:
    """Headers for GitHub REST calls, with auth when a token is in the env.

    Unauthenticated GitHub API access is capped at 60 requests/hour per IP, so a
    burst of deploys can exhaust it and make valid releases look missing. A
    ``GITHUB_TOKEN`` (or ``GH_TOKEN``, the gh-CLI spelling) lifts the limit to
    5000/hour. The token is optional — public release lookups still work without
    one until the limit is hit.
    """
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _release_tag_exists(repo: str, tag: str) -> bool:
    """Return True/False for a GitHub release tag, raising on indeterminate replies.

    Only a 200 (exists) or 404 (genuinely missing) is a definitive answer.
    Anything else — most importantly a 403 rate-limit, but also 429/5xx — must
    NOT be reported as "missing", or a transient error masquerades as a bad
    version. Those cases raise a clear RuntimeError instead.
    """
    url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    resp = requests.get(url, headers=_github_headers(), timeout=30)
    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False

    rate_limited = (
        resp.status_code in (403, 429)
        and resp.headers.get("X-RateLimit-Remaining") == "0"
    )
    hint = (
        " GitHub API rate limit exceeded — set GITHUB_TOKEN to raise the "
        "60/hour unauthenticated limit to 5000/hour."
        if rate_limited
        else ""
    )
    raise RuntimeError(
        f"GitHub returned HTTP {resp.status_code} checking release "
        f"'{tag}' in {repo}; cannot determine whether it exists.{hint}"
    )


def remote_branch_exists(repo_owner: str, repo_name: str, branch_name: str) -> bool:
    """Return True if ``branch_name`` resolves to a ref on the remote GitHub repo.

    Uses ``git ls-remote --heads --tags`` so both branches and tags are accepted
    (``git clone --branch`` accepts either). No authentication is required for
    public repos. Raises RuntimeError if git itself fails to reach the remote.

    Git is forced into non-interactive mode: when the repo or branch is missing
    (or the repo is private), GitHub answers HTTPS with a 401, which would
    otherwise make git prompt for a username/password on the terminal. The env
    overrides below turn that prompt into a clean non-zero exit instead.
    """
    url = f"https://github.com/{repo_owner}/{repo_name}.git"
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "GCM_INTERACTIVE": "never",
    }
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", "--tags", url, branch_name],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
    except FileNotFoundError as e:
        raise RuntimeError("git executable not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Timed out querying {url} for branch '{branch_name}'") from e

    if result.returncode != 0:
        stderr = result.stderr.strip()
        # A 401 over HTTPS (with prompts disabled) means the repo does not exist
        # or is private — almost always a wrong --repo-owner.
        if "could not read Username" in stderr or "Authentication failed" in stderr:
            raise RuntimeError(
                f"Repository {url} was not found or is private "
                f"(GitHub returned 401). Check --repo-owner."
            )
        raise RuntimeError(f"Failed to query {url}: {stderr or 'git ls-remote failed'}")

    return bool(result.stdout.strip())


def validate_branch_exists(repo_owner: str, repo_name: str, branch_name: str) -> None:
    """Raise ValueError if ``branch_name`` does not exist on the remote repo."""
    if not remote_branch_exists(repo_owner, repo_name, branch_name):
        raise ValueError(
            f"Branch '{branch_name}' was not found on "
            f"https://github.com/{repo_owner}/{repo_name}. "
            f"Check --repo-owner and --branch-name."
        )


def get_release_url(version: str | None = None) -> str:
    """Get the download URL for an ant-node release from GitHub.

    If version is None, fetches the latest release. Otherwise fetches by tag v{version}.
    Returns the asset download URL.
    """
    if version:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/v{version}"
    else:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

    resp = requests.get(url, headers=_github_headers(), timeout=30)
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
    """Check if a specific ant-node release version exists on GitHub.

    Raises on a rate-limit/transient reply rather than reporting "missing".
    """
    return _release_tag_exists(GITHUB_REPO, f"v{version}")


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

    resp = requests.get(url, headers=_github_headers(), timeout=30)
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
    """Check if a specific ant (client) release version exists on GitHub.

    Raises on a rate-limit/transient reply rather than reporting "missing".
    """
    return _release_tag_exists(ANT_GITHUB_REPO, f"ant-cli-v{version}")


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


# --- ant-core WASM package (browser/WASM clients) ---

#: Filename of the WASM client bundle in S3. A tarball rather than a single
#: file: the artifact is the wasm-pack output (`ant_core_bg.wasm`, `ant_core.js`,
#: `ant_core.d.ts`, `package.json`) plus the compiled TypeScript client and its
#: node_modules, which must land on the VM together.
ANT_WASM_ARCHIVE_NAME = "ant-wasm-client.tar.gz"


def get_ant_wasm_build_url(name: str, repo_owner: str, branch_name: str) -> str:
    """Return the S3 URL for a built WASM client bundle.

    There is no release form: no ant-core WASM package is published as a GitHub
    release, so this bundle is always built from source by `build ant-wasm`.
    """
    key = f"{BUILDS_KEY_PREFIX}/{name}/{repo_owner}/{branch_name}/{ANT_WASM_ARCHIVE_NAME}"
    return f"https://{BUILDS_BUCKET}.s3.{BUILDS_REGION}.amazonaws.com/{key}"


def check_ant_wasm_build_exists(name: str, repo_owner: str, branch_name: str) -> bool:
    """Check whether a built WASM client bundle exists in S3."""
    key = f"{BUILDS_KEY_PREFIX}/{name}/{repo_owner}/{branch_name}/{ANT_WASM_ARCHIVE_NAME}"
    s3 = boto3.client("s3", region_name=BUILDS_REGION)
    try:
        s3.head_object(Bucket=BUILDS_BUCKET, Key=key)
        return True
    except botocore.exceptions.ClientError:
        return False
