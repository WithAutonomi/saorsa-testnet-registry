import re
import time
from concurrent.futures import ThreadPoolExecutor

from rich.console import Console
from rich.live import Live
from rich.table import Table

from saorsa_deploy.terraform import (
    TerraformResult,
    TerraformRunConfig,
    run_terraform,
    run_terraform_destroy,
)

MAX_CONCURRENT = 5

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def _format_elapsed(seconds: float) -> str:
    """Format elapsed seconds as mm:ss."""
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _build_status_table(
    statuses: dict[str, str],
    start_times: dict[str, float],
    spinner_tick: int,
    action_label: str = "applying...",
) -> Table:
    """Build a rich table showing the status of each region."""
    table = Table(show_header=True, header_style="bold")
    table.add_column("Provider")
    table.add_column("Region")
    table.add_column("Status")
    table.add_column("Elapsed")
    now = time.monotonic()
    for key in sorted(statuses.keys()):
        status = statuses[key]
        provider, region = key.split("/", 1)
        start = start_times.get(key, now)
        elapsed = _format_elapsed(now - start)
        if status == "running":
            frame = SPINNER_FRAMES[spinner_tick % len(SPINNER_FRAMES)]
            symbol = f"[yellow]{frame} {action_label}[/yellow]"
        elif status == "done":
            symbol = "[green]done[/green]"
        elif status == "pending":
            symbol = "[dim]pending[/dim]"
            elapsed = ""
        else:
            symbol = "[red]FAILED[/red]"
        table.add_row(provider, region, symbol, elapsed)
    return table


def _parse_resource_summary(stdout: str) -> dict[str, int]:
    """Parse terraform apply output for resource counts.

    Looks for lines like: Apply complete! Resources: 6 added, 0 changed, 0 destroyed.
    """
    counts = {"added": 0, "changed": 0, "destroyed": 0}
    match = re.search(
        r"(\d+) added, (\d+) changed, (\d+) destroyed",
        stdout,
    )
    if match:
        counts["added"] = int(match.group(1))
        counts["changed"] = int(match.group(2))
        counts["destroyed"] = int(match.group(3))
    return counts


def execute_terraform_runs(
    configs: list[TerraformRunConfig],
    action: str = "apply",
) -> list[TerraformResult]:
    """Execute multiple Terraform runs in parallel with progress display.

    Args:
        configs: List of TerraformRunConfig for each region.
        action: Either "apply" or "destroy". Determines which terraform
                command to run and the status label shown during execution.

    Runs up to MAX_CONCURRENT Terraform operations at once.
    Displays a live-updating table with spinners and elapsed time.
    On failure, prints the full error output for failed regions.
    Returns results and prints a summary of resources affected.
    """
    if action == "destroy":
        run_fn = run_terraform_destroy
        action_label = "destroying..."
    else:
        run_fn = run_terraform
        action_label = "applying..."

    console = Console()
    statuses: dict[str, str] = {}
    start_times: dict[str, float] = {}
    results: list[TerraformResult] = []
    spinner_tick = 0

    for config in configs:
        statuses[f"{config.provider}/{config.region}"] = "pending"

    with Live(
        _build_status_table(statuses, start_times, spinner_tick, action_label),
        console=console,
        refresh_per_second=4,
    ) as live:
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as pool:
            future_to_key = {}
            for config in configs:
                key = f"{config.provider}/{config.region}"
                statuses[key] = "running"
                start_times[key] = time.monotonic()
                future = pool.submit(run_fn, config)
                future_to_key[future] = key

            while future_to_key:
                # Update display with spinner animation
                spinner_tick += 1
                live.update(_build_status_table(statuses, start_times, spinner_tick, action_label))

                # Check for completed futures (non-blocking)
                done = set()
                for future in future_to_key:
                    if future.done():
                        done.add(future)

                for future in done:
                    key = future_to_key.pop(future)
                    result = future.result()
                    results.append(result)
                    statuses[key] = "done" if result.success else "failed"
                    live.update(
                        _build_status_table(statuses, start_times, spinner_tick, action_label)
                    )

                if future_to_key:
                    time.sleep(0.25)

    # Print resource summary
    total_added = 0
    total_changed = 0
    total_destroyed = 0
    for result in results:
        if result.success:
            counts = _parse_resource_summary(result.stdout)
            total_added += counts["added"]
            total_changed += counts["changed"]
            total_destroyed += counts["destroyed"]

    console.print()
    if total_added or total_changed or total_destroyed:
        console.print(
            f"[bold]Resources: {total_added} added, "
            f"{total_changed} changed, {total_destroyed} destroyed[/bold]"
        )

    # Print error details for any failures
    failures = [r for r in results if not r.success]
    if failures:
        console.print()
        for result in failures:
            console.print(f"[bold red]FAILED: {result.provider}/{result.region}[/bold red]")
            if result.stderr:
                console.print(result.stderr)
            if result.stdout:
                console.print(result.stdout)
            console.print()

    return results
