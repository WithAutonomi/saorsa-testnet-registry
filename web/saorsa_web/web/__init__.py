from flask import Blueprint

from saorsa_web.web.deployments import bp as deployments_bp

web_bp = Blueprint("web", __name__)
web_bp.register_blueprint(deployments_bp)
