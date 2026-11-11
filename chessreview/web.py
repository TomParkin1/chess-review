"""Flask web app: upload PGNs, watch analysis progress, browse games, review mistakes.

Pages render a small HTML shell; the data comes from the JSON API below and is
drawn by the scripts in static/.
"""

import json
import re
import sqlite3
import threading
from collections import Counter
from datetime import date

import chess
from flask import Flask, Response, g, jsonify, render_template, request
from werkzeug.exceptions import BadRequest, HTTPException

from . import (backup, chesscom, coach, convert, db, insights, level, library, progress, report, repertoire,
               session, spar, srs, tablebase)
from .classify import (ADAPTIVE_BANDS, MODE_KEY, REBUILD, Eval, adaptive_enabled, capture_square, judge, refresh,
                       thresholds_for)
from .config import PROJECT_ROOT, Config, effective_workers, load_config
from .engine import AnalysisJob, count_pending
from .explain import ATTACKING_KINDS, MoveContext, explain_best, hint, san_line
from .live import LiveEngine, PositionAnalysis
from .threats import gain_of_move, threat_of, training_threat
from .pgn_import import ImportStats, import_pgn_text

JUDGEMENT_ORDER = ["good", "inaccuracy", "mistake", "blunder"]
REVIEW_RESULTS = {"solved", "solved_with_help", "failed", "skipped"}
THREAT_RESULTS = {"correct", "wrong", "skipped"}

# Latest review result for a mistake row `m` (NULL if never attempted).
STATUS_SQL = """(SELECT r.result FROM review_results r
                 WHERE r.game_id = m.game_id AND r.ply = m.ply ORDER BY r.id DESC LIMIT 1)"""
# Practice filters that narrow each other (B16): name -> column; fixed display order where it matters.
FACETS = [("opening", "g.opening_family"), ("phase", "m.phase"), ("result", "g.my_result"),
          ("color", "g.my_color"), ("time_class", "g.time_class"), ("kind", "m.kind")]
FACET_ORDER = {"phase": ["opening", "middlegame", "endgame"], "result": ["win", "draw", "loss"],
               "color": ["white", "black"]}
STATUS_FILTERS = {
    "new": f"{STATUS_SQL} IS NULL",
    "unsolved": f"({STATUS_SQL} IS NULL OR {STATUS_SQL} IN ('failed', 'skipped'))",
    "failed": f"{STATUS_SQL} IN ('failed', 'skipped')",
    "solved": f"{STATUS_SQL} IN ('solved', 'solved_with_help')",
}


MAX_LIMIT = 200     # puzzle items built in one request (one SQL term each)


def _body() -> dict:
    """The request's JSON object; a missing, broken or non-object body is a 400."""
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        raise BadRequest("Expected a JSON object in the request body.")
    return data


def _int(data: dict, key: str, default: int | None = None) -> int:
    """data[key] as an int (or `default` when it is missing); anything else is a 400."""
    value = data.get(key, default)
    if value is None:
        raise BadRequest(f"{key} is required.")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise BadRequest(f"{key} must be a whole number.") from None


def _color(value: str | None) -> str:
    """A colour parameter: white (the default) or black; anything else is a 400."""
    value = value or "white"
    if value not in ("white", "black"):
        raise BadRequest("color must be white or black.")
    return value


def _board(fen: str | None) -> chess.Board:
    try:
        return chess.Board(fen or "")
    except ValueError:
        raise BadRequest("invalid FEN") from None


def _moves(data: dict) -> list[str]:
    moves = data.get("moves", [])
    if not isinstance(moves, list) or not all(isinstance(m, str) for m in moves):
        raise BadRequest("moves must be a list of UCI moves.")
    return moves


