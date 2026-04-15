from flask import Blueprint, abort, render_template, request

from saorsa_db.models import Deployment, DeploymentEvent, DeploymentStatus
from saorsa_web.db import db_session

bp = Blueprint("web_deployments", __name__, url_prefix="/deployments")


@bp.route("")
def list_deployments():
    deployments = (
        db_session.query(Deployment).order_by(Deployment.created_at.desc()).all()
    )
    if request.headers.get("HX-Request"):
        return render_template("deployments/_rows.html", deployments=deployments)
    return render_template("deployments/list.html", deployments=deployments)


@bp.route("/<int:deployment_id>")
def get_deployment(deployment_id):
    deployment = db_session.query(Deployment).get(deployment_id)
    if not deployment:
        abort(404)
    return render_template("deployments/detail.html", deployment=deployment)


_MANUALLY_DESTROYABLE = {
    DeploymentStatus.RUNNING,
    DeploymentStatus.STOPPED,
    DeploymentStatus.FAILED,
}


@bp.route("/<int:deployment_id>/mark-destroyed", methods=["POST"])
def mark_destroyed(deployment_id):
    deployment = db_session.query(Deployment).get(deployment_id)
    if not deployment:
        abort(404)
    if deployment.status not in _MANUALLY_DESTROYABLE:
        abort(400)

    old_status = deployment.status.value
    deployment.status = DeploymentStatus.DESTROYED
    event = DeploymentEvent(
        deployment_id=deployment.id,
        event_type="manually_destroyed",
        details={"previous_status": old_status},
    )
    db_session.add(event)
    db_session.commit()

    if request.headers.get("HX-Request"):
        return render_template("deployments/_row.html", deployment=deployment)
    return render_template("deployments/detail.html", deployment=deployment)
