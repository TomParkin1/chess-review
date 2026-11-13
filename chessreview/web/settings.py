"""Settings: thresholds, the tablebase, the puzzle library and backups."""

import threading
from datetime import date
from pathlib import Path

from flask import Blueprint, Response, jsonify, request
from flask.typing import ResponseReturnValue

from .. import backup, db, level, library, repertoire, tablebase
from ..classify import ADAPTIVE_BANDS, MODE_KEY, REBUILD, adaptive_enabled, refresh, thresholds_for
from .common import REVIEW_RESULTS, app_state, config, conn, json_body
from .games import rebuild_running, start_analysis
from .puzzles import items_for

bp = Blueprint("settings", __name__)


@bp.get("/api/settings/thresholds")
def thresholds_info() -> ResponseReturnValue:
    cfg = config()
    c = conn()
    rating = level.current_rating(c)
    adaptive = adaptive_enabled(c, cfg)
    th = thresholds_for(rating, cfg, True)
    std = thresholds_for(None, cfg, False)

    def pct(v: float) -> float:
        return round(v * 50, 1)  # -1..1 win-chance drop -> win% points

    mine = c.execute("""SELECT SUM(judgement = 'inaccuracy') AS i, SUM(judgement = 'mistake') AS m,
                               SUM(judgement = 'blunder') AS b FROM mistakes WHERE is_mine = 1""").fetchone()
    return jsonify(
        adaptive=adaptive,
        rating=rating,
        band=th.band,
        adaptive_thresholds=[pct(th.inaccuracy), pct(th.mistake), pct(th.blunder)],
        standard_thresholds=[pct(std.inaccuracy), pct(std.mistake), pct(std.blunder)],
        bands=[{"below": b, "values": [pct(i), pct(m), pct(x)]} for b, i, m, x in ADAPTIVE_BANDS],
        rebuild=dict(REBUILD),
        counts={"inaccuracies": mine["i"] or 0, "mistakes": mine["m"] or 0, "blunders": mine["b"] or 0},
    )


@bp.post("/api/settings/thresholds")
def thresholds_set() -> ResponseReturnValue:
    """Switch adaptive/standard thresholds: every game is reclassified in the background
    (progress in GET /api/settings/thresholds `rebuild`)."""
    cfg = config()
    adaptive = bool(json_body().get("adaptive"))
    c = conn()
    with c:
        db.set_meta(c, MODE_KEY, "adaptive" if adaptive else "standard")
    refresh(c, cfg)  # the fingerprint changed: starts the rebuild
    return jsonify(adaptive=adaptive, rebuild=dict(REBUILD))


@bp.get("/api/settings/tablebase")
def tablebase_info() -> ResponseReturnValue:
    c = conn()
    cached = c.execute("SELECT COUNT(*) FROM tablebase_cache").fetchone()[0]
    return jsonify(enabled=tablebase.enabled(c), cached=cached, max_pieces=tablebase.MAX_PIECES)


@bp.post("/api/settings/tablebase")
def tablebase_set() -> ResponseReturnValue:
    """Body: {enabled: bool}. Off: Stockfish alone plays and judges endgames (no network)."""
    enabled = json_body().get("enabled")
    if not isinstance(enabled, bool):
        return jsonify(error="enabled must be true or false"), 400
    c = conn()
    tablebase.set_enabled(c, enabled)
    return tablebase_info()


@bp.get("/api/library/status")
def library_status() -> ResponseReturnValue:
    state = app_state()
    c = conn()
    return jsonify(
        count=library.count(c),
        fetched_on=db.get_meta(c, library.FETCHED_KEY),
        outdated=library.outdated(c),
        rating=library.rating(c),
        played=int(db.get_meta(c, library.COUNT_KEY) or 0),
        progress=state.library.as_dict(),
    )


