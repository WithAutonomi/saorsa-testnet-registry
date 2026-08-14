"""Create the least-privilege registry role used by client VMs (V2-837).

Run once by the operator, against the admin ``DATABASE_URL``. Prints the
connection string the VMs should be given — which is what
``pool_database_url_env`` in a ``prod-continuous-uploader`` manifest names.

The URL is printed, never stored: like the wallet key, the manifest holds the
*name* of an environment variable, not its value.
"""

import os
import sys

from rich.console import Console
from sqlalchemy import text

from saorsa_deploy.pool_client_role import (
    READ_ONLY_TABLES,
    READ_WRITE_TABLES,
    PoolClientRoleError,
    build_client_url,
    build_grant_statements,
    build_revoke_statements,
    generate_password,
    validate_role_name,
)

DEFAULT_ROLE = "ant_pool_client"


def _admin_url(console: Console) -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        console.print(
            "[bold red]Error:[/bold red] No DATABASE_URL configured. Creating "
            "a role needs an administrative connection to the registry."
        )
        sys.exit(1)
    return url


def cmd_pool_client_role(args) -> None:
    console = Console()
    role = args.role or DEFAULT_ROLE
    try:
        validate_role_name(role)
    except PoolClientRoleError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)

    admin_url = _admin_url(console)

    from sqlalchemy import create_engine

    engine = create_engine(admin_url)

    if args.revoke:
        console.print(f"Dropping role [bold]{role}[/bold]...")
        with engine.begin() as conn:
            for statement in build_revoke_statements(role):
                conn.execute(text(statement))
        console.print(f"[green]Role {role} removed.[/green]")
        return

    password = args.password or generate_password()
    try:
        statements = build_grant_statements(role, password)
    except PoolClientRoleError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(1)

    if args.dry_run:
        console.print("[bold]Statements that would run:[/bold]")
        for statement in statements:
            # Never echo the password, even on a dry run — terminals scroll
            # into pastes and tickets.
            console.print(f"  {statement.replace(password, '<password>')}")
        return

    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))

    client_url = build_client_url(admin_url, role, password)

    console.print(f"[green]Role [bold]{role}[/bold] created/updated.[/green]")
    console.print()
    console.print("[bold]Granted:[/bold]")
    console.print(f"  SELECT               {', '.join(READ_ONLY_TABLES)}")
    console.print(f"  SELECT/INSERT/UPDATE {', '.join(READ_WRITE_TABLES)}")
    console.print("  [dim]no DELETE, no DDL, nothing else in the registry[/dim]")
    console.print()
    console.print("[bold]Connection string for the client VMs:[/bold]")
    console.print(f"  {client_url}")
    console.print()
    console.print(
        "Export it as [bold]POOL_DATABASE_URL[/bold] (or whatever the "
        "manifest's [bold]pool_database_url_env[/bold] names) before running "
        "[bold]deployments up[/bold]. It is not stored anywhere — re-run this "
        "command with [bold]--password[/bold] to set a known value, or without "
        "to rotate."
    )
