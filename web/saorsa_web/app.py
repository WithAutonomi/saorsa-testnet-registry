import logging
import os
import threading
from importlib.metadata import version

from flask import Flask, redirect
from werkzeug.middleware.proxy_fix import ProxyFix

from saorsa_web.api import api_bp
from saorsa_web.config import Config
from saorsa_web.db import init_db
from saorsa_web.web import web_bp
from saorsa_web.web.auth import auth_bp, configure_oauth, get_current_user

logger = logging.getLogger(__name__)


def create_app(config=None):
    app = Flask(
        __name__,
        template_folder=os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates"
        ),
    )
    app.config.from_object(Config)
    if config:
        app.config.from_mapping(config)

    if not app.config.get("SECRET_KEY") and not app.testing:
        raise RuntimeError("FLASK_SECRET_KEY environment variable must be set")

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    if app.debug or app.testing:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )

    init_db(app)
    configure_oauth(app)

    app.register_blueprint(api_bp)
    app.register_blueprint(web_bp)
    app.register_blueprint(auth_bp)

    if app.debug and not app.testing:
        _start_monitor_thread()

    @app.context_processor
    def inject_globals():
        return {
            "current_user": get_current_user(),
            "app_version": version("saorsa-web"),
        }

    @app.route("/")
    def index():
        return redirect("/deployments")

    return app


def _start_monitor_thread():
    """Start the deployment monitor as a background daemon thread.

    Only starts in the reloader child process (WERKZEUG_RUN_MAIN=true)
    to avoid running the monitor twice during debug mode.
    """
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return

    from saorsa_web.monitor import run_monitor_loop

    thread = threading.Thread(
        target=run_monitor_loop,
        kwargs={"interval": 60},
        daemon=True,
        name="deployment-monitor",
    )
    thread.start()
    logger.info("Started deployment monitor background thread")
