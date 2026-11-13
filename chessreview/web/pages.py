"""HTML pages: each is a thin shell whose data comes from the JSON API."""

from flask import Blueprint, current_app, render_template
from flask.typing import ResponseReturnValue

from .. import db, library
from ..config import effective_workers
from ..engine import StockfishNotFound, find_stockfish
from .common import config, conn
from .games import account_json

bp = Blueprint("pages", __name__)


@bp.get("/")
def index() -> ResponseReturnValue:
    account = account_json(conn())
    return render_template("index.html", account=account, **account)


@bp.get("/favicon.ico")
def favicon() -> ResponseReturnValue:
    return current_app.send_static_file("favicon.svg")


@bp.get("/game/<int:game_id>")
def game_page(game_id: int) -> ResponseReturnValue:
    # An unknown id gets the "Game not found" state at once, without a failing API call.
    found = conn().execute("SELECT 1 FROM games WHERE id = ?", (game_id,)).fetchone() is not None
    return render_template("game.html", game_id=game_id, found=found)


@bp.get("/review")
def review_page() -> ResponseReturnValue:
    return render_template("review.html")


@bp.get("/games")
def games_page() -> ResponseReturnValue:
    cfg = config()
    return render_template("games.html", username=db.username(conn(), cfg))


@bp.get("/settings")
def settings_page() -> ResponseReturnValue:
    cfg = config()
    try:
        stockfish = find_stockfish(cfg.engine.path)
    except StockfishNotFound:
        stockfish = "not found (see the README to install it)"
    return render_template(
        "settings.html",
        cfg=cfg,
        workers=effective_workers(cfg.engine),
        account=account_json(conn()),
        stockfish=stockfish,
    )


@bp.get("/insights")
def insights_page() -> ResponseReturnValue:
    return render_template("insights.html")


@bp.get("/openings")
def openings_page() -> ResponseReturnValue:
    return render_template("openings.html")


@bp.get("/train")
def train_page() -> ResponseReturnValue:
    return render_template("train.html")


@bp.get("/session")
def session_page() -> ResponseReturnValue:
    return render_template("session.html")


@bp.get("/convert")
def convert_page() -> ResponseReturnValue:
    return render_template("convert.html")


@bp.get("/calculate")
def calculate_page() -> ResponseReturnValue:
    return render_template("calculate.html")


@bp.get("/spar")
def spar_page() -> ResponseReturnValue:
    return render_template("spar.html")


@bp.get("/progress")
def progress_page() -> ResponseReturnValue:
    return render_template("progress.html")


@bp.get("/puzzles")
def puzzles_page() -> ResponseReturnValue:
    return render_template("puzzles.html", themes=library.THEMES)
