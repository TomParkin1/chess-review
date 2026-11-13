"""First run (the chess.com account), importing games, the analysis job and the games list."""

import contextlib
import re
import sqlite3

import chess
from flask import Blueprint, jsonify, request
from flask.typing import ResponseReturnValue

from .. import chesscom, db, report, session, srs
from ..classify import REBUILD, refresh
from ..engine import AnalysisJob, count_pending
from ..explain import san_line
from ..pgn_import import ImportStats, import_pgn_text
from .common import app_state, config, conn
from .puzzles import eval_white

bp = Blueprint("games", __name__)


def account_json(c: sqlite3.Connection) -> dict:
    """Who I am and how far the first run has got."""
    cfg = config()
    name = db.username(c, cfg)
    first_import = c.execute(f"SELECT MIN({srs.day_sql('at')}) FROM imports").fetchone()[0]
    return {
        "username": name,
        "username_source": "app" if db.get_meta(c, db.USERNAME_KEY) else ("config" if name else None),
        "games": c.execute("SELECT COUNT(*) FROM games").fetchone()[0],
        "synced": db.get_meta(c, chesscom.LAST_MONTH_KEY) is not None,
        "returning": first_import is not None and first_import < srs.today().isoformat(),
    }


@bp.get("/api/account")
def account() -> ResponseReturnValue:
    """?archives=1 also asks chess.com how many months of games there are (first-sync choice)."""
    c = conn()
    out = account_json(c)
    if request.args.get("archives") and out["username"]:
        with contextlib.suppress(LookupError, OSError):  # the summary is optional
            out |= _archive_summary(chesscom.archives_of(out["username"], fetch=chesscom.fetch_json))
    return jsonify(out)


@bp.post("/api/account")
def account_set() -> ResponseReturnValue:
    """Set my chess.com username (checked against chess.com). Body: {username}."""
    cfg = config()
    name = str((request.get_json(silent=True) or {}).get("username") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,40}", name):
        return jsonify(error="That doesn't look like a chess.com username."), 400
    try:
        archives = chesscom.archives_of(name, fetch=chesscom.fetch_json)
    except LookupError:
        return jsonify(error=f"chess.com has no player called “{name}”."), 404
    except OSError as exc:
        return jsonify(error=f"Couldn't reach chess.com to check the name ({exc})."), 502
    c = conn()
    changed = db.username(c, cfg).lower() != name.lower()
    with c:
        db.set_meta(c, db.USERNAME_KEY, name)
        if changed:  # the next sync starts from the new account's first month
            c.execute("DELETE FROM meta WHERE key = ?", (chesscom.LAST_MONTH_KEY,))
    return jsonify(account_json(c) | _archive_summary(archives))


def _archive_summary(archives: list[str]) -> dict:
    months = ["/".join(u.rstrip("/").split("/")[-2:]) for u in archives]
    return {"months": len(months), "first_month": months[0] if months else None}


@bp.post("/api/upload")
def upload() -> ResponseReturnValue:
    cfg = config()
    state = app_state()
    files = request.files.getlist("files")
    if not files:
        return jsonify(error="No files received"), 400
    c = conn()
    if not db.username(c, cfg):
        return jsonify(error="Set your chess.com username first: it tells which side you played."), 400
    if not state.import_lock.acquire(timeout=60):
        return jsonify(error="Another import is still running. Try again when it has finished."), 409
    try:
        total = ImportStats()
        batch_id = db.create_batch(c, "upload", f"Upload of {len(files)} file(s)")
        try:
            for f in files:
                text = f.read().decode("utf-8", errors="replace")
                total.merge(import_pgn_text(c, text, cfg, source=f.filename or "upload", batch_id=batch_id))
        finally:
            session.finish_import(c, cfg, batch_id)  # an empty batch is dropped, even after an error
    finally:
        state.import_lock.release()
    start_analysis()
    return jsonify(
        files=total.files,
        games_seen=total.games_seen,
        imported=total.imported,
        duplicates=total.duplicates,
        skipped=total.skipped,
    )


def start_analysis() -> None:
    """Start an analysis job unless one is running."""
    cfg = config()
    state = app_state()
    with state.job_lock:  # two requests at once must not start two jobs (the first would be orphaned)
        if not state.job.is_running():
            state.job = AnalysisJob(cfg)
            state.job.start()


def rebuild_running() -> bool:
    return bool(REBUILD.get("running"))


@bp.get("/api/sync")
def sync_status() -> ResponseReturnValue:
    cfg = config()
    state = app_state()
    c = conn()
    return jsonify(
        username=db.username(c, cfg),
        last_month=db.get_meta(c, chesscom.LAST_MONTH_KEY),
        running=state.import_lock.locked(),
    )


@bp.post("/api/sync")
def sync_now() -> ResponseReturnValue:
    """Fetch new games from chess.com (body: {since: all|3m|1m} for the first sync)."""
    cfg = config()
    state = app_state()
    since = (request.get_json(silent=True) or {}).get("since")
    if not state.import_lock.acquire(blocking=False):
        return jsonify(error="A sync or upload is already running. Try again when it has finished."), 409
    try:
        result = chesscom.sync(conn(), cfg, since=since, fetch=chesscom.fetch_json)
    except ValueError as exc:  # no username yet
        return jsonify(error=str(exc)), 400
    except LookupError as exc:
        return jsonify(error=str(exc)), 404
    except OSError as exc:  # network problems (URLError, timeouts)
        return jsonify(error=f"Could not reach chess.com: {exc}"), 502
    finally:
        state.import_lock.release()
    start_analysis()
    return jsonify(result.as_dict())


