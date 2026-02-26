from importlib.metadata import version

from flask import Blueprint, jsonify
from sqlalchemy import text

from saorsa_web.api.deployments import bp as deployments_bp
from saorsa_web.db import db_session

api_bp = Blueprint("api", __name__, url_prefix="/api")
api_bp.register_blueprint(deployments_bp)


@api_bp.route("/version")
def get_version():
    return jsonify({"version": version("saorsa-web")})


@api_bp.route("/health")
def health():
    try:
        db_session.execute(text("SELECT 1"))
        return jsonify({"status": "ok"})
    except Exception:
        return jsonify({"status": "error"}), 503
