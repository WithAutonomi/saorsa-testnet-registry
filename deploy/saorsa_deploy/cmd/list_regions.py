from rich.console import Console
from rich.table import Table

from saorsa_deploy.providers import PROVIDERS


def cmd_list_regions(args):
    """Print the regions supported for each cloud provider."""
    console = Console()

    table = Table(show_header=True, header_style="bold")
    table.add_column("Provider")
    table.add_column("Default")
    table.add_column("Regions")

    for name in sorted(PROVIDERS):
        provider = PROVIDERS[name]
        table.add_row(
            provider.name,
            provider.default_region,
            ", ".join(provider.regions),
        )

    console.print(table)
