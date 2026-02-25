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
    "success": (DeploymentStatus.STOPPED, "completed"),
    "failure": (DeploymentStatus.FAILED, "failed"),
    "cancelled": (DeploymentStatus.FAILED, "failed"),
}


def _map_github_status(gh_run: dict) -> tuple[DeploymentStatus, str] | None:
    """Map a GitHub workflow run to (DeploymentStatus, event_type).

    Returns None if the status/conclusion combination is not recognised.
    """
    status = gh_run.get("status")
    conclusion = gh_run.get("conclusion")

    if status in _STATUS_MAP:
        return _STATUS_MAP[status]

    if status == "completed" and conclusion in _CONCLUSION_MAP:
        return _CONCLUSION_MAP[conclusion]

    return None


def _poll_once() -> None:
    """Execute a single monitor tick: check all active deployments."""
    deployments = (
        db_session.query(Deployment)
        .filter(
            Deployment.status.in_([DeploymentStatus.PENDING, DeploymentStatus.RUNNING]),
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

        mapped = _map_github_status(gh_run)
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

        if new_status == deployment.status:
            logger.debug(
                "Deployment %s status unchanged (%s)", deployment.name, new_status.value
            )
            continue

        old_status = deployment.status
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


def run_monitor(interval=60):
    """Background status monitor loop.

    Polls GitHub Actions for workflow run status and updates deployment
    records in the database.  Runs as a standalone worker outside Flask,
    so it initialises its own database connection.
    """
    logger.info("Starting monitor with interval=%ds", interval)

    # Initialise the database connection for the standalone worker.
    database_url = os.environ.get(
        "DATABASE_URL",
        "postgresql://saorsa_testnet:saorsa_testnet@localhost:5432/saorsa_testnet_registry",
    )
    engine = create_engine(database_url)
    session_factory.configure(bind=engine)
    logger.info("Database connection configured")

    while True:
        try:
            logger.debug("Monitor tick")
            _poll_once()
        except Exception:
            logger.error("Error during monitor tick", exc_info=True)
        finally:
            db_session.remove()

        time.sleep(interval)