@bp.post("/api/analysis/start")
def analysis_start() -> ResponseReturnValue:
    state = app_state()
    start_analysis()
    return jsonify(state.job.progress.as_dict())


@bp.post("/api/analysis/stop")
def analysis_stop() -> ResponseReturnValue:
    state = app_state()
    state.job.stop()
    return jsonify(state.job.progress.as_dict())


@bp.get("/api/progress")
def analysis_progress() -> ResponseReturnValue:
    state = app_state()
    c = conn()
    p = state.job.progress.as_dict()
    p["pending"] = count_pending(c)
    return jsonify(p)


@bp.get("/api/games")
def games() -> ResponseReturnValue:
    cfg = config()
    c = conn()
    refresh(c, cfg)
    rows = c.execute("""
        SELECT g.id, g.date, g.end_time, g.white, g.black, g.white_elo, g.black_elo, g.my_color,
               g.my_result, g.result, g.time_class, g.time_control, g.eco, g.opening,
               g.opening_family, g.num_plies, g.termination, g.source, g.link,
               g.analysed_at IS NOT NULL AS analysed,
               CASE g.my_color WHEN 'white' THEN g.accuracy_white ELSE g.accuracy_black END AS my_accuracy,
               CASE g.my_color WHEN 'white' THEN g.accuracy_black ELSE g.accuracy_white END AS opp_accuracy,
               SUM(m.judgement = 'inaccuracy') AS inaccuracies,
               SUM(m.judgement = 'mistake') AS mistakes,
               SUM(m.judgement = 'blunder') AS blunders
        FROM games g LEFT JOIN mistakes m ON m.game_id = g.id AND m.is_mine = 1
        GROUP BY g.id ORDER BY g.end_time DESC, g.id DESC
    """).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.delete("/api/games")
def delete_games() -> ResponseReturnValue:
    """Start afresh: remove all games and their analysis (PGN files on disk are untouched)."""
    state = app_state()
    if rebuild_running():
        return jsonify(error="Your games are being reclassified. Try again when that has finished."), 409
    if not state.import_lock.acquire(blocking=False):
        return jsonify(error="A sync, upload or restore is running. Try again when it has finished."), 409
    try:
        with state.job_lock:
            job = state.job
            job.stop()
        if not job.wait(timeout=30):
            return jsonify(error="The analysis is still stopping. Try again in a moment."), 409
        deleted = db.delete_all_games(conn())
    finally:
        state.import_lock.release()
    return jsonify(deleted=deleted)


@bp.get("/api/game/<int:game_id>")
def game(game_id: int) -> ResponseReturnValue:
    cfg = config()
    c = conn()
    refresh(c, cfg)
    row = c.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    if row is None:
        return jsonify(error="Game not found. It may have been deleted."), 404
    positions = c.execute("SELECT * FROM positions WHERE game_id = ? ORDER BY ply", (game_id,)).fetchall()
    mistakes = {r["ply"]: dict(r) for r in c.execute("SELECT * FROM mistakes WHERE game_id = ?", (game_id,))}

    if positions:
        out_positions = [_position_json(p, mistakes.get(p["ply"])) for p in positions]
    else:
        out_positions = _unanalysed_positions(row)
    # Time used by the move played from each position (ply i -> spent[i]).
    for p, spent in zip(out_positions, report.time_spent(row["clocks_ms"], row["time_control"]), strict=False):
        p["spent_ms"] = spent
    game_json = {k: v for k, v in dict(row).items() if k not in ("pgn", "moves_uci", "clocks_ms")}
    return jsonify(game=game_json, positions=out_positions)


def _position_json(p: sqlite3.Row, mistake: dict | None) -> dict:
    out = {
        "ply": p["ply"],
        "fen": p["fen"],
        "move_uci": p["move_uci"],
        "move_san": p["move_san"],
        "best_uci": p["best_uci"],
        "eval": eval_white(p["score_cp"], p["score_mate"]),
    }
    if p["best_uci"]:
        board = chess.Board(p["fen"])
        line = [chess.Move.from_uci(u) for u in (p["pv_uci"] or "").split()]
        out["best_san"] = board.san(chess.Move.from_uci(p["best_uci"]))
        out["best_line"] = san_line(board, line, 6)
    if mistake:
        out["judgement"] = mistake["judgement"]
        out["explanation"] = mistake["explanation"]
        out["kind"] = mistake["kind"]
        out["is_mine"] = bool(mistake["is_mine"])
        out["mark"] = mistake["mark"]
        out["accuracy"] = mistake["accuracy"]
    return out


def _unanalysed_positions(game: sqlite3.Row) -> list[dict]:
    board = chess.Board(game["start_fen"]) if game["start_fen"] else chess.Board()
    out = []
    for ply, uci in enumerate(game["moves_uci"].split() + [None]):
        move = chess.Move.from_uci(uci) if uci else None
        out.append(
            {
                "ply": ply,
                "fen": board.fen(),
                "move_uci": uci,
                "move_san": board.san(move) if move else None,
                "eval": None,
            }
        )
        if move:
            board.push(move)
    return out
