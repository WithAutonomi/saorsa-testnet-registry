"""Optional deployment registry integration.

Records deployment lifecycle events to a PostgreSQL database when DATABASE_URL
is set. When the database is unavailable, all operations are silent no-ops so
that saorsa-deploy can still function as a standalone CLI.
"""

import logging
import os
from datetime import datetime
from importlib.metadata import version

from saorsa_db.db import get_session_factory
from saorsa_db.models import Deployment, DeploymentStatus
from saorsa_db.models.prod_service_audit import ProdServiceAuditAction
from saorsa_db.repositories import (
    DeploymentRepository,
    ProdFilePoolRepository,
    ProdFleetRepository,
    SqlAlchemyDeploymentRepository,
    SqlAlchemyProdFilePoolRepository,
    SqlAlchemyProdFleetRepository,
)

logger = logging.getLogger(__name__)

#: Whether the "could not read the tool version" warning has already been
#: emitted. One warning per process: a `deployments up` run creates one row, but
#: the same import is live for every recording call that follows it.
_TOOL_VERSION_WARNED = False


def _try_get_repository() -> DeploymentRepository | None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return None
    try:
        factory = get_session_factory(database_url)
        session = factory()
        return SqlAlchemyDeploymentRepository(session)
    except Exception as exc:
        # Loud, not debug: callers report None as "DATABASE_URL is not set",
        # and on 2026-10-09 a VM whose venv lacked the psycopg driver printed
        # exactly that with the variable set, sending the diagnosis the wrong
        # way. The real reason must reach the journal.
        logger.warning("Could not connect to the registry database: %s", exc)
        return None


def get_repository() -> DeploymentRepository | None:
    return _try_get_repository()


def get_file_pool_repository() -> ProdFilePoolRepository | None:
    """Repository for the production upload file pool.

    Returns None when DATABASE_URL is unset or unreachable, like every other
    accessor here — but callers must think harder about that case than usual.
    The pool is the mechanism that stops a file being uploaded twice, so a
    pool-backed deploy cannot silently proceed without it the way the old
    name-based duplicate check did: it must refuse and say why.
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return None
    try:
        factory = get_session_factory(database_url)
        return SqlAlchemyProdFilePoolRepository(factory())
    except Exception:
        logger.debug("Could not connect to registry database", exc_info=True)
        return None


def _tool_version() -> str | None:
    """The running `saorsa-deploy` version, or None if it cannot be read.

    antops pins a tool version per job, so the row has to be able to name the
    version that started the run. There is no sane fallback for a tree whose
    metadata is missing — a guess would be worse than a null, because a wrong
    version is read as a fact — so the column is left null and the reason is
    warned about once per process.
    """
    global _TOOL_VERSION_WARNED
    try:
        return version("saorsa-deploy")
    except Exception as exc:  # noqa: BLE001 - any metadata failure is the same answer
        if not _TOOL_VERSION_WARNED:
            _TOOL_VERSION_WARNED = True
            logger.warning(
                "Could not read the saorsa-deploy version (%s); recording the "
                "deployment without one",
                exc,
            )
        return None


def record_deployment_created(
    name: str,
    deploy_args: dict,
    repo: DeploymentRepository | None = None,
) -> int | None:
    repo = repo or get_repository()
    if repo is None:
        return None
    try:
        # Written at creation only. Every command that re-provisions, resumes or
        # records against an existing row goes through `record_event` and friends,
        # which never touch this column, so the row keeps naming the version that
        # STARTED the run rather than the last one to touch it.
        deployment = repo.create(
            name=name,
            deploy_args=deploy_args,
            saorsa_deploy_version=_tool_version(),
        )
        logger.info("Recorded deployment '%s' (id=%d) in registry", name, deployment.id)
        return deployment.id
    except Exception:
        logger.warning("Failed to record deployment in registry", exc_info=True)
        return None


def _find_deployment(name: str, repo: DeploymentRepository) -> Deployment | None:
    """Find the best matching deployment: prefer active, fall back to most recent."""
    deployment = repo.get_active_by_name(name)
    if deployment is None:
        deployment = repo.get_by_name(name)
    return deployment


def record_event(
    name: str,
    event_type: str,
    details: dict | None = None,
    status: DeploymentStatus | None = None,
    repo: DeploymentRepository | None = None,
) -> None:
    repo = repo or get_repository()
    if repo is None:
        return
    try:
        deployment = _find_deployment(name, repo)
        if deployment is None:
            logger.debug("Deployment '%s' not found in registry, skipping event", name)
            return
        if status is not None:
            repo.update_status(deployment.id, status)
        repo.add_event(deployment.id, event_type, details)
    except Exception:
        logger.warning("Failed to record event in registry", exc_info=True)


def record_status(
    name: str,
    status: DeploymentStatus,
    repo: DeploymentRepository | None = None,
) -> None:
    repo = repo or get_repository()
    if repo is None:
        return
    try:
        deployment = _find_deployment(name, repo)
        if deployment is None:
            logger.debug("Deployment '%s' not found in registry, skipping status update", name)
            return
        repo.update_status(deployment.id, status)
    except Exception:
        logger.warning("Failed to update status in registry", exc_info=True)


def record_deploy_args_update(
    name: str,
    updates: dict,
    repo: DeploymentRepository | None = None,
) -> None:
    """Shallow-merge ``updates`` into the deployment's ``deploy_args``.

    Silently no-ops when DATABASE_URL is unset or the deployment is not
    found — must not break the caller's deploy flow.
    """
    repo = repo or get_repository()
    if repo is None:
        return
    try:
        deployment = _find_deployment(name, repo)
        if deployment is None:
            logger.debug(
                "Deployment '%s' not found in registry, skipping deploy_args update",
                name,
            )
            return
        repo.merge_deploy_args(deployment.id, updates)
    except Exception:
        logger.warning("Failed to merge deploy_args in registry", exc_info=True)


def get_prod_fleet_repository() -> ProdFleetRepository | None:
    """Repository for standalone production-fleet records (disk surveys +
    service audit), keyed by inventory name. Returns None when DATABASE_URL is
    unset or unreachable — callers must degrade to no-DB, since the
    ``prod services`` commands run standalone off inventory.json."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return None
    try:
        factory = get_session_factory(database_url)
        return SqlAlchemyProdFleetRepository(factory())
    except Exception:
        logger.debug("Could not connect to registry database", exc_info=True)
        return None


