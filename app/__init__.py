"""Flask 应用工厂。"""
from __future__ import annotations

import os

from flask import Flask

from . import db
from .routes import bp


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SETTLE_SECRET", "dev-secret-change-me")
    app.config["MAX_CONTENT_LENGTH"] = 22 * 1024 * 1024
    db.init_db()
    app.register_blueprint(bp)
    return app
