"""Prune old rows from `deployment_host_metric_buckets`.

Bucketed host metrics are the bulk of what `record-analysis` writes — a single
staging run stores roughly a hundred thousand rows — and the registry runs on a
small managed Postgres instance where the table exceeding the cache is what
makes queries slow, not the disk bill.

Pruning deliberately touches **only** the bucket table. The
`deployment_host_results` rollup row is left intact, so a pruned deployment
degrades to exactly the whole-run view it would have had before bucketing
existed, rather than losing its analysis. What is lost is the ability to
re-slice that run to a narrower window — and that is unrecoverable, since the
source InfluxDB is destroyed with the monitoring stack.
"""
import sys
from datetime import datetime, timedelta, timezone

from rich.console import Console
from rich.table import Table

from saorsa_deploy.registry import get_repository


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.strftime("%Y-%m-%d %H:%M:%S")


def cmd_prune_host_buckets(args) -> None:
    console = Console()

    repo = get_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] DATABASE_URL is not set. "
            "This command requires the registry database."
        )
        sys.exit(1)

    if args.older_than < 0:
        console.print(
            "[bold red]Error:[/bold red] --older-than must be zero or positive."
        )
        sys.exit(1)

    deployment_id = None
    if args.name:
        deployment = repo.get_active_by_name(args.name) or repo.get_by_name(args.name)
        if deployment is None:
            console.print(
                f"[bold red]Error:[/bold red] No deployment named "
                f"'{args.name}' in the registry."
            )
            sys.exit(1)
        deployment_id = deployment.id

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.older_than)
    scope = f"deployment '{args.name}'" if args.name else "all deployments"
    console.print(
        f"[bold]Buckets older than {_fmt_dt(cutoff)} UTC "
        f"({args.older_than}d) across {scope}:[/bold]"
    )

    # Report before deleting, always -- not only under --dry-run. This is an
    # irreversible delete of data that cannot be re-captured, so the operator
    # should see what went even when they did not ask for a preview.
    affected = repo.summarize_host_metric_buckets(cutoff=cutoff)
    if deployment_id is not None:
        affected = [r for r in affected if r["deployment_id"] == deployment_id]

    if not affected:
        console.print("[green]Nothing to prune.[/green]")
        return

    table = Table(title="Bucket rows to remove")
    table.add_column("Deployment")
    table.add_column("id", justify="right")
    table.add_column("Rows", justify="right")
    table.add_column("Oldest bucket (UTC)")
    table.add_column("Newest affected (UTC)")
    for row in affected:
        table.add_row(
            row["name"],
            str(row["deployment_id"]),
            f"{row['rows']:,}",
            _fmt_dt(row["first_bucket"]),
            _fmt_dt(row["last_bucket"]),
        )
    console.print(table)

    total = sum(row["rows"] for row in affected)
    if args.dry_run:
        console.print(
            f"[yellow]--dry-run: {total:,} row(s) across "
            f"{len(affected)} deployment(s) would be deleted. "
            "Nothing was changed.[/yellow]"
        )
        return

    deleted = repo.delete_host_metric_buckets_before(
        cutoff=cutoff, deployment_id=deployment_id
    )
    console.print(
        f"[bold green]Deleted {deleted:,} bucket row(s).[/bold green] "
        "The deployment_host_results rollup rows are untouched — those "
        "deployments keep their whole-run analysis, but can no longer be "
        "re-sliced to a narrower window."
    )
