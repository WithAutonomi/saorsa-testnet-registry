from flask import Blueprint, jsonify, request

from saorsa_web.db import db_session
from saorsa_web.models.deployment import Deployment, DeploymentStatus

bp = Blueprint("api_deployments", __name__, url_prefix="/deployments")


@bp.route("", methods=["GET"])
def list_deployments():
    deployments = db_session.query(Deployment).order_by(Deployment.created_at.desc()).all()
    return jsonify([d.to_dict() for d in deployments])


@bp.route("/<int:deployment_id>", methods=["GET"])
def get_deployment(deployment_id):
    deployment = db_session.get(Deployment, deployment_id)
    if deployment is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify(deployment.to_dict())


@bp.route("", methods=["POST"])
def create_deployment():
    data = request.get_json()
    if not data or "name" not in data:
        return jsonify({"error": "name is required"}), 400

    status_str = data.get("status", "pending")
    try:
        status = DeploymentStatus(status_str)
    except ValueError:
        return jsonify({"error": f"Invalid status: {status_str}"}), 400

    deployment = Deployment(name=data["name"], status=status)
    db_session.add(deployment)
    db_session.commit()
    return jsonify(deployment.to_dict()), 201


@bp.route("/<int:deployment_id>", methods=["PUT"])
def update_deployment(deployment_id):
    deployment = db_session.get(Deployment, deployment_id)
    if deployment is None:
        return jsonify({"error": "Not found"}), 404

    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

    if "name" in data:
        deployment.name = data["name"]

    if "status" in data:
        try:
            deployment.status = DeploymentStatus(data["status"])
        except ValueError:
            return jsonify({"error": f"Invalid status: {data['status']}"}), 400

    db_session.commit()
    return jsonify(deployment.to_dict())


@bp.route("/<int:deployment_id>", methods=["DELETE"])
def delete_deployment(deployment_id):
    deployment = db_session.get(Deployment, deployment_id)
    if deployment is None:
        return jsonify({"error": "Not found"}), 404

    db_session.delete(deployment)
    db_session.commit()
    return "", 204
