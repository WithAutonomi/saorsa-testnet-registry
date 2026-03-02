import logging
import os
import time

from sqlalchemy import create_engine

from saorsa_web.db import db_session, session_factory
from saorsa_web.github import GitHubAPIError, get_workflow_run
from saorsa_web.models.deployment import Deployment, DeploymentEvent, DeploymentStatus

logger = logging.getLogger(__name__)

# Map GitHub run status/conclusion to our DeploymentStatus and event_type
_STATUS_MAP = {
    "queued": (DeploymentStatus.RUNNING, "running"),
    "in_progress": (DeploymentStatus.RUNNING, "running"),
}

_CONCLUSION_MAP = {
    "success": (DeploymentStatus.RUNNING, "completed"),
    "failure": (DeploymentStatus.FAILED, "failed"),
    "cancelled": (DeploymentStatus.FAILED, "failed"),
}

# When a deployment is DESTROYING, workflow success means it's DESTROYED.
_DESTROY_CONCLUSION_MAP = {
    "success": (DeploymentStatus.DESTROYED, "destroyed"),
    "failure": (DeploymentStatus.FAILED, "failed"),
    "cancelled": (DeploymentStatus.FAILED, "failed"),
}


def _map_github_status(
    gh_run: dict,
    deployment_status: DeploymentStatus = DeploymentStatus.PENDING,
) -> tuple[DeploymentStatus, str] | None:
    """Map a GitHub workflow run to (DeploymentStatus, event_type).

    Uses *deployment_status* to pick the right conclusion map: deployments
    in DESTROYING state use _DESTROY_CONCLUSION_MAP so that a successful
    workflow completion becomes DESTROYED.  Normal deployments stay RUNNING
    after a successful workflow (the testnet is live).

    Returns None if the status/conclusion combination is not recognised.
    """
    status = gh_run.get("status")
    conclusion = gh_run.get("conclusion")

    if status in _STATUS_MAP and deployment_status != DeploymentStatus.DESTROYING:
        return _STATUS_MAP[status]

    if status == "completed":
        conclusion_map = (
            _DESTROY_CONCLUSION_MAP
            if deployment_status == DeploymentStatus.DESTROYING
            else _CONCLUSION_MAP
        )
        if conclusion in conclusion_map:
            return conclusion_map[conclusion]

    return None


def _poll_once() -> None:
    """Execute a single monitor tick: check all active deployments."""
    deployments = (
        db_session.query(Deployment)
        .filter(
            Deployment.status.in_(
                [
                    DeploymentStatus.PENDING,
                    DeploymentStatus.RUNNING,
                    DeploymentStatus.DESTROYING,
                ]
            ),
            Deployment.workflow_run_id.isnot(None),
        )
        .all()
    )

    if not deployments:
        logger.debug("No active deployments to monitor")
        return

    logger.info("Checking %d active deployment(s)", len(deployments))

    for deployment in deployments:
        try:
            gh_run = get_workflow_run(deployment.workflow_run_id)
        except GitHubAPIError:
            logger.error(
                "Failed to fetch workflow run %d for deployment %s",
                deployment.workflow_run_id,
                deployment.name,
                exc_info=True,
            )
            continue

        mapped = _map_github_status(gh_run, deployment.status)
        if mapped is None:
            logger.warning(
                "Unrecognised GitHub run state for deployment %s: "
                "status=%s conclusion=%s",
                deployment.name,
                gh_run.get("status"),
                gh_run.get("conclusion"),
            )
            continue

        new_status, event_type = mapped

        # Terminal events worth recording even when status doesn't change
        # (e.g. workflow "completed" while deployment stays RUNNING).
        _TERMINAL_EVENTS = {"completed", "destroyed", "failed"}

        if new_status == deployment.status:
            if event_type not in _TERMINAL_EVENTS:
                logger.debug(
                    "Deployment %s status unchanged (%s)",
                    deployment.name,
                    new_status.value,
                )
                continue
            already_recorded = any(
                e.event_type == event_type for e in deployment.events
            )
            if already_recorded:
                logger.debug(
                    "Deployment %s event %s already recorded",
                    deployment.name,
                    event_type,
                )
                continue

        old_status = deployment.status
        if new_status != deployment.status:
            deployment.status = new_status

        event = DeploymentEvent(
            deployment_id=deployment.id,
            event_type=event_type,
            details={
                "workflow_run_id": gh_run.get("id"),
                "html_url": gh_run.get("html_url"),
                "status": gh_run.get("status"),
                "conclusion": gh_run.get("conclusion"),
                "previous_status": old_status.value,
                "new_status": new_status.value,
            },
        )
        db_session.add(event)
        db_session.commit()

        logger.info(
            "Deployment %s transitioned from %s to %s (event: %s)",
            deployment.name,
            old_status.value,
            new_status.value,
            event_type,
        )


def run_monitor_loop(interval=60):
    """Monitor poll loop.

    Assumes the database connection is already configured (e.g. by the
    Flask app or by ``run_monitor``).
    """
    logger.info("Starting monitor loop with interval=%ds", interval)

    while True:
        try:
            logger.debug("Monitor tick")
            _poll_once()
        except Exception:
            logger.error("Error during monitor tick", exc_info=True)
        finally:
            db_session.remove()

        time.sleep(interval)


def run_monitor(interval=60):
    """Background status monitor — standalone entry point.

    Initialises its own database connection then enters the poll loop.
    Use ``run_monitor_loop`` when running inside a Flask app context
    where the database is already configured.
    """
    logger.info("Starting monitor with interval=%ds", interval)

    database_url = os.environ.get(
        "DATABASE_URL",
        "postgresql://saorsa_testnet:saorsa_testnet@localhost:5432/saorsa_testnet_registry",
    )
    engine = create_engine(database_url)
    session_factory.configure(bind=engine)
    logger.info("Database connection configured")

    run_monitor_loop(interval)
