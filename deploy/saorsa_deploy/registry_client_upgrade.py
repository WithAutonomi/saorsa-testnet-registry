"""What a host-side service script needs on the VM besides the script itself.

`prod-continuous-uploader` is the one client kind whose service runs under the
`/opt/ant-registry-client` venv and writes Postgres itself, so replacing its
script is only half an upgrade:

* the venv's ``saorsa-db`` wheel was installed at provision time, and nothing
  else updates it -- not the operator's CLI, not an Alembic migration -- so a
  script calling a repository argument that wheel does not accept raises
  ``TypeError`` on the service's FIRST operation, a successful upload included,
  before the row is written and before the pool claim in hand is resolved;
* the columns that argument writes have to exist in the registry already, or
  the same operation dies in the ``INSERT`` instead.

Two commands install that script -- ``deployments upgrade-service-script`` and
``deployments upgrade-wasm-client``, which redeploys it alongside a new bundle
-- and a coupling this sharp held in only one of them is a trap for whoever
uses the other (INFRA-484 review). So it lives here, once, and both import it.

Everything returns a problem rather than exiting: one caller refuses
immediately, the other collects every problem in the invocation and reports
them together, because its build is ~10 minutes on a VM that bills.
"""

from __future__ import annotations

from pathlib import Path

from pyinfra.api import State
from sqlalchemy import inspect as sa_inspect

from saorsa_deploy.provisioning.saorsa_db_install import (
    SaorsaDbInstaller,
    build_saorsa_db_wheel,
)

#: Kinds whose service runs under the registry-client venv and talks to
#: Postgres, rather than under `/usr/bin/python3` importing nothing of ours.
#: The download kinds need neither the wheel nor the schema and get neither.
KINDS_WITH_REGISTRY_CLIENT = frozenset({"prod-continuous-uploader"})

#: Columns the upgraded uploader script writes that an older registry does not
#: have, as ``(table, column)``. Checked, never created: a migration is the
#: operator's step, and `alembic upgrade head` is a different command with a
#: different blast radius.
REQUIRED_REGISTRY_COLUMNS = (
    ("deployment_prod_upload_results", "error_kind"),
    ("deployment_upload_records", "error_kind"),
    ("deployment_upload_records", "reason"),
)


def needs_registry_client(kind: str) -> bool:
    return kind in KINDS_WITH_REGISTRY_CLIENT


def missing_registry_columns(repo) -> list[str]:
    """Which of ``REQUIRED_REGISTRY_COLUMNS`` the connected registry lacks.

    A table missing entirely is reported the same way a column is -- the
    distinction does not change what the operator has to run.
    """
    inspector = sa_inspect(repo.session.get_bind())
    tables = set(inspector.get_table_names())
    missing: list[str] = []
    for table, column in REQUIRED_REGISTRY_COLUMNS:
        if table not in tables:
            missing.append(f"{table} (table absent)")
            continue
        if column not in {c["name"] for c in inspector.get_columns(table)}:
            missing.append(f"{table}.{column}")
    return missing


def schema_problem(repo, kind: str) -> str | None:
    """A ready-made refusal for a registry the new script would die against."""
    if not needs_registry_client(kind):
        return None
    missing = missing_registry_columns(repo)
    if not missing:
        return None
    return (
        f"the registry this is pointed at is missing {', '.join(missing)}. A "
        f"'{kind}' VM writes Postgres itself, so the new script's first upload "
        "would fail in the INSERT -- while holding a pool claim. Run "
        "`cd db && uv run alembic upgrade head` against this registry first."
    )


def wheel_for_kinds(kinds) -> tuple[Path | None, str | None]:
    """``(wheel, problem)``: the wheel to install, built once, or why not.

    None for both when no named kind runs under the venv. The wheel is built
    from this workspace -- the same `SaorsaDbInstaller` seam the kind's own
    `deploy` uses, so the VM runs exactly the code the script came from -- which
    is why these kinds have to be upgraded from the repository checkout.
    """
    if not any(needs_registry_client(kind) for kind in kinds):
        return None, None
    try:
        return build_saorsa_db_wheel(), None
    except (FileNotFoundError, ValueError) as exc:
        return None, (
            f"{exc}\nA 'prod-continuous-uploader' VM runs its service under "
            "/opt/ant-registry-client, whose `saorsa-db` wheel has to be "
            "replaced alongside the script, and that wheel is built from this "
            "workspace. Run this command from the saorsa-testnet-registry "
            "checkout."
        )


def add_install_ops(state: State, wheel: Path, console=None) -> None:
    """Fold the wheel install into a pass, BEFORE the script is written.

    Order is the point: the script is what calls the new repository argument,
    so a restart that beat the wheel would raise on the service's first
    operation.
    """
    SaorsaDbInstaller(wheel_path=wheel, console=console).add_ops(state)
