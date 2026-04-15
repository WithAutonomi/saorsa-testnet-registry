from flask import Blueprint, jsonify

from saorsa_db.models import Deployment, DeploymentEvent
from saorsa_web.db import db_session

bp = Blueprint("api_deployments", __name__, url_prefix="/deployments")


@bp.route("", methods=["GET"])
def list_deployments():
    deployments = (
        db_session.query(Deployment).order_by(Deployment.created_at.desc()).all()
    )
    return jsonify([d.to_dict() for d in deployments])


@bp.route("/<int:deployment_id>", methods=["GET"])
def get_deployment(deployment_id):
    deployment = db_session.get(Deployment, deployment_id)
    if deployment is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify(deployment.to_dict())


@bp.route("/<int:deployment_id>/events", methods=["GET"])
def list_deployment_events(deployment_id):
    deployment = db_session.get(Deployment, deployment_id)
    if deployment is None:
        return jsonify({"error": "Not found"}), 404

    events = (
        db_session.query(DeploymentEvent)
        .filter(DeploymentEvent.deployment_id == deployment_id)
        .order_by(DeploymentEvent.created_at)
        .all()
    )
    return jsonify([e.to_dict() for e in events])
