from flask import Blueprint

from saorsa_web.api.deployments import bp as deployments_bp

api_bp = Blueprint("api", __name__, url_prefix="/api")
api_bp.register_blueprint(deployments_bp)