def create_app(cfg: Config | None = None) -> Flask:
    cfg = cfg or load_config()
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB of PGN is plenty
    app.config["TEMPLATES_AUTO_RELOAD"] = True  # edited pages show up without a server restart

    state = {"job": AnalysisJob(cfg), "library": library.FetchProgress(), "library_thread": None}
    import_lock = threading.Lock()    # one upload, chess.com sync, restore or Delete all at a time
    job_lock = threading.Lock()       # checking and starting the analysis job is one step
    live = LiveEngine(cfg)
    app.extensions["live"] = live  # closed by the `serve` command on shutdown

    def conn() -> sqlite3.Connection:
        if "db" not in g:
            g.db = db.connect(cfg.db_path)
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        c = g.pop("db", None)
        if c is not None:
            c.close()

    # ------------------------------------------------------------ errors

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException):
        """API errors are JSON ({error}) so pages can say what went wrong; pages keep HTML."""
        if request.path.startswith("/api/"):
            return jsonify(error=exc.description or exc.name), exc.code
        return exc

    @app.errorhandler(Exception)
    def server_error(exc: Exception):
        app.logger.exception("Unexpected error in %s", request.path)
        if request.path.startswith("/api/"):
            return jsonify(error=f"Something went wrong on the server ({type(exc).__name__}: {exc})."), 500
        return "Something went wrong on the server. The details are in the terminal running Chess Review.", 500

    # ------------------------------------------------------------ pages

    @app.get("/")
    def index():
        account = _account(conn())
        return render_template("index.html", account=account, **account)

    @app.get("/favicon.ico")
    def favicon():
        return app.send_static_file("favicon.svg")

    @app.get("/game/<int:game_id>")
    def game_page(game_id: int):
        # An unknown id gets the "Game not found" state at once, without a failing API call.
        found = conn().execute("SELECT 1 FROM games WHERE id = ?", (game_id,)).fetchone() is not None
        return render_template("game.html", game_id=game_id, found=found)

    @app.get("/review")
    def review_page():
        return render_template("review.html")

    @app.get("/games")
    def games_page():
        return render_template("games.html", username=db.username(conn(), cfg))

    @app.get("/settings")
    def settings_page():
        return render_template("settings.html", cfg=cfg, workers=effective_workers(cfg.engine),
                               account=_account(conn()))

    # ------------------------------------------------------------ account (first run)

    def _account(c: sqlite3.Connection) -> dict:
        """Who I am and how far the first run has got."""
        name = db.username(c, cfg)
        first_import = c.execute(f"SELECT MIN({srs.day_sql('at')}) FROM imports").fetchone()[0]
        return {"username": name, "username_source": "app" if db.get_meta(c, db.USERNAME_KEY) else
                ("config" if name else None),
                "games": c.execute("SELECT COUNT(*) FROM games").fetchone()[0],
                "synced": db.get_meta(c, chesscom.LAST_MONTH_KEY) is not None,
                "returning": first_import is not None and first_import < srs.today().isoformat()}

    @app.get("/api/account")
    def account():
        """?archives=1 also asks chess.com how many months of games there are (first-sync choice)."""
        c = conn()
        out = _account(c)
        if request.args.get("archives") and out["username"]:
            try:
                out |= _archive_summary(chesscom.archives_of(out["username"], fetch=chesscom.fetch_json))
            except (LookupError, OSError):
                pass
        return jsonify(out)

    @app.post("/api/account")
    def account_set():
        """Set my chess.com username (checked against chess.com). Body: {username}."""
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
            if changed:     # the next sync starts from the new account's first month
                c.execute("DELETE FROM meta WHERE key = ?", (chesscom.LAST_MONTH_KEY,))
        return jsonify(_account(c) | _archive_summary(archives))

    def _archive_summary(archives: list[str]) -> dict:
        months = ["/".join(u.rstrip("/").split("/")[-2:]) for u in archives]
        return {"months": len(months), "first_month": months[0] if months else None}

    # ------------------------------------------------------------ upload + analysis

    @app.post("/api/upload")
    def upload():
        files = request.files.getlist("files")
        if not files:
            return jsonify(error="No files received"), 400
        c = conn()
        if not db.username(c, cfg):
            return jsonify(error="Set your chess.com username first: it tells which side you played."), 400
        if not import_lock.acquire(timeout=60):
            return jsonify(error="Another import is still running. Try again when it has finished."), 409
        try:
            total = ImportStats()
            batch_id = db.create_batch(c, "upload", f"Upload of {len(files)} file(s)")
            try:
                for f in files:
                    text = f.read().decode("utf-8", errors="replace")
                    total.merge(import_pgn_text(c, text, cfg, source=f.filename or "upload",
                                                batch_id=batch_id))
            finally:
                session.finish_import(c, cfg, batch_id)     # an empty batch is dropped, even after an error
        finally:
            import_lock.release()
        _start_job()
        return jsonify(files=total.files, games_seen=total.games_seen, imported=total.imported,
                       duplicates=total.duplicates, skipped=total.skipped)

    def _start_job():
        with job_lock:      # two requests at once must not start two jobs (the first would be orphaned)
            if not state["job"].is_running():
                state["job"] = AnalysisJob(cfg)
                state["job"].start()

    def _rebuild_running() -> bool:
        return bool(REBUILD.get("running"))

    @app.get("/api/sync")
    def sync_status():
        c = conn()
        return jsonify(username=db.username(c, cfg),
                       last_month=db.get_meta(c, chesscom.LAST_MONTH_KEY),
                       running=import_lock.locked())

    @app.post("/api/sync")
    def sync_now():
        """Fetch new games from chess.com (body: {since: all|3m|1m} for the first sync)."""
        since = (request.get_json(silent=True) or {}).get("since")
        if not import_lock.acquire(blocking=False):
            return jsonify(error="A sync or upload is already running. Try again when it has finished."), 409
        try:
            result = chesscom.sync(conn(), cfg, since=since, fetch=chesscom.fetch_json)
        except ValueError as exc:       # no username yet
            return jsonify(error=str(exc)), 400
        except LookupError as exc:
            return jsonify(error=str(exc)), 404
        except OSError as exc:  # network problems (URLError, timeouts)
            return jsonify(error=f"Could not reach chess.com: {exc}"), 502
        finally:
            import_lock.release()
        _start_job()
        return jsonify(result.as_dict())

    @app.post("/api/analysis/start")
    def analysis_start():
        _start_job()
        return jsonify(state["job"].progress.as_dict())

    @app.post("/api/analysis/stop")
    def analysis_stop():
        state["job"].stop()
        return jsonify(state["job"].progress.as_dict())

    @app.get("/api/progress")
    def analysis_progress():
        c = conn()
        p = state["job"].progress.as_dict()
        p["pending"] = count_pending(c)
        return jsonify(p)

    # ------------------------------------------------------------ games

    @app.get("/api/games")
    def games():
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

    @app.delete("/api/games")
    def delete_games():
        """Start afresh: remove all games and their analysis (PGN files on disk are untouched)."""
        if _rebuild_running():
            return jsonify(error="Your games are being reclassified. Try again when that has finished."), 409
        if not import_lock.acquire(blocking=False):
            return jsonify(error="A sync, upload or restore is running. Try again when it has finished."), 409
        try:
            with job_lock:
                job = state["job"]
                job.stop()
            if not job.wait(timeout=30):
                return jsonify(error="The analysis is still stopping. Try again in a moment."), 409
            deleted = db.delete_all_games(conn())
        finally:
            import_lock.release()
        return jsonify(deleted=deleted)

    @app.get("/api/game/<int:game_id>")
    def game(game_id: int):
        c = conn()
        refresh(c, cfg)
        row = c.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        if row is None:
            return jsonify(error="Game not found. It may have been deleted."), 404
        positions = c.execute(
            "SELECT * FROM positions WHERE game_id = ? ORDER BY ply", (game_id,)).fetchall()
        mistakes = {r["ply"]: dict(r) for r in c.execute(
            "SELECT * FROM mistakes WHERE game_id = ?", (game_id,))}

        if positions:
            out_positions = [_position_json(p, mistakes.get(p["ply"])) for p in positions]
        else:
            out_positions = _unanalysed_positions(row)
        # Time used by the move played from each position (ply i -> spent[i]).
        for p, spent in zip(out_positions, report.time_spent(row["clocks_ms"], row["time_control"])):
            p["spent_ms"] = spent
        game_json = {k: row[k] for k in row.keys() if k not in ("pgn", "moves_uci", "clocks_ms")}
        return jsonify(game=game_json, positions=out_positions)

    # ------------------------------------------------------------ review

    @app.get("/api/mistakes")
    def mistakes():
        """My mistakes for Practice, filtered. `keys` lists every match in order ("<game>:<ply>"),
        `items` the first `limit` of them as puzzles (all of them without `limit`), and `options`
        the values of each facet that have matches under the other filters, with counts."""
        c = conn()
        refresh(c, cfg)
        args = request.args
        where, params = ["m.is_mine = 1"], []
        if args.get("judgement") == "miss":     # mistakes/blunders that let the opponent's error go
            where.append("m.mark = 'miss'")
        else:
            judgements = [j for j in args.get("judgement", "mistake,blunder").split(",")
                          if j in JUDGEMENT_ORDER]
            where.append(f"m.judgement IN ({','.join('?' * len(judgements))})")
            params += judgements
        if args.get("game", type=int):
            where.append("m.game_id = ?")
            params.append(args.get("game", type=int))
        if args.get("status") in STATUS_FILTERS:
            where.append(STATUS_FILTERS[args["status"]])
        last = args.get("last", type=int)
        if last and last > 0:
            # Only my most recent N games (newest by date, then by import order).
            where.append("g.id IN (SELECT id FROM games ORDER BY end_time DESC, id DESC LIMIT ?)")
            params.append(last)
        order = {
            "worst": "m.win_drop DESC, g.id, m.ply",
            "newest": "g.end_time DESC, g.id DESC, m.ply",
            "oldest": "g.end_time, g.id, m.ply",
        }.get(args.get("sort", "worst"), "m.win_drop DESC, g.id, m.ply")

        # One light query for every candidate (no positions, no explanations), then the facets.
        columns = ", ".join(f"{column} AS {name}" for name, column in FACETS)
        rows = c.execute(f"""SELECT m.game_id, m.ply, {columns} FROM mistakes m JOIN games g ON g.id = m.game_id
                             WHERE {' AND '.join(where)} ORDER BY {order}""", params).fetchall()
        # In time trouble / played very fast when I made the mistake (needs chess.com clocks).
        for flag, test in (("low_clock", insights.in_time_trouble), ("fast", insights.fast_move)):
            if args.get(flag):
                clocks = {r["id"]: r for r in c.execute(
                    "SELECT id, clocks_ms, time_control FROM games WHERE clocks_ms IS NOT NULL")}
                rows = [r for r in rows if r["game_id"] in clocks and test(
                    clocks[r["game_id"]]["clocks_ms"], clocks[r["game_id"]]["time_control"], r["ply"])]
        chosen = {name: args[name] for name, _ in FACETS if args.get(name)}
        hits = [r for r in rows if _facets_match(r, chosen)]
        limit = args.get("limit", type=int)
        if limit is None:
            by_key = {(i["game_id"], i["ply"]): i for i in _query_mistakes(
                c, cfg, where + [f"{column} = ?" for name, column in FACETS if name in chosen],
                params + [chosen[name] for name, _ in FACETS if name in chosen], order)}
            items = [by_key[(r["game_id"], r["ply"])] for r in hits]
        else:
            items = _items_for(c, [((r["game_id"], r["ply"]), "practice")
                                   for r in hits[:min(max(0, limit), MAX_LIMIT)]])
        return jsonify(total=len(hits), keys=[f"{r['game_id']}:{r['ply']}" for r in hits], items=items,
                       options=_facet_options(rows, chosen), analysed=coach.analysed_games(c))

    @app.get("/api/mistakes/items")
    def mistake_items():
        """Puzzle items for `keys` ("<game>:<ply>,...", at most 50), in that order: the next
        Practice session from the key list /api/mistakes returned."""
        keyed = []
        for key in request.args.get("keys", "").split(",")[:50]:
            game_id, _, ply = key.partition(":")
            if game_id.isdigit() and ply.isdigit():
                keyed.append(((int(game_id), int(ply)), "practice"))
        c = conn()
        refresh(c, cfg)
        return jsonify(items=_items_for(c, keyed))

    # ------------------------------------------------------------ training (SRS) + new-games session

    @app.get("/insights")
    def insights_page():
        return render_template("insights.html")

    @app.get("/api/insights")
    def insights_data():
        c = conn()
        refresh(c, cfg)
        return jsonify(insights.compute(c, request.args.get("time_class") or None,
                                        request.args.get("last", type=int)))

    @app.get("/api/settings/thresholds")
    def thresholds_info():
        c = conn()
        rating = level.current_rating(c)
        adaptive = adaptive_enabled(c, cfg)
        th = thresholds_for(rating, cfg, True)
        std = thresholds_for(None, cfg, False)
        pct = lambda v: round(v * 50, 1)   # -1..1 win-chance drop -> win% points
        mine = c.execute("""SELECT SUM(judgement = 'inaccuracy') AS i, SUM(judgement = 'mistake') AS m,
                                   SUM(judgement = 'blunder') AS b FROM mistakes WHERE is_mine = 1""").fetchone()
        return jsonify(adaptive=adaptive, rating=rating, band=th.band,
                       adaptive_thresholds=[pct(th.inaccuracy), pct(th.mistake), pct(th.blunder)],
                       standard_thresholds=[pct(std.inaccuracy), pct(std.mistake), pct(std.blunder)],
                       bands=[{"below": b, "values": [pct(i), pct(m), pct(x)]} for b, i, m, x in ADAPTIVE_BANDS],
                       rebuild=dict(REBUILD), counts={"inaccuracies": mine["i"] or 0, "mistakes": mine["m"] or 0,
                                                      "blunders": mine["b"] or 0})

    @app.post("/api/settings/thresholds")
    def thresholds_set():
        """Switch adaptive/standard thresholds: every game is reclassified in the background
        (progress in GET /api/settings/thresholds `rebuild`)."""
        adaptive = bool(_body().get("adaptive"))
        c = conn()
        with c:
            db.set_meta(c, MODE_KEY, "adaptive" if adaptive else "standard")
        refresh(c, cfg)    # the fingerprint changed: starts the rebuild
        return jsonify(adaptive=adaptive, rebuild=dict(REBUILD))

    @app.get("/api/insights/endgame-training")
    def endgame_training():
        """Insights' endgame card: my endgame drills (essential endgames, won positions, puzzles)."""
        return jsonify(insights.drill_progress(conn()))

    @app.get("/api/settings/tablebase")
    def tablebase_info():
        c = conn()
        cached = c.execute("SELECT COUNT(*) FROM tablebase_cache").fetchone()[0]
        return jsonify(enabled=tablebase.enabled(c), cached=cached, max_pieces=tablebase.MAX_PIECES)

    @app.post("/api/settings/tablebase")
    def tablebase_set():
        """Body: {enabled: bool}. Off: Stockfish alone plays and judges endgames (no network)."""
        enabled = _body().get("enabled")
        if not isinstance(enabled, bool):
            return jsonify(error="enabled must be true or false"), 400
        c = conn()
        tablebase.set_enabled(c, enabled)
        return tablebase_info()

    @app.get("/openings")
    def openings_page():
        return render_template("openings.html")

    def _tree(color: str):
        return repertoire.build(conn(), color, request.args.get("family") or None)

    @app.get("/api/openings")
    def openings_overview():
        """Families I play per colour, repertoire problems and drill status."""
        c = conn()
        refresh(c, cfg)
        color = _color(request.args.get("color"))
        tree = _tree(color)
        families = [dict(r) for r in c.execute(
            """SELECT opening_family AS family, COUNT(*) AS games FROM games WHERE my_color = ?
               GROUP BY opening_family ORDER BY games DESC""", (color,))]
        return jsonify(families=families, problems=repertoire.problems(tree),
                       stats=srs.line_stats(c, color), positions=sum(
                           1 for n in tree.values() if n.my_turn and n.repertoire()[1]))

    @app.get("/api/openings/node")
    def openings_node():
        c = conn()
        refresh(c, cfg)
        color = _color(request.args.get("color"))
        tree = _tree(color)
        fen = request.args.get("fen")
        epd = _board(fen).epd() if fen else repertoire.root_epd()
        node = repertoire.node_json(tree, epd)
        # Not an error (an empty database, or a move off my repertoire): the page says so.
        return jsonify(node or {"reached": False})

    @app.get("/api/openings/drill")
    def openings_drill():
        c = conn()
        refresh(c, cfg)
        color = _color(request.args.get("color"))
        mode = request.args.get("mode")
        tree = _tree(color)
        if mode == "due":
            lines = repertoire.due_exercises(tree, color, srs.due_lines(c, color))
        else:
            lines = repertoire.drill_lines(tree, color, count=request.args.get("count", 6, type=int))
        return jsonify(lines=lines)

    @app.post("/api/openings/result")
    def openings_result():
        data = _body()
        if data.get("result") not in REVIEW_RESULTS or not isinstance(data.get("key"), str) or not data["key"]:
            return jsonify(error="key and a valid result are required"), 400
        srs.record_line(conn(), data["key"], data["result"])
        return jsonify(ok=True)

    @app.get("/train")
    def train_page():
        return render_template("train.html")

    @app.get("/session")
    def session_page():
        return render_template("session.html")

    @app.get("/api/train/stats")
    def train_stats():
        c = conn()
        refresh(c, cfg)
        out = srs.stats(c, cfg)
        out["session"] = session.status(c, cfg)
        out["lines_due"] = len(srs.due_lines(c, "white")) + len(srs.due_lines(c, "black"))
        return jsonify(out)

    @app.get("/api/today")
    def today():
        """Today's workout plan with progress, plus streak, the real-game default and how far
        the games are (for the first run: no games yet / still analysing)."""
        c = conn()
        refresh(c, cfg)
        return _today_json(c, coach.today_plan(c, cfg))

    @app.post("/api/today/refresh")
    def today_refresh():
        """Rebuild today's plan (e.g. after new games), keeping what was already tried today."""
        c = conn()
        refresh(c, cfg)
        return _today_json(c, coach.today_plan(c, cfg, refresh=True))

    @app.post("/api/today/budget")
    def today_budget():
        """Body: {minutes: 10 | 20 | 30}. Today's plan is rebuilt for the new budget, keeping what
        was already tried today (its time counts)."""
        c = conn()
        try:
            coach.set_budget(c, _int(_body(), "minutes"))
        except (TypeError, ValueError) as exc:
            return jsonify(error=str(exc)), 400
        refresh(c, cfg)
        return _today_json(c, coach.today_plan(c, cfg, refresh=True))

    def _today_json(c: sqlite3.Connection, plan: dict):
        for block in plan["blocks"]:
            del block["items"]      # the keys are internal; /api/today/<block> serves the puzzles
        games = c.execute("SELECT COUNT(*) AS total, SUM(analysed_at IS NOT NULL) AS analysed FROM games").fetchone()
        return jsonify(plan=plan, streak=srs.streak(c), blind=cfg.training.blind, budgets=coach.BUDGETS,
                       session=session.status(c, cfg), username=db.username(c, cfg),
                       games={"total": games["total"], "analysed": games["analysed"] or 0})

    @app.get("/api/today/<block_id>")
    def today_block(block_id: str):
        """The puzzles of one block, each marked done_today. Reviews follow the real-game toggle
        (?blind=1|0), real-game positions are always blind, focus puzzles are labelled."""
        c = conn()
        plan = coach.today_plan(c, cfg)
        block = next((b for b in plan["blocks"] if b["id"] == block_id), None)
        if block is None:
            return jsonify(error="no such block today"), 404
        attempted = coach.attempted_today(c, srs.today())
        blind = request.args.get("blind", "1" if cfg.training.blind else "0") == "1"
        mode = {"reviews": "blind" if blind else None, "real": "blind"}.get(block_id)
        items = _items_for(c, coach.block_items(plan, block_id))
        for item in items:
            if item.get("mode") == "library":
                item["done_today"] = ("lib", item["puzzle_id"]) in attempted
                continue
            item["mode"] = mode
            item["done_today"] = (item["game_id"], item["ply"]) in attempted
        return jsonify(items=items, block={k: v for k, v in block.items() if k != "items"})

    # ------------------------------------------------------------ convert + endgames

    @app.get("/convert")
    def convert_page():
        return render_template("convert.html")

    @app.get("/api/convert")
    def convert_overview():
        c = conn()
        refresh(c, cfg)
        rating = level.current_rating(c)
        return jsonify(positions=convert.convert_overview(c), endgames=convert.endgame_overview(c),
                       next=convert.next_convert(c), rating=rating, winning_cp=level.winning_cp(rating),
                       levels=convert.LEVELS, analysed=coach.analysed_games(c))

    @app.get("/api/convert/task")
    def convert_task():
        task = convert.task_for(conn(), request.args.get("key", ""))
        if task is None:
            return jsonify(error="unknown position"), 404
        return jsonify(task.as_dict())

    def _replay(task: convert.Task, moves: list[str]) -> chess.Board:
        """The playout so far; raises ValueError on an illegal move."""
        board = chess.Board(task.fen)
        for uci in moves:
            move = chess.Move.from_uci(uci)
            if move not in board.legal_moves:
                raise ValueError(f"illegal move {uci}")
            board.push(move)
        return board

    @app.post("/api/play/move")
    def play_move():
        """My move in a playout; Stockfish replies. Body: {key, moves (so far), uci, streak}."""
        data = _body()
        streak = _int(data, "streak", 0)
        c = conn()
        task = convert.task_for(c, str(data.get("key", "")))
        if task is None:
            return jsonify(error="unknown position"), 404
        try:
            board = _replay(task, _moves(data))
            move = chess.Move.from_uci(data.get("uci", ""))
            if move not in board.legal_moves:
                raise ValueError(f"illegal move {data.get('uci')}")
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        me = chess.WHITE if task.color == "white" else chess.BLACK
        rating = level.current_rating(c)
        out = {"san": board.san(move), "uci": move.uci()}
        before = tablebase.probe(c, board)          # the position I moved in (E3: blame a move only if known)
        board.push(move)
        st = convert.state(task, board, None, streak, rating)
        if st.status == "playing":
            analysis = live.analyse(board)      # Stockfish's eval of the position after its best reply
            # With 7 pieces or fewer the tablebase defends perfectly and judges my move exactly (E3).
            tb = tablebase.probe(c, board)
            best = tablebase.choose(tb, analysis.best) if tb else None
            reply = chess.Move.from_uci(best.uci) if best else analysis.best
            out |= {"reply_uci": reply.uci(), "reply_san": board.san(reply),
                    "reply_source": "tablebase" if best else "stockfish"}
            board.push(reply)
            ev = analysis.for_side(me)
            out |= {"eval": str(ev), "cp": ev.as_cp()}
            exact = (_start_wdl(c, task), before.wdl if before else None, -tb.wdl) if tb else None
            if exact:       # my side, before and after my move (the Convert lesson finds the slip by it)
                out["tablebase"] = {"wdl": exact[2], "verdict": tablebase.verdict(exact[2]),
                                    "before_wdl": exact[1],
                                    "before_verdict": tablebase.verdict(exact[1]) if exact[1] is not None else None}
            st = convert.state(task, board, ev, st.streak, rating, tb=exact)
        out |= {"fen": board.fen(), "status": st.status, "reason": st.reason, "streak": st.streak}
        return jsonify(out)

    @app.post("/api/play/hint")
    def play_hint():
        data = _body()
        c = conn()
        task = convert.task_for(c, str(data.get("key", "")))
        if task is None:
            return jsonify(error="unknown position"), 404
        try:
            board = _replay(task, _moves(data))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        tb = tablebase.probe(c, board)
        if tb and tb.best:      # perfect play (E3); in a draw, the engine's drawing move
            move = tablebase.choose(tb, live.analyse(board).best if tb.best.wdl == 0 else None)
            return jsonify(uci=move.uci, san=move.san, source="tablebase")
        best = live.analyse(board).best
        return jsonify(uci=best.uci() if best else None, san=board.san(best) if best else None, source="stockfish")

    def _start_wdl(c: sqlite3.Connection, task: convert.Task) -> int | None:
        """The task's start from my side in the tablebase (None: more than 7 pieces, or unknown)."""
        tb = tablebase.probe(c, chess.Board(task.fen))
        return tb.wdl if tb else None

    @app.post("/api/convert/result")
    def convert_result():
        data = _body()
        task = convert.task_for(conn(), str(data.get("key", "")))
        if task is None or data.get("result") not in REVIEW_RESULTS:
            return jsonify(error="unknown position or result"), 400
        srs.record_key(conn(), task.kind, task.card_key, data["result"])
        return jsonify(ok=True)

    # ------------------------------------------------------------ sparring (E5)

    REVIEW_CHUNK = 12        # plies judged per /api/spar/review request

    @app.get("/spar")
    def spar_page():
        return render_template("spar.html")

    @app.get("/api/spar")
    def spar_overview():
        c = conn()
        refresh(c, cfg)
        return jsonify(positions=spar.positions(c), levels=spar.levels(c), offset=spar.saved_offset(c),
                       rating=level.current_rating(c), history=spar.history(c))

    @app.post("/api/spar/offset")
    def spar_offset():
        c = conn()
        spar.save_offset(c, _int(_body(), "offset"))
        return jsonify(offset=spar.saved_offset(c))

    def _spar_game(data: dict) -> tuple[chess.Board, chess.Color, list[str]]:
        start = _board(data.get("fen"))
        if not start.is_valid():        # e.g. no kings, pawns on the back rank: the engine can't play it
            raise BadRequest("that position isn't legal")
        moves = _moves(data)
        try:
            board = spar.replay(start.fen(), moves)
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
        return board, chess.WHITE if _color(data.get("color")) == "white" else chess.BLACK, moves

    @app.post("/api/spar/move")
    def spar_move():
        """Body: {fen (start), color (mine), moves (so far), uci (mine; omitted when Stockfish is to
        move, e.g. I play Black from the start), offset}. Stockfish replies at the chosen strength."""
        data = _body()
        board, me, _ = _spar_game(data)
        c = conn()
        st = spar.strength(spar.target_elo(level.current_rating(c), _int(data, "offset", 0)))
        out: dict = {}
        if data.get("uci") is not None:
            if board.turn != me:
                return jsonify(error="it isn't your move"), 400
            try:
                move = chess.Move.from_uci(str(data["uci"]))
            except ValueError:
                move = None
            if move is None or move not in board.legal_moves:
                return jsonify(error=f"illegal move {data['uci']}"), 400
            out |= {"uci": move.uci(), "san": board.san(move)}
            board.push(move)
        elif board.turn == me:
            return jsonify(error="your move"), 400
        over = spar.outcome(board, me)
        if over is None:
            reply = live.play(board, st.options, st.limit)
            out |= {"reply_uci": reply.uci(), "reply_san": board.san(reply)}
            board.push(reply)
            over = spar.outcome(board, me)
        out |= {"fen": board.fen(), "status": over[0] if over else "playing", "reason": over[1] if over else "",
                "elo": st.elo}
        return jsonify(out)

    @app.post("/api/spar/finish")
    def spar_finish():
        """Store a finished game: {source, fen, color, moves, offset, resigned}. The result comes from
        the position (a game not over yet can only be resigned: a loss)."""
        data = _body()
        board, me, moves = _spar_game(data)
        over = spar.outcome(board, me)
        if over is None:
            if not data.get("resigned"):
                return jsonify(error="the game isn't over"), 400
            over = ("loss", "You resigned.")
        c = conn()
        offset = spar.clamp_offset(_int(data, "offset", 0))
        elo = spar.strength(spar.target_elo(level.current_rating(c), offset)).elo
        source = str(data.get("source") or "fen")[:40]
        game_id = spar.record(c, source, chess.Board(data["fen"]).fen(), "white" if me == chess.WHITE else "black",
                              elo, offset, moves, over[0], over[1])
        return jsonify(id=game_id, result=over[0], reason=over[1], history=spar.history(c))

    @app.post("/api/spar/review")
    def spar_review():
        """Judge my moves of a stored game at full strength: {id, from} -> REVIEW_CHUNK plies from
        `from` ({ply, san, uci, mine, judgement, before, after, best_san}), and `next` (or None)."""
        data = _body()
        c = conn()
        g = spar.game(c, _int(data, "id"))
        if g is None:
            return jsonify(error="no such game"), 404
        moves = g["moves"].split()
        start = max(0, _int(data, "from", 0))
        me = chess.WHITE if g["color"] == "white" else chess.BLACK
        th = level.current_thresholds(c, cfg)
        board = spar.replay(g["start_fen"], moves[:start])
        out = []
        for ply in range(start, min(len(moves), start + REVIEW_CHUNK)):
            move = chess.Move.from_uci(moves[ply])
            entry = {"ply": ply, "uci": move.uci(), "san": board.san(move), "mine": board.turn == me}
            if entry["mine"]:
                before = live.analyse(board)
                after_board = board.copy()
                after_board.push(move)
                after = live.analyse(after_board)
                verdict, _, _ = judge(before.for_side(me), after.for_side(me), cfg, th)
                is_best = before.best == move
                entry |= {"judgement": "good" if is_best else verdict, "before": str(before.for_side(me)),
                          "after": str(after.for_side(me)),
                          "best_san": board.san(before.best) if before.best and not is_best else None}
            out.append(entry)
            board.push(move)
        nxt = start + REVIEW_CHUNK
        return jsonify(moves=out, next=nxt if nxt < len(moves) else None, total=len(moves))

    @app.get("/progress")
    def progress_page():
        return render_template("progress.html")

    @app.get("/api/progress/report")
    def progress_report():
        c = conn()
        refresh(c, cfg)
        return jsonify(progress.report(c, cfg))

    def _items_for(c, keyed: list[tuple[tuple, str]]) -> list[dict]:
        """Puzzle items for ((game_id, ply) | ("lib", puzzle_id), tag) pairs, in the given order,
        tagged due/new/calm/focus."""
        mine = [k for k, _ in keyed if k[0] != "lib"]
        by_key: dict = {}
        if mine:
            pairs = " OR ".join("(m.game_id = ? AND m.ply = ?)" for _ in mine)
            flat = [v for k in mine for v in k]
            by_key = {(i["game_id"], i["ply"]): i
                      for i in _query_mistakes(c, cfg, ["m.is_mine = 1", f"({pairs})"], flat, "m.game_id")}
        lib_ids = [k[1] for k, _ in keyed if k[0] == "lib"]
        if lib_ids:
            rows = c.execute(f"SELECT * FROM puzzles WHERE id IN ({','.join('?' * len(lib_ids))})", lib_ids)
            by_key |= {("lib", r["id"]): library.puzzle_json(r) for r in rows}
        out = []
        for k, tag in keyed:
            if k in by_key:
                item = by_key[k]
                out.append({**item, "queue": tag,
                            "calm": item.get("mode") != "library" and (tag == "calm" or item["judgement"] == "good")})
        return out

    # ------------------------------------------------------------ puzzle library

    @app.get("/puzzles")
    def puzzles_page():
        return render_template("puzzles.html", themes=library.THEMES)

    @app.get("/api/library/status")
    def library_status():
        c = conn()
        return jsonify(count=library.count(c), fetched_on=db.get_meta(c, library.FETCHED_KEY),
                       outdated=library.outdated(c),
                       rating=library.rating(c), played=int(db.get_meta(c, library.COUNT_KEY) or 0),
                       progress=state["library"].as_dict())

    @app.post("/api/library/fetch")
    def library_fetch():
        """Download the Lichess puzzle database in the background (about 300 MB, streamed)."""
        thread = state["library_thread"]
        if thread is not None and thread.is_alive():
            return jsonify(error="already downloading"), 409
        progress = state["library"] = library.FetchProgress(state="running")

        def run():
            c = db.connect(cfg.db_path)
            try:
                library.fetch(c, progress)
            except Exception as exc:    # network or format problems: shown in Settings
                progress.state, progress.error = "error", str(exc)
            finally:
                c.close()
        state["library_thread"] = threading.Thread(target=run, daemon=True, name="puzzle-library")
        state["library_thread"].start()
        return jsonify(progress.as_dict())

    @app.get("/api/library/next")
    def library_next():
        """New puzzles near my puzzle rating; ?theme=<Lichess theme> (optional), ?n=10."""
        c = conn()
        theme = request.args.get("theme")
        themes = [theme] if theme in library.THEMES else None
        n = min(max(request.args.get("n", 10, type=int) or 10, 1), 50)
        ids = library.pick(c, n, themes)
        return jsonify(items=_items_for(c, [(("lib", i), "library") for i in ids]),
                       rating=library.rating(c), count=library.count(c))

    @app.post("/api/library/result")
    def library_result():
        data = _body()
        if data.get("result") not in REVIEW_RESULTS:
            return jsonify(error="unknown result"), 400
        try:
            return jsonify(library.record(conn(), str(data.get("puzzle_id")), data["result"]))
        except LookupError:
            return jsonify(error="unknown puzzle"), 404

    # ------------------------------------------------------------ backups

    def _backups_folder():
        return backup.folder_for(cfg.db_path)

    @app.get("/api/backups")
    def backups_list():
        folder = _backups_folder()
        shown = folder.relative_to(PROJECT_ROOT) if folder.is_relative_to(PROJECT_ROOT) else folder
        return jsonify(backups=[b.as_dict() for b in backup.files(folder)], folder=str(shown))

    @app.post("/api/backups")
    def backups_make():
        """Settings -> Back up now."""
        path = backup.make(conn(), "manual", _backups_folder())
        backup.rotate(_backups_folder(), cfg.backup)
        return jsonify(backup=path.name)

    @app.post("/api/backups/restore")
    def backups_restore():
        """Replace all data with a backup: JSON {name} of a copy in the backups folder, or a
        multipart `file` (e.g. an exported copy). The current data is backed up first."""
        folder = _backups_folder()
        thread = state["library_thread"]
        if thread is not None and thread.is_alive():
            return jsonify(error="The puzzle library is downloading. Restore when it has finished."), 409
        if _rebuild_running():
            return jsonify(error="Your games are being reclassified. Restore when that has finished."), 409
        if not import_lock.acquire(blocking=False):
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
            job = state["job"]
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
            repertoire.clear_cache()    # the stamp would almost surely differ, but a restore replaces everything
            games = c.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        finally:
            import_lock.release()
        _start_job()        # a copy from before some games were analysed finishes them
        return jsonify(restored=source.name, before=before.name if before else None, games=games)

    @app.get("/api/backups/export")
    def backups_export():
        """Export history: a consistent copy of the whole database (Restore can load it)."""
        return Response(backup.snapshot_bytes(conn()), mimetype="application/vnd.sqlite3",
                        headers={"Content-Disposition":
                                 f'attachment; filename="chess-review-{date.today().isoformat()}.db"'})

    @app.get("/api/session")
    def session_info():
        c = conn()
        refresh(c, cfg)
        st = session.status(c, cfg)
        if not st["open"]:
            return jsonify(st)
        where, params = session.puzzle_where(cfg, st["batch_ids"])
        st["items"] = _query_mistakes(c, cfg, ["m.is_mine = 1", where], params,
                                      "g.end_time, g.id, m.ply")
        return jsonify(st)

    @app.get("/api/session/self-review")
    def self_review_games():
        """Games of the open session to self-review (moves only: no evals, no marks)."""
        c = conn()
        refresh(c, cfg)     # loaded alongside /api/session: both must see the new games classified
        st = session.status(c, cfg)
        return jsonify(games=session.self_review_games(c, cfg, st["batch_ids"]) if st["open"] else [])

    @app.post("/api/session/self-review")
    def self_review_pick():
        """Body: {game_id, ply} (ply of my move I think the game turned on, or null = not sure)."""
        data = _body()
        ply = data.get("ply")
        try:
            return jsonify(session.record_self_review(conn(), int(data.get("game_id")),
                                                      None if ply is None else int(ply)))
        except (LookupError, TypeError, ValueError):
            return jsonify(error="no turning point for this game"), 404

    @app.post("/api/session/done")
    def session_done():
        c = conn()
        raw = (request.get_json(silent=True) or {}).get("batch_ids", [])
        if not isinstance(raw, list):
            return jsonify(error="batch_ids must be a list"), 400
        batch_ids = [_int({"id": i}, "id") for i in raw]
        summary = session.summary(c, cfg, batch_ids)
        session.mark_done(c, batch_ids)
        return jsonify(summary)

    @app.post("/api/judge")
    def judge_move():
        """Judge a move I try in review mode.

        Body: {fen, uci, game_id?, ply?, step?}. On the first step of a puzzle (step 0)
        the stored analysis of the game position is used, so the verdict matches the game.
        """
        data = _body()
        fen, uci = data.get("fen"), data.get("uci")
        if not (isinstance(fen, str) and isinstance(uci, str) and fen and uci):
            return jsonify(error="fen and uci are required"), 400
        before = prev_capture = None
        c = conn()
        thresholds = level.current_thresholds(c, cfg)   # tries are judged at my level *now*
        if data.get("game_id") is not None:
            if _int(data, "step", 0) == 0 and data.get("ply") is not None:
                pos = c.execute("SELECT * FROM positions WHERE game_id = ? AND ply = ?",
                                (_int(data, "game_id"), _int(data, "ply"))).fetchone()
                if pos is not None and pos["fen"] == fen:
                    before = _stored_analysis(pos)
                    prev = c.execute("SELECT fen, move_uci FROM positions WHERE game_id = ? AND ply = ?",
                                     (data["game_id"], pos["ply"] - 1)).fetchone()
                    prev_capture = _prev_capture(prev["fen"], prev["move_uci"]) if prev else None
        try:
            result = live.judge(fen, uci, before, thresholds, prev_capture)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(result.as_dict())

    @app.post("/api/review/result")
    def review_result():
        data = _body()
        if data.get("result") not in REVIEW_RESULTS:
            return jsonify(error="unknown result"), 400
        threat_result = data.get("threat_result")
        if threat_result is not None and threat_result not in THREAT_RESULTS:
            return jsonify(error="unknown threat_result"), 400
        game_id, ply = _int(data, "game_id"), _int(data, "ply")
        attempts, hints = _int(data, "attempts", 0), _int(data, "hints_used", 0)
        c = conn()
        if c.execute("SELECT 1 FROM mistakes WHERE game_id = ? AND ply = ? AND is_mine = 1",
                     (game_id, ply)).fetchone() is None:
            return jsonify(error="unknown position"), 404
        with c:
            c.execute("""INSERT INTO review_results (game_id, ply, result, attempts, hints_used, mode, threat_result)
                         VALUES (?, ?, ?, ?, ?, ?, ?)""",
                      (game_id, ply, data["result"], attempts, hints,
                       "blind" if data.get("mode") == "blind" else "labelled", threat_result))
        srs.record(c, game_id, ply, data["result"], kind="position" if data.get("calm") else "mistake")
        return jsonify(ok=True)

    @app.post("/api/threat/check")
    def threat_check():
        """Did I spot the opponent's threat? Body: {game_id, ply, uci} (the opponent move I think
        they threaten, played as if I passed) or uci = null for "no real threat"."""
        data = _body()
        c = conn()
        row = c.execute("""SELECT p.*, g.my_color FROM positions p JOIN games g ON g.id = p.game_id
                           WHERE p.game_id = ? AND p.ply = ?""", (_int(data, "game_id"), _int(data, "ply"))).fetchone()
        if row is None:
            return jsonify(error="unknown position"), 404
        me = chess.WHITE if row["my_color"] == "white" else chess.BLACK
        th = level.current_thresholds(c, cfg)
        threat = threat_of(row, me, th)
        if threat is None:
            return jsonify(error="no threat data for this position"), 404
        out = {"threat": threat.as_dict(row["fen"]), "threshold": th.mistake}
        uci = data.get("uci")
        if not uci:
            out["correct"] = not threat.real
            return jsonify(out)
        passed = chess.Board(row["fen"])
        passed.push(chess.Move.null())
        try:
            move = chess.Move.from_uci(str(uci))
        except ValueError:
            return jsonify(error=f"illegal move {uci}"), 400
        if move not in passed.legal_moves:
            return jsonify(error=f"illegal move {uci}"), 400
        out["san"] = passed.san(move)
        if uci == threat.uci:
            out["your_gain"], out["correct"] = round(threat.gain, 3), threat.real
            return jsonify(out)
        # Any other move that would hurt me as much counts too (several threats at once).
        after_board = passed.copy(stack=False)
        after_board.push(move)
        before = Eval.from_row(row, me)
        after = live.analyse(after_board).for_side(me)
        gain = gain_of_move(before, after)
        out["your_gain"] = round(gain, 3)
        out["correct"] = threat.real and gain >= th.mistake
        return jsonify(out)

    @app.get("/api/analyse/stream")
    def analyse_stream():
        """Server-Sent Events with live multi-line analysis of `fen`."""
        fen = request.args.get("fen", "")
        try:
            chess.Board(fen)
        except ValueError:
            return jsonify(error="invalid FEN"), 400

        def events():
            for snapshot in live.stream(fen):
                yield f"data: {json.dumps(snapshot)}\n\n"

        return Response(events(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


def _query_mistakes(c: sqlite3.Connection, cfg: Config, where: list[str], params: list,
                    order: str) -> list[dict]:
    """My mistakes as puzzle items (shared by Practice, Train and the new-games session)."""
    rows = c.execute(f"""
        SELECT m.*, p.fen, p.move_uci, p.move_san, p.best_uci, p.pv_uci,
               p.score_cp AS before_cp, p.score_mate AS before_mate,
               p.score_cp, p.score_mate, p.threat_cp, p.threat_mate, p.threat_uci, p.threat_pv,
               p2.score_cp AS after_cp, p2.score_mate AS after_mate, p2.pv_uci AS after_pv,
               p0.move_uci AS prev_move_uci, p0.fen AS prev_fen,
               g.my_color, g.white, g.black, g.date, g.opening, g.opening_family,
               g.my_result, g.time_class, {STATUS_SQL} AS status,
               c.box AS card_box, c.due_on AS card_due
        FROM mistakes m
        JOIN positions p ON p.game_id = m.game_id AND p.ply = m.ply
        JOIN positions p2 ON p2.game_id = m.game_id AND p2.ply = m.ply + 1
        LEFT JOIN positions p0 ON p0.game_id = m.game_id AND p0.ply = m.ply - 1
        LEFT JOIN cards c ON c.game_id = m.game_id AND c.ply = m.ply
        JOIN games g ON g.id = m.game_id
        WHERE {' AND '.join(where)}
        ORDER BY {order}
    """, params).fetchall()
    th = level.current_thresholds(c, cfg)
    return [_mistake_json(r, cfg, th) for r in rows]


def _prev_capture(prev_fen: str | None, prev_uci: str | None) -> chess.Square | None:
    """Where the opponent's last move (from the stored position before it) captured, if it did."""
    if not prev_fen or not prev_uci:
        return None
    return capture_square(chess.Board(prev_fen), chess.Move.from_uci(prev_uci))


def _stored_analysis(pos: sqlite3.Row) -> PositionAnalysis:
    pv = tuple(chess.Move.from_uci(u) for u in (pos["pv_uci"] or "").split())
    best = chess.Move.from_uci(pos["best_uci"]) if pos["best_uci"] else None
    return PositionAnalysis(Eval(pos["score_cp"], pos["score_mate"]), best, pv)


# ------------------------------------------------------------ JSON helpers

def _eval_white(cp: int | None, mate: int | None) -> dict:
    return {"cp": cp, "mate": mate}


def _position_json(p: sqlite3.Row, mistake: dict | None) -> dict:
    out = {
        "ply": p["ply"], "fen": p["fen"], "move_uci": p["move_uci"], "move_san": p["move_san"],
        "best_uci": p["best_uci"], "eval": _eval_white(p["score_cp"], p["score_mate"]),
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
        out.append({"ply": ply, "fen": board.fen(), "move_uci": uci,
                    "move_san": board.san(move) if move else None, "eval": None})
        if move:
            board.push(move)
    return out


def _playout_moves(kind: str | None, before: Eval, cfg: Config) -> int:
    """How many of my moves a review puzzle asks for."""
    if kind == "missed_mate" and before.mate and before.mate > 0:
        return min(before.mate, 5)
    if kind in ATTACKING_KINDS:
        return cfg.review.playout_tactical
    return cfg.review.playout_other


def _mistake_json(r: sqlite3.Row, cfg: Config, th) -> dict:
    me = chess.WHITE if r["my_color"] == "white" else chess.BLACK
    board = chess.Board(r["fen"])
    best = chess.Move.from_uci(r["best_uci"]) if r["best_uci"] else None
    line = [chess.Move.from_uci(u) for u in (r["pv_uci"] or "").split()]
    before = Eval.from_row({"score_cp": r["before_cp"], "score_mate": r["before_mate"]}, me)
    after = Eval.from_row({"score_cp": r["after_cp"], "score_mate": r["after_mate"]}, me)
    ply = r["ply"]
    reply = [chess.Move.from_uci(u) for u in (r["after_pv"] or "").split()]
    good = r["judgement"] == "good"    # a calm position: I played well in the game
    best_explanation = explain_best(MoveContext(
        board=board, move=chess.Move.from_uci(r["move_uci"]), best=best, best_line=line,
        reply_line=reply, before=before, after=after, prev_capture=_prev_capture(r["prev_fen"], r["prev_move_uci"]),
        min_loss=th.min_loss), r["kind"]) if best and not good else ""
    threat = training_threat(r, me, th)
    return {
        "game_id": r["game_id"], "ply": ply,
        "move_label": f"{ply // 2 + 1}{'.' if ply % 2 == 0 else '...'}{r['move_san']}",
        "judgement": r["judgement"], "kind": r["kind"], "explanation": r["explanation"],
        "phase": r["phase"], "win_drop": r["win_drop"],
        "fen": r["fen"], "move_uci": r["move_uci"], "move_san": r["move_san"],
        "best_uci": r["best_uci"], "best_san": board.san(best) if best else None,
        "best_line": san_line(board, line, 6),
        "best_explanation": best_explanation,
        "best_pv_uci": [m.uci() for m in line],
        "eval_before": str(before), "eval_after": str(after),
        "eval_before_white": _eval_white(r["before_cp"], r["before_mate"]),
        "my_color": r["my_color"],
        "opponent": r["black"] if r["my_color"] == "white" else r["white"],
        "date": r["date"], "opening": r["opening"], "opening_family": r["opening_family"],
        "my_result": r["my_result"], "time_class": r["time_class"],
        "prev_move_uci": r["prev_move_uci"], "status": r["status"],
        "hint": _calm_hint(threat) if good else hint(r["kind"]), "playout_moves": _playout_moves(r["kind"], before, cfg),
        "card_box": r["card_box"], "card_due": r["card_due"],
        "mark": r["mark"], "threat": threat.as_dict(r["fen"]) if threat else None,
    }


def _calm_hint(threat) -> str:
    """Hint for a position I handled well in the game (no mistake kind to hint at)."""
    if threat is not None and threat.real:
        return "Your opponent is threatening something. Find it and deal with it first."
    return "No tricks here: look for a solid, useful move that keeps everything protected."


def _facets_match(row: sqlite3.Row, chosen: dict, skip: str | None = None) -> bool:
    return all(row[name] == value for name, value in chosen.items() if name != skip)


def _facet_options(rows: list[sqlite3.Row], chosen: dict) -> dict:
    """For each Practice facet, the values with matches under all the *other* chosen filters,
    with counts (B16: no options that lead to an empty list). A chosen value with no matches
    stays listed, with count 0, so the form still shows why the list is empty."""
    options = {}
    for name, _ in FACETS:
        counts = Counter(r[name] for r in rows if r[name] and _facets_match(r, chosen, skip=name))
        if chosen.get(name):
            counts.setdefault(chosen[name], 0)
        values = [v for v in FACET_ORDER.get(name, []) if v in counts] or sorted(counts, key=lambda v: (-counts[v], v))
        options[name] = [{"value": v, "count": counts[v]} for v in values]
    return options
