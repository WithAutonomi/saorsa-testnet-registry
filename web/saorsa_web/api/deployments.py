import logging

from flask import Blueprint, current_app, jsonify, request

from saorsa_web.db import db_session
from saorsa_web.github import GitHubAPIError, dispatch_workflow
from saorsa_web.models.deployment import Deployment, DeploymentEvent, DeploymentStatus

logger = logging.getLogger(__name__)

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
    if not data:
        return jsonify({"error": "Request body required"}), 400

    name = data.get("name")
    if not name or not isinstance(name, str) or not name.strip():
        return jsonify({"error": "name is required and must be a non-empty string"}), 400

    deploy_args = data.get("deploy_args")
    if deploy_args is None or not isinstance(deploy_args, dict):
        return jsonify({"error": "deploy_args is required and must be a dict"}), 400

    branch = data.get("branch", "main")

    try:
        workflow_run_id = dispatch_workflow(
            deploy_args,
            pat=current_app.config["WORKFLOW_RUNNER_PAT"],
            repo=current_app.config["GITHUB_REPO"],
            workflow_file=current_app.config["WORKFLOW_FILE"],
            branch=branch,
        )
    except GitHubAPIError as exc:
        logger.error("GitHub workflow dispatch failed: %s", exc)
        return jsonify({"error": str(exc)}), 502

    deployment = Deployment(
        name=name.strip(),
        deploy_args=deploy_args,
        workflow_run_id=workflow_run_id,
        status=DeploymentStatus.PENDING,
    )
    db_session.add(deployment)
    db_session.flush()

    event = DeploymentEvent(
        deployment_id=deployment.id,
        event_type="created",
        details={"workflow_run_id": workflow_run_id},
    )
    db_session.add(event)
    db_session.commit()

    result = deployment.to_dict()
    result.pop("deploy_args", None)
    return jsonify(result), 201


@bp.route("/<int:deployment_id>", methods=["PUT"])
def update_deployment(deployment_id):
    deployment = db_session.get(Deployment, deployment_id)
    if deployment is None:
        return jsonify({"error": "Not found"}), 404

    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body required"}), 400

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