@bp.post("/api/library/fetch")
def library_fetch() -> ResponseReturnValue:
    """Download the Lichess puzzle database in the background (about 300 MB, streamed)."""
    cfg = config()
    state = app_state()
    thread = state.library_thread
    if thread is not None and thread.is_alive():
        return jsonify(error="already downloading"), 409
    progress = state.library = library.FetchProgress(state="running")

    def run() -> None:
        c = db.connect(cfg.db_path)
        try:
            library.fetch(c, progress)
        except Exception as exc:  # network or format problems: shown in Settings
            progress.state, progress.error = "error", str(exc)
        finally:
            c.close()

    state.library_thread = threading.Thread(target=run, daemon=True, name="puzzle-library")
    state.library_thread.start()
    return jsonify(progress.as_dict())


@bp.get("/api/library/next")
def library_next() -> ResponseReturnValue:
    """New puzzles near my puzzle rating; ?theme=<Lichess theme> (optional), ?n=10."""
    c = conn()
    theme = request.args.get("theme")
    themes = [theme] if theme in library.THEMES else None
    n = min(max(request.args.get("n", 10, type=int) or 10, 1), 50)
    ids = library.pick(c, n, themes)
    return jsonify(
        items=items_for(c, config(), [(("lib", i), "library") for i in ids]),
        rating=library.rating(c),
        count=library.count(c),
    )


@bp.post("/api/library/result")
def library_result() -> ResponseReturnValue:
    data = json_body()
    if data.get("result") not in REVIEW_RESULTS:
        return jsonify(error="unknown result"), 400
    try:
        return jsonify(library.record(conn(), str(data.get("puzzle_id")), data["result"]))
    except LookupError:
        return jsonify(error="unknown puzzle"), 404


def _backups_folder() -> Path:
    cfg = config()
    return backup.folder_for(cfg.db_path)


@bp.get("/api/backups")
def backups_list() -> ResponseReturnValue:
    folder = _backups_folder()
    shown = folder.relative_to(Path.cwd()) if folder.is_relative_to(Path.cwd()) else folder
    return jsonify(backups=[b.as_dict() for b in backup.files(folder)], folder=str(shown))


@bp.post("/api/backups")
def backups_make() -> ResponseReturnValue:
    """Settings -> Back up now."""
    cfg = config()
    path = backup.make(conn(), "manual", _backups_folder())
    backup.rotate(_backups_folder(), cfg.backup)
    return jsonify(backup=path.name if path else None)  # make() returns None only without a folder


@bp.post("/api/backups/restore")
def backups_restore() -> ResponseReturnValue:
    """Replace all data with a backup: JSON {name} of a copy in the backups folder, or a
    multipart `file` (e.g. an exported copy). The current data is backed up first."""
    cfg = config()
    state = app_state()
    folder = _backups_folder()
    thread = state.library_thread
    if thread is not None and thread.is_alive():
        return jsonify(error="The puzzle library is downloading. Restore when it has finished."), 409
    if rebuild_running():
        return jsonify(error="Your games are being reclassified. Restore when that has finished."), 409
    if not state.import_lock.acquire(blocking=False):
        return jsonify(error="A sync or upload is running. Restore when it has finished."), 409
    try:
        upload = request.files.get("file")
        if upload is not None:
            source = backup.save_upload(folder, upload.read())
        else:
            name = str((request.get_json(silent=True) or {}).get("name") or "")
            source = folder / name
            if not backup.NAME.match(name) or not source.is_file():
                return jsonify(error="No such backup."), 404
        job = state.job
        job.stop()
        job.wait(timeout=30)
        c = conn()
        try:
            before = db.restore(c, source)
        except ValueError as exc:
            if upload is not None:
                source.unlink(missing_ok=True)
            return jsonify(error=str(exc)), 400
        backup.rotate(folder, cfg.backup)
        repertoire.clear_cache()  # the stamp would almost surely differ, but a restore replaces everything
        games = c.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    finally:
        state.import_lock.release()
    start_analysis()  # a copy from before some games were analysed finishes them
    return jsonify(restored=source.name, before=before.name if before else None, games=games)


@bp.get("/api/backups/export")
def backups_export() -> ResponseReturnValue:
    """Export history: a consistent copy of the whole database (Restore can load it)."""
    return Response(
        backup.snapshot_bytes(conn()),
        mimetype="application/vnd.sqlite3",
        headers={"Content-Disposition": f'attachment; filename="chess-review-{date.today().isoformat()}.db"'},
    )
