import os
import sys

from rich.console import Console
from rich.table import Table

from saorsa_deploy.hetzner_availability import (
    fetch_availability_map,
    fetch_server_types,
)


def _filter_types(
    server_types: list[dict], requested: str | None
) -> list[dict]:
    if requested is None:
        return sorted(server_types, key=lambda st: st.get("name", ""))
    matches = [st for st in server_types if st.get("name") == requested]
    if not matches:
        names = sorted(st.get("name", "") for st in server_types)
        raise ValueError(
            f"Unknown Hetzner server type '{requested}'. Known: {', '.join(names)}"
        )
    return matches


def _resolve_regions(
    requested_regions: list[str] | None,
    availability: dict[str, set[int]],
) -> list[str]:
    all_regions = sorted(availability.keys())
    if requested_regions is None:
        return all_regions
    unknown = [r for r in requested_regions if r not in availability]
    if unknown:
        raise ValueError(
            f"Unknown Hetzner region(s): {', '.join(unknown)}. "
            f"Known: {', '.join(all_regions)}"
        )
    return [r for r in all_regions if r in requested_regions]


def _build_availability_table(
    server_types: list[dict],
    regions: list[str],
    availability: dict[str, set[int]],
) -> Table:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Type")
    table.add_column("Cores", justify="right")
    table.add_column("Memory (GB)", justify="right")
    table.add_column("Disk (GB)", justify="right")
    table.add_column("CPU")
    table.add_column("Arch")
    for region in regions:
        table.add_column(region, justify="center")

    for st in server_types:
        row = [
            st.get("name", ""),
            str(st.get("cores", "")),
            str(st.get("memory", "")),
            str(st.get("disk", "")),
            st.get("cpu_type", ""),
            st.get("architecture", ""),
        ]
        st_id = st.get("id")
        for region in regions:
            available = st_id in availability.get(region, set())
            row.append("[green]✓[/green]" if available else "[red]✗[/red]")
        table.add_row(*row)

    return table


def cmd_hetzner_availability(args):
    """Print a matrix of Hetzner server-type availability per region."""
    console = Console()

    token = os.environ.get("HCLOUD_TOKEN")
    if not token:
        console.print(
            "[bold red]Error:[/bold red] HCLOUD_TOKEN is not set. "
            "Export a Hetzner Cloud API token and retry."
        )
        sys.exit(1)

    try:
        server_types = fetch_server_types(token)
        availability = fetch_availability_map(token)
    except RuntimeError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    try:
        filtered_types = _filter_types(server_types, args.server_type)
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

    console.print(_build_availability_table(filtered_types, regions, availability))
