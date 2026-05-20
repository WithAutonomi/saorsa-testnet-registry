"""Print every file uploaded to production Autonomi, grouped by upload run.

Reads the ``deployment_prod_upload_results`` Postgres table (populated by
``deployments record-results`` for both ``prod-uploaders`` and
``prod-local-uploads`` deployments) and prints one section per deployment
("upload run"), with the files uploaded under that run inside it.

Within each successful section, rows are deduplicated by datamap address
(same file uploaded twice in the same run = one entry, using the earliest
``started_at``). The same datamap may legitimately appear in multiple
sections if the file was uploaded across more than one run. With
``--failed``, every non-success row is printed verbatim with no dedup.
"""
from rich.console import Console
from saorsa_db.models.prod_upload_result import ProdUploadStatus

from saorsa_deploy.registry import get_repository


def cmd_prod_files(args) -> None:
    console = Console()
    repo = get_repository()
    if repo is None:
        console.print(
            "[yellow]No DATABASE_URL configured — nothing to read.[/yellow]"
        )
        return

    rows = repo.list_all_prod_upload_results(failed_only=args.failed)
    if not rows:
        if args.failed:
            console.print("[yellow]No failed prod uploads recorded.[/yellow]")
        else:
            console.print("[yellow]No successful prod uploads recorded.[/yellow]")
        return

    deployment_ids = {row.deployment_id for row in rows}
    deployments = {}
    for did in deployment_ids:
        dep = repo.get(did)
        if dep is not None:
            deployments[did] = dep

    grouped: dict[int, list] = {}
    for row in rows:
        grouped.setdefault(row.deployment_id, []).append(row)

    sorted_groups = sorted(
        grouped.items(),
        key=lambda kv: (
            deployments[kv[0]].created_at
            if kv[0] in deployments
            else min(r.started_at for r in kv[1])
        ),
    )

    first = True
    for deployment_id, group_rows in sorted_groups:
        if args.failed:
            entries = sorted(group_rows, key=lambda r: (r.file_name or "", r.started_at))
        else:
            entries = sorted(
                _dedup_by_address(group_rows),
                key=lambda r: (r.file_name or "", r.started_at),
            )
        if not entries:
            continue
        if not first:
            console.print()
        first = False
        _print_group_header(console, deployments.get(deployment_id), deployment_id)
        for row in entries:
            _print_row(console, row, failed=args.failed)


def _dedup_by_address(rows):
    by_address = {}
    for row in rows:
        if not row.address:
            continue
        existing = by_address.get(row.address)
        if existing is None or row.started_at < existing.started_at:
            by_address[row.address] = row
    return list(by_address.values())


def _print_group_header(console: Console, deployment, deployment_id: int) -> None:
    if deployment is None:
        console.print(f"[bold cyan]=== <deployment id={deployment_id}> ===[/bold cyan]")
        return
    kind = ""
    if isinstance(deployment.deploy_args, dict):
        kind = deployment.deploy_args.get("kind") or ""
    created = deployment.created_at.strftime("%Y-%m-%d %H:%M:%S").strip()
    bits = [f"{deployment.name} (id={deployment.id})"]
    if kind:
        bits.append(kind)
    if created:
        bits.append(f"started {created}")
    console.print(f"[bold cyan]=== {'  '.join(bits)} ===[/bold cyan]")


def _print_row(console: Console, row, failed: bool) -> None:
    console.print(row.file_name or "<unnamed>")
    if row.address:
        console.print(f"    address:  {row.address}")
    else:
        console.print("    address:  <none>")
    console.print(f"    size:     {_format_size(row.file_size_bytes)}")
    if row.started_at is not None:
        ts = row.started_at.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
        if not ts:
            ts = row.started_at.isoformat()
        console.print(f"    uploaded: {ts}")
    if failed:
        status_name = (
            row.status.value if isinstance(row.status, ProdUploadStatus) else str(row.status)
        )
        console.print(f"    status:   {status_name}")
        if row.reason:
            console.print(f"    reason:   {row.reason}")
    console.print()


def _format_size(num_bytes) -> str:
    if num_bytes is None:
        return "<unknown>"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{num_bytes} B"
