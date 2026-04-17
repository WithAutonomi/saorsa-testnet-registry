import os
import sys

from rich.console import Console
from rich.table import Table

from saorsa_deploy.vultr_availability import (
    fetch_plan_availability_map,
    fetch_plans,
)


def _filter_plans(plans: list[dict], requested: str | None) -> list[dict]:
    if requested is None:
        return sorted(plans, key=lambda p: p.get("id", ""))
    matches = [p for p in plans if p.get("id") == requested]
    if not matches:
        names = sorted(p.get("id", "") for p in plans)
        preview = ", ".join(names[:10])
        suffix = ", ..." if len(names) > 10 else ""
        raise ValueError(
            f"Unknown Vultr plan '{requested}'. "
            f"Known ({len(names)} plans): {preview}{suffix}"
        )
    return matches


def _resolve_regions(
    requested_regions: list[str] | None,
    availability: dict[str, set[str]],
) -> list[str]:
    all_regions = sorted(availability.keys())
    if requested_regions is None:
        return all_regions
    unknown = [r for r in requested_regions if r not in availability]
    if unknown:
        raise ValueError(
            f"Unknown Vultr region(s): {', '.join(unknown)}. "
            f"Known: {', '.join(all_regions)}"
        )
    return [r for r in all_regions if r in requested_regions]


def _build_availability_table(
    plans: list[dict],
    regions: list[str],
    availability: dict[str, set[str]],
) -> Table:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Plan")
    table.add_column("vCPU", justify="right")
    table.add_column("RAM (MB)", justify="right")
    table.add_column("Disk (GB)", justify="right")
    table.add_column("$/mo", justify="right")
    for region in regions:
        table.add_column(region, justify="center")

    for plan in plans:
        row = [
            plan.get("id", ""),
            str(plan.get("vcpu_count", "")),
            str(plan.get("ram", "")),
            str(plan.get("disk", "")),
            f"{plan.get('monthly_cost', 0):.2f}",
        ]
        plan_id = plan.get("id")
        for region in regions:
            available = plan_id in availability.get(region, set())
            row.append("[green]✓[/green]" if available else "[red]✗[/red]")
        table.add_row(*row)

    return table


def cmd_vultr_availability(args):
    """Print a matrix of Vultr plan availability per region."""
    console = Console()

    token = os.environ.get("VULTR_API_KEY")
    if not token:
        console.print(
            "[bold red]Error:[/bold red] VULTR_API_KEY is not set. "
            "Export a Vultr API key and retry."
        )
        sys.exit(1)

    try:
        plans = fetch_plans(token)
        availability = fetch_plan_availability_map(token)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    try:
        filtered_plans = _filter_plans(plans, args.plan)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    requested_regions = None
    if args.region:
        requested_regions = [r.strip() for r in args.region.split(",") if r.strip()]
    try:
        regions = _resolve_regions(requested_regions, availability)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    if not regions:
        console.print("[yellow]No regions matched the filter.[/yellow]")
        sys.exit(1)

    console.print(_build_availability_table(filtered_plans, regions, availability))
