"""Refuse to quietly tear down a deployment whose results were never recorded.

A remote upload run emits its per-file results to the deployment's InfluxDB.
They only reach Postgres when ``deployments record-results`` is run — and the
teardown destroys the monitoring VM, taking InfluxDB with it. Anything not
recorded by then is **gone permanently**: the per-file addresses, chunk counts
and costs, and with them any chance of closing the file-pool claims that run
took, which then stay open forever with no way to resolve them.

This is a warning, not a refusal. Blocking a non-interactive teardown would
strand billable cloud infrastructure — the release skills run ``down``
unattended — and an operator who genuinely wants the data gone should not have
to fight the tool. So: warn loudly always, prompt when someone is there to
answer, proceed otherwise.
"""

import sys
from dataclasses import dataclass


@dataclass
class ResultsStatus:
    """What is recorded versus what was expected, for one deployment."""

    expected: int | None
    recorded: int
    open_claims: int

    @property
    def unrecorded(self) -> int | None:
        if self.expected is None:
            return None
        return max(self.expected - self.recorded, 0)

    @property
    def is_concerning(self) -> bool:
        """Whether tearing down now would destroy data that was never captured.

        Nothing recorded at all is always concerning. A partial recording is
        concerning too — a mid-run ``record-results`` followed by more uploads
        leaves the tail uncaptured.
        """
        if self.expected is None:
            return self.recorded == 0 and self.open_claims > 0
        return self.unrecorded > 0


def assess(repo, pool_repo, deployment, kind: str) -> ResultsStatus:
    """Compare recorded result rows against the number of files expected.

    The expected count comes from the deployment's **pool claims** when it has
    any, which is exact: the claims say precisely how many files that run took.
    Without the pool there is no reliable expectation — a hand-written URL list
    lives on the operator's disk and may have changed — so ``expected`` is None
    and the check falls back to "was anything recorded at all?".
    """
    open_claims = 0
    expected: int | None = None
    if pool_repo is not None:
        claims = pool_repo.list_pool_claims(deployment_id=deployment.id)
        if claims:
            expected = len(claims)
            open_claims = sum(1 for claim in claims if claim.is_open)

    if kind == "prod-canary":
        recorded = len(repo.list_prod_canary_uploads_for_deployment(deployment.id))
    else:
        recorded = len(repo.list_prod_upload_results_for_deployment(deployment.id))

    return ResultsStatus(expected=expected, recorded=recorded, open_claims=open_claims)


def warn_if_unrecorded(
    console,
    deployment_name: str,
    status: ResultsStatus,
    *,
    interactive: bool,
    input_fn=input,
) -> bool:
    """Warn about unrecorded results; return False if the operator aborts.

    Returns True when the teardown should proceed — which is every
    non-interactive case, deliberately.
    """
    if not status.is_concerning:
        return True

    console.print()
    console.print(
        "[bold yellow]⚠  Results have not been fully recorded for "
        f"'{deployment_name}'.[/bold yellow]"
    )
    if status.expected is not None:
        console.print(
            f"   {status.recorded} of {status.expected} expected file result(s) are in Postgres."
        )
    else:
        console.print("   No file results are in Postgres for this deployment.")
    if status.open_claims:
        console.print(
            f"   {status.open_claims} file-pool claim(s) are still open and "
            "will never resolve once the results are gone."
        )
    console.print(
        "   Destroying this deployment destroys its InfluxDB, and anything not "
        "recorded goes with it — permanently."
    )
    console.print()
    console.print(
        f"   [bold]saorsa-deploy deployments record-results --name {deployment_name}[/bold]"
    )
    console.print()

    if not interactive:
        # Refusing here would strand billable infrastructure whenever `down`
        # runs unattended, which is how the release skills invoke it.
        console.print("[yellow]   Non-interactive — continuing with the teardown anyway.[/yellow]")
        return True

    answer = input_fn("   Continue with the teardown? [y/N] ").strip().lower()
    if answer in ("y", "yes"):
        return True
    console.print("[bold]Teardown aborted.[/bold] Nothing was destroyed.")
    return False


def check_before_destroy(console, deployment_name: str, kind: str) -> None:
    """Full guard: assess, warn, and exit 0 if the operator chooses to stop.

    Best-effort with respect to the registry — no database means nothing to
    compare against, and that must not block a teardown.
    """
    from saorsa_deploy.registry import get_file_pool_repository, get_repository

    repo = get_repository()
    if repo is None:
        return
    deployment = repo.get_active_by_name(deployment_name) or repo.get_by_name(deployment_name)
    if deployment is None:
        return

    try:
        status = assess(repo, get_file_pool_repository(), deployment, kind)
    except Exception:
        # A guard that breaks a teardown when it cannot read the registry would
        # be worse than the problem it exists to prevent.
        return

    if not warn_if_unrecorded(
        console,
        deployment_name,
        status,
        interactive=sys.stdin.isatty() and sys.stdout.isatty(),
    ):
        sys.exit(0)
