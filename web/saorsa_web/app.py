import os

from flask import Flask, redirect

from saorsa_web.api import api_bp
from saorsa_web.config import Config
from saorsa_web.db import init_db
from saorsa_web.web import web_bp


def create_app(config=None):
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates"),
    )
    app.config.from_object(Config)
    if config:
        app.config.from_mapping(config)

    init_db(app)

    app.register_blueprint(api_bp)
    app.register_blueprint(web_bp)

    @app.route("/")
    def index():
        return redirect("/deployments")

    return app