def record_service_audit(
    inventory_name: str,
    host: str,
    service_name: str,
    action: ProdServiceAuditAction,
    occurred_at: datetime,
    size_bytes: int | None = None,
    note: str | None = None,
    repo: ProdFleetRepository | None = None,
) -> None:
    """Best-effort audit row for a `prod services` add/remove/reset. Silent
    no-op when DATABASE_URL is unset; warns (never raises) on error, since the
    live SSH change has already succeeded by the time this runs."""
    repo = repo or get_prod_fleet_repository()
    if repo is None:
        return
    try:
        repo.record_service_audit(
            inventory_name=inventory_name,
            host=host,
            service_name=service_name,
            action=action,
            occurred_at=occurred_at,
            size_bytes=size_bytes,
            note=note,
        )
    except Exception:
        logger.warning("Failed to record service audit in registry", exc_info=True)


def record_disk_survey(
    inventory_name: str,
    surveyed_at: datetime,
    services: list[dict],
    host_disk: dict,
    failed_hosts: dict | None = None,
    attempted_host_count: int | None = None,
    repo: ProdFleetRepository | None = None,
) -> int | None:
    """Best-effort persist of a disk survey; returns the new survey id or None.
    Silent no-op when DATABASE_URL is unset; warns (never raises) on error.

    ``failed_hosts`` records which hosts the survey could not reach and why, so
    the totals are readable as short rather than as a reclaim (V2-1147)."""
    repo = repo or get_prod_fleet_repository()
    if repo is None:
        return None
    try:
        survey = repo.create_disk_survey(
            inventory_name=inventory_name,
            surveyed_at=surveyed_at,
            services=services,
            host_disk=host_disk,
            failed_hosts=failed_hosts,
            attempted_host_count=attempted_host_count,
        )
        return survey.id
    except Exception:
        logger.warning("Failed to record disk survey in registry", exc_info=True)
        return None
