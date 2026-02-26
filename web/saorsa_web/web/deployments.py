from flask import Blueprint, render_template

from saorsa_web.db import db_session
from saorsa_web.models.deployment import Deployment

bp = Blueprint("web_deployments", __name__, url_prefix="/deployments")


@bp.route("")
def list_deployments():
    deployments = (
        db_session.query(Deployment).order_by(Deployment.created_at.desc()).all()
    )
    return render_template("deployments/list.html", deployments=deployments)
