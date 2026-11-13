"""Flask web app: pages are thin HTML shells; their data comes from the JSON API in the blueprints.

Blueprints by area: pages (HTML), games (account, import, analysis), practice (my mistakes as puzzles),
training (Today, sessions, Insights, Progress, Openings), play (Convert, Spar, Calculate) and settings.
"""

from flask import Flask, Response, current_app, jsonify, request
from werkzeug.exceptions import HTTPException

from ..config import Config, load_config
from ..engine import AnalysisJob
from ..live import LiveEngine
from . import games, pages, play, practice, settings, training
from .common import AppState, close_conn

BLUEPRINTS = (pages.bp, games.bp, practice.bp, training.bp, play.bp, settings.bp)


def create_app(cfg: Config | None = None) -> Flask:
    """The web app for `cfg` (default: load_config()). Call app.extensions["live"].close() when done."""
    cfg = cfg or load_config()
    app = Flask(__name__.rsplit(".", 1)[0])  # the package: templates/ and static/ sit beside the modules
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB of PGN is plenty
    app.config["TEMPLATES_AUTO_RELOAD"] = True  # edited pages show up without a server restart

    state = AppState(cfg=cfg, live=LiveEngine(cfg), job=AnalysisJob(cfg))
    app.extensions["chessreview"] = state
    app.extensions["live"] = state.live  # closed by the `serve` command on shutdown
    app.teardown_appcontext(close_conn)
    app.register_error_handler(HTTPException, http_error)
    app.register_error_handler(Exception, server_error)
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)
    return app


def http_error(exc: HTTPException) -> HTTPException | tuple[Response, int]:
    """API errors are JSON ({error}) so pages can say what went wrong; pages keep HTML."""
    if request.path.startswith("/api/"):
        return jsonify(error=exc.description or exc.name), exc.code or 500
    return exc


def server_error(exc: Exception) -> tuple[Response | str, int]:
    """Log an unexpected error; API callers get its type and message as JSON."""
    current_app.logger.exception("Unexpected error in %s", request.path)
    if request.path.startswith("/api/"):
        return jsonify(error=f"Something went wrong on the server ({type(exc).__name__}: {exc})."), 500
    return "Something went wrong on the server. The details are in the terminal running Chess Review.", 500
