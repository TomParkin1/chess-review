"""Shared by the blueprints: the app's state, the database connection and request parsing."""

import sqlite3
import threading
from dataclasses import dataclass, field

import chess
from flask import current_app, g, request
from werkzeug.exceptions import BadRequest

from .. import db, library
from ..config import Config
from ..engine import AnalysisJob
from ..live import LiveEngine


@dataclass
class AppState:
    """Everything the requests of one app share (kept in app.extensions["chessreview"])."""

    cfg: Config
    live: LiveEngine  # long-lived Stockfish processes; `serve` closes them on shutdown
    job: AnalysisJob  # the current (or last) analysis job
    library: library.FetchProgress = field(default_factory=library.FetchProgress)
    library_thread: threading.Thread | None = None
    import_lock: threading.Lock = field(default_factory=threading.Lock)  # one import, sync, restore or Delete all
    job_lock: threading.Lock = field(default_factory=threading.Lock)  # checking and starting the job is one step


def app_state() -> AppState:
    """The running app's shared state."""
    return current_app.extensions["chessreview"]


def config() -> Config:
    """The running app's configuration."""
    return app_state().cfg


def live_engine() -> LiveEngine:
    """The running app's long-lived Stockfish processes."""
    return app_state().live


def conn() -> sqlite3.Connection:
    """This request's database connection (opened on first use, closed when the request ends)."""
    if "db" not in g:
        g.db = db.connect(config().db_path)
    return g.db


def close_conn(_exc: BaseException | None = None) -> None:
    """Close the request's connection, if one was opened."""
    c = g.pop("db", None)
    if c is not None:
        c.close()


REVIEW_RESULTS = {"solved", "solved_with_help", "failed", "skipped"}


def json_body() -> dict:
    """The request's JSON object; a missing, broken or non-object body is a 400."""
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        raise BadRequest("Expected a JSON object in the request body.")
    return data


def int_field(data: dict, key: str, default: int | None = None) -> int:
    """data[key] as an int (or `default` when it is missing); anything else is a 400."""
    value = data.get(key, default)
    if value is None:
        raise BadRequest(f"{key} is required.")
    try:
        return int(value)
    except TypeError, ValueError:
        raise BadRequest(f"{key} must be a whole number.") from None


def color_field(value: str | None) -> str:
    """A colour parameter: white (the default) or black; anything else is a 400."""
    value = value or "white"
    if value not in ("white", "black"):
        raise BadRequest("color must be white or black.")
    return value


def board_from_fen(fen: str | None) -> chess.Board:
    try:
        return chess.Board(fen or "")
    except ValueError:
        raise BadRequest("invalid FEN") from None


def moves_field(data: dict) -> list[str]:
    moves = data.get("moves", [])
    if not isinstance(moves, list) or not all(isinstance(m, str) for m in moves):
        raise BadRequest("moves must be a list of UCI moves.")
    return moves
