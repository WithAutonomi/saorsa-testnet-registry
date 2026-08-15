import sys
from datetime import datetime, timedelta, timezone

from rich.console import Console
from rich.table import Table
from saorsa_db.models import DeploymentStatus

from saorsa_deploy.registry import get_repository

_STATUS_STYLES = {
    DeploymentStatus.PENDING: "yellow",
    DeploymentStatus.RUNNING: "green",
    DeploymentStatus.STOPPED: "cyan",
    DeploymentStatus.FAILED: "bold red",
    DeploymentStatus.DESTROYING: "yellow",
    DeploymentStatus.DESTROYED: "dim",
}

_RECENT_WINDOW = timedelta(days=7)

# Display labels keyed by the underlying ``deploy_args["kind"]`` value.
# The values stored in the database stay as the canonical type names; only
# the rendered ``Kind`` cell is reformatted.
_KIND_LABELS = {
    "testnet": "Testnet",
    "prod-uploaders": "Prod Uploads",
    "prod-continuous-downloader": "Prod Downloads",
    "prod-downloaders": "Prod Downloads [One-Shot]",
    "prod-local-uploads": "Prod Uploads [Home]",
    "prod-continuous-uploader": "Prod Daily Write Test",
}

_TESTNET_KIND = "testnet"

# Kinds that run ant-node services, and so have a meaningful node version.
# Every other kind is client-only: it records the *client* binary as
# ``ant_binary`` and never writes ``node_binary``.
_NODE_BEARING_KINDS = {
    _TESTNET_KIND,
    "testnet-extension",
    "prod-nodes",
    "prod-debug-nodes",
}

# Branch labels can be far longer than a table cell can carry (real examples run
# to 50+ characters, e.g.
# ``WithAutonomi/debug/audit-type-and-storage-commitment-timeout-logging``).
# Truncate rather than let Rich wrap the cell and make every row two lines tall.
_BRANCH_LABEL_MAX = 28


def _derive_raw_kind(deploy_args: dict) -> str:
    if not isinstance(deploy_args, dict):
        return ""
    explicit = deploy_args.get("kind")
    if explicit:
        return explicit
    # Regular testnets recorded by bootstraps infra don't carry an explicit
    # kind. Recognise them by the presence of bootstrap_count.
    if "bootstrap_count" in deploy_args:
        return _TESTNET_KIND
    return ""


def _derive_kind(deploy_args: dict) -> str:
    raw = _derive_raw_kind(deploy_args)
    if not raw:
        return ""
    return _KIND_LABELS.get(raw, raw)


def _derive_node_count(deploy_args: dict) -> str:
    # Node count only applies to testnet deployments. Anything else
    # (prod runs, unknown kind) renders as ``N/A`` so the column is never
    # ambiguous about whether the value is missing or non-applicable.
    #
    # Total node services = vm_count * nodes_per_vm. ``vm_count`` is written
    # by `nodes infra`; ``nodes_per_vm`` is written by `nodes provision`.
    # Pre-fix legacy rows lack one or both fields and render as N/A.
    if _derive_raw_kind(deploy_args) != _TESTNET_KIND:
        return "N/A"
    if not isinstance(deploy_args, dict):
        return "N/A"
    vm_count = deploy_args.get("vm_count")
    nodes_per_vm = deploy_args.get("nodes_per_vm")
    if vm_count is None or nodes_per_vm is None:
        return "N/A"
    return str(vm_count * nodes_per_vm)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _derive_node_version(deploy_args: dict) -> str:
    # The ant-node binary a deployment ran, from the descriptor written by
    # `nodes provision` / `prod nodes deploy` / `prod debug-nodes deploy` (see
    # ``binary_descriptor.build_binary_descriptor``).
    #
    # ``N/A`` covers both "no node tier in this kind" and "node_binary was never
    # recorded" — matching _derive_node_count, which likewise renders N/A for a
    # non-applicable kind and for a testnet missing its counts. Legacy rows
    # predating the deploy_args write fall into the latter case; `ls` reads only
    # Postgres, so there is deliberately no S3-state fallback here.
    if _derive_raw_kind(deploy_args) not in _NODE_BEARING_KINDS:
        return "N/A"
    if not isinstance(deploy_args, dict):
        return "N/A"
    descriptor = deploy_args.get("node_binary")
    if not isinstance(descriptor, dict):
        return "N/A"
    kind = descriptor.get("kind")
    if kind == "release":
        version = descriptor.get("version")
        return str(version) if version else "N/A"
    if kind == "branch":
        branch = descriptor.get("branch")
        if not branch:
            return "N/A"
        # The owner is load-bearing: the same branch name commonly exists on
        # several forks (jacderida / WithAutonomi / grumbach).
        owner = descriptor.get("repo_owner") or "?"
        return _truncate(f"{owner}/{branch}", _BRANCH_LABEL_MAX)
    if kind == "latest":
        return "latest"
    return "N/A"


def _filter_by_name(deployments: list, substring: str) -> list:
    needle = substring.lower()
    return [d for d in deployments if needle in d.name.lower()]


def _filter_recent(deployments: list) -> list:
    cutoff = datetime.now(timezone.utc) - _RECENT_WINDOW
    out = []
    for d in deployments:
        created = d.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created >= cutoff:
            out.append(d)
    return out


def cmd_ls(args):
    """List deployments registered in the database."""
    console = Console(width=140)

    repo = get_repository()
    if repo is None:
        console.print(
            "[bold red]Error:[/bold red] DATABASE_URL is not set or the database "
            "is unreachable."
        )
        sys.exit(1)

    if getattr(args, "active", False):
        deployments = repo.list_active()
    else:
        deployments = repo.list_all()

    status_filter = getattr(args, "status", None)
    if status_filter:
        try:
            wanted = DeploymentStatus(status_filter)
        except ValueError:
            valid = ", ".join(s.value for s in DeploymentStatus)
            console.print(
                f"[bold red]Error:[/bold red] Unknown status '{status_filter}'. "
                f"Valid values: {valid}."
            )
            sys.exit(1)
        deployments = [d for d in deployments if d.status == wanted]

    name_filter = getattr(args, "name", None)
    if name_filter:
        deployments = _filter_by_name(deployments, name_filter)

    if getattr(args, "recent", False):
        deployments = _filter_recent(deployments)

    ids_with_results = repo.deployment_ids_with_results()

    if getattr(args, "has_results", False):
        deployments = [d for d in deployments if d.id in ids_with_results]

    if not deployments:
        console.print("[dim]No deployments recorded.[/dim]")
        return

    table = Table(title=f"Deployments ({len(deployments)})")
    table.add_column("ID", justify="right")
    table.add_column("Name")
    table.add_column("Kind")
    table.add_column("Status")
    table.add_column("Nodes", justify="right")
    table.add_column("Node Version")
    table.add_column("Has Results")
    table.add_column("Created")

    for d in deployments:
        style = _STATUS_STYLES.get(d.status, "")
        status_cell = f"[{style}]{d.status.value}[/]" if style else d.status.value
        has_results = (
            "[green]yes[/green]" if d.id in ids_with_results else "[dim]no[/dim]"
        )
        table.add_row(
            str(d.id),
            d.name,
            _derive_kind(d.deploy_args),
            status_cell,
            _derive_node_count(d.deploy_args),
            _derive_node_version(d.deploy_args),
            has_results,
            d.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        )

    console.print(table)
