"""GitHub Actions API client for dispatching and monitoring workflow runs."""

import json
import logging
import os
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

DEFAULT_GITHUB_REPO = "jacderida/saorsa-testnet-registry"
DEFAULT_WORKFLOW_FILE = "launch-testnet.yml"
POLL_INTERVAL = 2
POLL_TIMEOUT = 30


class GitHubAPIError(Exception):
    """Raised when a GitHub API call fails or times out."""


def _headers(pat: str) -> dict:
    """Build common headers for GitHub API requests."""
    return {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _resolve_params(
    pat: str | None = None,
    repo: str | None = None,
    workflow_file: str | None = None,
) -> tuple[str, str, str]:
    """Resolve GitHub config from explicit args or environment variables.

    Parameters are resolved in this order of priority:
    1. Explicit function argument
    2. Environment variable
    3. Default value

    This allows the functions to work both inside a Flask app context
    (where config is passed explicitly) and from the standalone monitor
    worker (where values come from env vars).
    """
    resolved_pat = pat or os.environ.get("WORKFLOW_RUNNER_PAT", "")
    resolved_repo = repo or os.environ.get("GITHUB_REPO", DEFAULT_GITHUB_REPO)
    resolved_workflow = workflow_file or os.environ.get(
        "WORKFLOW_FILE", DEFAULT_WORKFLOW_FILE
    )

    if not resolved_pat:
        raise GitHubAPIError("WORKFLOW_RUNNER_PAT is not configured")

    return resolved_pat, resolved_repo, resolved_workflow


def dispatch_workflow(
    deploy_args: dict | None = None,
    pat: str | None = None,
    repo: str | None = None,
    workflow_file: str | None = None,
    branch: str = "main",
    inputs: dict | None = None,
) -> int:
    """Dispatch a workflow and return the new run ID.

    Sends a workflow_dispatch event to the GitHub Actions API, then polls
    for the newly created run until it appears or the timeout is reached.

    Args:
        deploy_args: Dictionary of deployment arguments, serialised as JSON and
            passed as the ``deploy_args`` workflow input.  Mutually exclusive
            with *inputs*.
        pat: GitHub personal access token. Falls back to WORKFLOW_RUNNER_PAT env var.
        repo: GitHub repository in "owner/repo" format. Falls back to GITHUB_REPO env var.
        workflow_file: Workflow filename. Falls back to WORKFLOW_FILE env var.
        branch: Git ref to dispatch the workflow on. Defaults to "main".
        inputs: Raw workflow inputs dict.  When provided, *deploy_args* is
            ignored and this dict is sent directly as the ``inputs`` field.

    Returns:
        The workflow_run_id of the newly created run.

    Raises:
        GitHubAPIError: If the dispatch fails or the run cannot be found within the timeout.
    """
    resolved_pat, resolved_repo, resolved_workflow = _resolve_params(
        pat, repo, workflow_file
    )

    dispatch_url = (
        f"https://api.github.com/repos/{resolved_repo}"
        f"/actions/workflows/{resolved_workflow}/dispatches"
    )

    if inputs is not None:
        workflow_inputs = inputs
    elif deploy_args is not None:
        workflow_inputs = {"deploy_args": json.dumps(deploy_args)}
    else:
        raise GitHubAPIError("Either deploy_args or inputs must be provided")

    payload = {
        "ref": branch,
        "inputs": workflow_inputs,
    }

    # Record timestamp before dispatching so we can find the new run
    before_dispatch = datetime.now(timezone.utc)

    logger.info(
        "Dispatching workflow %s on %s (ref=%s, inputs=%s)",
        resolved_workflow,
        resolved_repo,
        branch,
        workflow_inputs,
    )
    resp = requests.post(
        dispatch_url,
        headers=_headers(resolved_pat),
        json=payload,
        timeout=30,
    )

    if resp.status_code != 204:
        raise GitHubAPIError(
            f"Workflow dispatch failed with status {resp.status_code}: {resp.text}"
        )

    logger.info("Dispatch accepted, polling for new run...")

    # Poll for the new run
    deadline = time.monotonic() + POLL_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        runs = get_workflow_runs(
            pat=resolved_pat,
            repo=resolved_repo,
            workflow_file=resolved_workflow,
        )
        for run in runs:
            created_at = datetime.fromisoformat(
                run["created_at"].replace("Z", "+00:00")
            )
            if created_at >= before_dispatch:
                logger.info("Found new workflow run: id=%d", run["id"])
                return run["id"]

    raise GitHubAPIError(
        f"Timed out after {POLL_TIMEOUT}s waiting for workflow run to appear"
    )


def get_workflow_runs(
    pat: str | None = None,
    repo: str | None = None,
    workflow_file: str | None = None,
) -> list[dict]:
    """Fetch recent workflow runs for the configured workflow.

    Args:
        pat: GitHub personal access token. Falls back to WORKFLOW_RUNNER_PAT env var.
        repo: GitHub repository in "owner/repo" format. Falls back to GITHUB_REPO env var.
        workflow_file: Workflow filename. Falls back to WORKFLOW_FILE env var.

    Returns:
        List of run dicts, each containing at least: id, status, conclusion, created_at.

    Raises:
        GitHubAPIError: If the API request fails.
    """
    resolved_pat, resolved_repo, resolved_workflow = _resolve_params(
        pat, repo, workflow_file
    )

    runs_url = (
        f"https://api.github.com/repos/{resolved_repo}"
        f"/actions/workflows/{resolved_workflow}/runs"
    )

    resp = requests.get(
        runs_url,
        headers=_headers(resolved_pat),
        params={"per_page": 5},
        timeout=30,
    )

    if resp.status_code != 200:
        raise GitHubAPIError(
            f"Failed to fetch workflow runs with status {resp.status_code}: {resp.text}"
        )

    data = resp.json()
    return [
        {
            "id": run["id"],
            "status": run["status"],
            "conclusion": run["conclusion"],
            "created_at": run["created_at"],
        }
        for run in data.get("workflow_runs", [])
    ]


def get_workflow_run(
    run_id: int,
    pat: str | None = None,
    repo: str | None = None,
) -> dict:
    """Fetch details for a specific workflow run.

    Args:
        run_id: The workflow run ID to fetch.
        pat: GitHub personal access token. Falls back to WORKFLOW_RUNNER_PAT env var.
        repo: GitHub repository in "owner/repo" format. Falls back to GITHUB_REPO env var.

    Returns:
        Dict with run details: id, status, conclusion, created_at, updated_at, html_url.

    Raises:
        GitHubAPIError: If the API request fails.
    """
    resolved_pat, resolved_repo, _ = _resolve_params(pat, repo)

    run_url = f"https://api.github.com/repos/{resolved_repo}/actions/runs/{run_id}"

    resp = requests.get(
        run_url,
        headers=_headers(resolved_pat),
        timeout=30,
    )

    if resp.status_code != 200:
        raise GitHubAPIError(
            f"Failed to fetch workflow run {run_id} with status {resp.status_code}: {resp.text}"
        )

    run = resp.json()
    return {
        "id": run["id"],
        "status": run["status"],
        "conclusion": run["conclusion"],
        "created_at": run["created_at"],
        "updated_at": run["updated_at"],
        "html_url": run["html_url"],
    }
