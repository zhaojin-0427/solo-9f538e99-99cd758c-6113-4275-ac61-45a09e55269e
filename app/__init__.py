"""Flask application: CSV upload, firmware registry, device frame views."""

import os

from flask import Flask, flash, redirect, url_for

from . import db


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        DATA_DIR=os.environ.get("DATA_DIR", os.path.join(os.getcwd(), "data")),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        SECRET_KEY=os.environ.get("SECRET_KEY", "factory-frame-viewer"),
    )
    if test_config:
        app.config.update(test_config)
    app.config.setdefault(
        "DATABASE", os.path.join(app.config["DATA_DIR"], "frames.sqlite3")
    )
    os.makedirs(app.config["DATA_DIR"], exist_ok=True)
    os.makedirs(os.path.dirname(app.config["DATABASE"]) or ".", exist_ok=True)

    db.init_app(app)

    from .views import bp

    app.register_blueprint(bp)

    @app.errorhandler(413)
    def too_large(_err):
        flash("文件过大，请拆分后每批不超过 200 帧。", "error")
        return redirect(url_for("main.index"))

    return app
