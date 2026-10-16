"""Flask web app: upload PGNs, watch analysis progress, browse games, review mistakes.

Pages render a small HTML shell; the data comes from the JSON API below and is
drawn by the scripts in static/.
"""

import json
import sqlite3
import threading

import chess
from flask import Flask, Response, abort, g, jsonify, render_template, request

from . import chesscom, db, insights, report, repertoire, session, srs
from .classify import Eval, ensure_classified
from .config import Config, load_config
from .engine import AnalysisJob, count_pending
from .explain import ATTACKING_KINDS, hint, san_line
from .live import LiveEngine, PositionAnalysis
from .pgn_import import ImportStats, import_pgn_text

JUDGEMENT_ORDER = ["good", "inaccuracy", "mistake", "blunder"]
REVIEW_RESULTS = {"solved", "solved_with_help", "failed", "skipped"}

# Latest review result for a mistake row `m` (NULL if never attempted).
STATUS_SQL = """(SELECT r.result FROM review_results r
                 WHERE r.game_id = m.game_id AND r.ply = m.ply ORDER BY r.id DESC LIMIT 1)"""
STATUS_FILTERS = {
    "new": f"{STATUS_SQL} IS NULL",
    "unsolved": f"({STATUS_SQL} IS NULL OR {STATUS_SQL} IN ('failed', 'skipped'))",
    "failed": f"{STATUS_SQL} IN ('failed', 'skipped')",
    "solved": f"{STATUS_SQL} IN ('solved', 'solved_with_help')",
}


def create_app(cfg: Config | None = None) -> Flask:
    cfg = cfg or load_config()
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB of PGN is plenty
    app.config["TEMPLATES_AUTO_RELOAD"] = True  # edited pages show up without a server restart

    state = {"job": AnalysisJob(cfg)}
    sync_lock = threading.Lock()
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

    # ------------------------------------------------------------ pages

    @app.get("/")
    def index():
        return render_template("index.html", username=cfg.player.username)

    @app.get("/game/<int:game_id>")
    def game_page(game_id: int):
        return render_template("game.html", game_id=game_id)

    @app.get("/review")
    def review_page():
        return render_template("review.html")

    @app.get("/games")
    def games_page():
        return render_template("games.html", username=cfg.player.username)

    @app.get("/settings")
    def settings_page():
        return render_template("settings.html", cfg=cfg)

    # ------------------------------------------------------------ upload + analysis

    @app.post("/api/upload")
    def upload():
        files = request.files.getlist("files")
        if not files:
            return jsonify(error="No files received"), 400
        total = ImportStats()
        batch_id = db.create_batch(conn(), "upload", f"Upload of {len(files)} file(s)")
        for f in files:
            text = f.read().decode("utf-8", errors="replace")
            total.merge(import_pgn_text(conn(), text, cfg, source=f.filename or "upload",
                                        batch_id=batch_id))
        db.finish_batch(conn(), batch_id)
        _start_job()
        return jsonify(files=total.files, games_seen=total.games_seen, imported=total.imported,
                       duplicates=total.duplicates, skipped=total.skipped)

    def _start_job():
        job = state["job"]
        if not job.is_running():
            state["job"] = job = AnalysisJob(cfg)
            job.start()

    @app.get("/api/sync")
    def sync_status():
        c = conn()
        return jsonify(username=cfg.player.username,
                       last_month=db.get_meta(c, chesscom.LAST_MONTH_KEY),
                       running=sync_lock.locked())

    @app.post("/api/sync")
    def sync_now():
        """Fetch new games from chess.com (body: {since: all|3m|1m} for the first sync)."""
        since = (request.get_json(silent=True) or {}).get("since")
        if not sync_lock.acquire(blocking=False):
            return jsonify(error="A sync is already running"), 409
        try:
            result = chesscom.sync(conn(), cfg, since=since)
        except LookupError as exc:
            return jsonify(error=str(exc)), 404
        except OSError as exc:  # network problems (URLError, timeouts)
            return jsonify(error=f"Could not reach chess.com: {exc}"), 502
        finally:
            sync_lock.release()
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
    def progress():
        c = conn()
        p = state["job"].progress.as_dict()
        p["pending"] = count_pending(c)
        return jsonify(p)

    # ------------------------------------------------------------ games

    @app.get("/api/games")
    def games():
        c = conn()
        ensure_classified(c, cfg)
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
        job = state["job"]
        job.stop()
        job.wait(timeout=30)
        deleted = db.delete_all_games(conn())
        return jsonify(deleted=deleted)

    @app.get("/api/game/<int:game_id>")
    def game(game_id: int):
        c = conn()
        ensure_classified(c, cfg)
        row = c.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        if row is None:
            abort(404)
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
        c = conn()
        ensure_classified(c, cfg)
        args = request.args
        where, params = ["m.is_mine = 1"], []
        judgements = [j for j in args.get("judgement", "mistake,blunder").split(",")
                      if j in JUDGEMENT_ORDER]
        where.append(f"m.judgement IN ({','.join('?' * len(judgements))})")
        params += judgements
        for arg, column in [("opening", "g.opening_family"), ("phase", "m.phase"),
                            ("result", "g.my_result"), ("time_class", "g.time_class"),
                            ("kind", "m.kind"), ("color", "g.my_color")]:
            if args.get(arg):
                where.append(f"{column} = ?")
                params.append(args[arg])
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
            "worst": "m.win_drop DESC",
            "newest": "g.end_time DESC, g.id DESC, m.ply",
            "oldest": "g.end_time, g.id, m.ply",
        }.get(args.get("sort", "worst"), "m.win_drop DESC")

        items = _query_mistakes(c, cfg, where, params, order)
        if args.get("low_clock"):
            items = [i for i in items if _low_clock(c, i)]
        return jsonify(items=items, options=_filter_options(c))

    def _low_clock(c, item) -> bool:
        """Was my clock under a minute when I made this mistake? (needs chess.com clocks)"""
        g = c.execute("SELECT clocks_ms, time_control FROM games WHERE id = ?", (item["game_id"],)).fetchone()
        if not g["clocks_ms"]:
            return False
        base, _ = report.parse_time_control(g["time_control"])
        left = insights.clock_before(g["clocks_ms"].split(), base, item["ply"])
        return left is not None and left < insights.LOW_CLOCK_MS

    # ------------------------------------------------------------ training (SRS) + new-games session

    @app.get("/insights")
    def insights_page():
        return render_template("insights.html")

    @app.get("/api/insights")
    def insights_data():
        c = conn()
        ensure_classified(c, cfg)
        return jsonify(insights.compute(c, request.args.get("time_class") or None,
                                        request.args.get("last", type=int)))

    @app.get("/openings")
    def openings_page():
        return render_template("openings.html")

    def _tree(color: str):
        return repertoire.build(conn(), color, request.args.get("family") or None)

    @app.get("/api/openings")
    def openings_overview():
        """Families I play per colour, repertoire problems and drill status."""
        c = conn()
        ensure_classified(c, cfg)
        color = request.args.get("color", "white")
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
        ensure_classified(c, cfg)
        color = request.args.get("color", "white")
        tree = _tree(color)
        fen = request.args.get("fen")
        epd = chess.Board(fen).epd() if fen else repertoire.root_epd()
        node = repertoire.node_json(tree, epd)
        if node is None:
            return jsonify(error="You haven't reached this position in your games."), 404
        return jsonify(node)

    @app.get("/api/openings/drill")
    def openings_drill():
        c = conn()
        ensure_classified(c, cfg)
        color = request.args.get("color", "white")
        tree = _tree(color)
        if request.args.get("mode") == "due":
            lines = repertoire.due_exercises(tree, color, srs.due_lines(c, color))
        else:
            lines = repertoire.drill_lines(tree, color, count=request.args.get("count", 6, type=int))
        return jsonify(lines=lines)

    @app.post("/api/openings/result")
    def openings_result():
        data = request.get_json(force=True)
        if data.get("result") not in REVIEW_RESULTS or not data.get("key"):
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
        ensure_classified(c, cfg)
        out = srs.stats(c, cfg)
        out["session"] = session.status(c, cfg)
        out["lines_due"] = len(srs.due_lines(c, "white")) + len(srs.due_lines(c, "black"))
        return jsonify(out)

    @app.get("/api/train/queue")
    def train_queue():
        c = conn()
        ensure_classified(c, cfg)
        q = srs.queue(c, cfg)
        items = _items_for(c, q["due"], "due") + _items_for(c, q["new"], "new")
        return jsonify(items=items, stats=srs.stats(c, cfg))

    def _items_for(c, keys: list[tuple[int, int]], tag: str) -> list[dict]:
        """Mistake items for (game_id, ply) keys, in the given order, tagged due/new."""
        if not keys:
            return []
        pairs = " OR ".join("(m.game_id = ? AND m.ply = ?)" for _ in keys)
        flat = [v for k in keys for v in k]
        by_key = {(i["game_id"], i["ply"]): i
                  for i in _query_mistakes(c, cfg, ["m.is_mine = 1", f"({pairs})"], flat, "m.game_id")}
        out = []
        for k in keys:
            if k in by_key:
                by_key[k]["queue"] = tag
                out.append(by_key[k])
        return out

    @app.get("/api/session")
    def session_info():
        c = conn()
        ensure_classified(c, cfg)
        st = session.status(c, cfg)
        if not st["open"]:
            return jsonify(st)
        where, params = session.puzzle_where(cfg, st["batch_ids"])
        st["items"] = _query_mistakes(c, cfg, ["m.is_mine = 1", where], params,
                                      "g.end_time, g.id, m.ply")
        return jsonify(st)

    @app.post("/api/session/done")
    def session_done():
        c = conn()
        batch_ids = [int(i) for i in (request.get_json(force=True) or {}).get("batch_ids", [])]
        summary = session.summary(c, cfg, batch_ids)
        session.mark_done(c, batch_ids)
        return jsonify(summary)

    @app.post("/api/judge")
    def judge_move():
        """Judge a move I try in review mode.

        Body: {fen, uci, game_id?, ply?, step?}. On the first step of a puzzle (step 0)
        the stored analysis of the game position is used, so the verdict matches the game.
        """
        data = request.get_json(force=True)
        fen, uci = data.get("fen"), data.get("uci")
        if not fen or not uci:
            return jsonify(error="fen and uci are required"), 400
        before = None
        if data.get("step", 0) == 0 and data.get("game_id") is not None:
            pos = conn().execute("SELECT * FROM positions WHERE game_id = ? AND ply = ?",
                                 (data["game_id"], data.get("ply"))).fetchone()
            if pos is not None and pos["fen"] == fen:
                before = _stored_analysis(pos)
        try:
            result = live.judge(fen, uci, before)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(result.as_dict())

    @app.post("/api/review/result")
    def review_result():
        data = request.get_json(force=True)
        if data.get("result") not in REVIEW_RESULTS:
            return jsonify(error="unknown result"), 400
        c = conn()
        with c:
            c.execute("""INSERT INTO review_results (game_id, ply, result, attempts, hints_used)
                         VALUES (?, ?, ?, ?, ?)""",
                      (data["game_id"], data["ply"], data["result"],
                       int(data.get("attempts", 0)), int(data.get("hints_used", 0))))
        srs.record(c, int(data["game_id"]), int(data["ply"]), data["result"])
        return jsonify(ok=True)

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
               p2.score_cp AS after_cp, p2.score_mate AS after_mate,
               p0.move_uci AS prev_move_uci,
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
    return [_mistake_json(r, cfg) for r in rows]


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


def _mistake_json(r: sqlite3.Row, cfg: Config) -> dict:
    me = chess.WHITE if r["my_color"] == "white" else chess.BLACK
    board = chess.Board(r["fen"])
    best = chess.Move.from_uci(r["best_uci"]) if r["best_uci"] else None
    line = [chess.Move.from_uci(u) for u in (r["pv_uci"] or "").split()]
    before = Eval.from_row({"score_cp": r["before_cp"], "score_mate": r["before_mate"]}, me)
    after = Eval.from_row({"score_cp": r["after_cp"], "score_mate": r["after_mate"]}, me)
    ply = r["ply"]
    return {
        "game_id": r["game_id"], "ply": ply,
        "move_label": f"{ply // 2 + 1}{'.' if ply % 2 == 0 else '...'}{r['move_san']}",
        "judgement": r["judgement"], "kind": r["kind"], "explanation": r["explanation"],
        "phase": r["phase"], "win_drop": r["win_drop"],
        "fen": r["fen"], "move_uci": r["move_uci"], "move_san": r["move_san"],
        "best_uci": r["best_uci"], "best_san": board.san(best) if best else None,
        "best_line": san_line(board, line, 6),
        "best_pv_uci": [m.uci() for m in line],
        "eval_before": str(before), "eval_after": str(after),
        "eval_before_white": _eval_white(r["before_cp"], r["before_mate"]),
        "my_color": r["my_color"],
        "opponent": r["black"] if r["my_color"] == "white" else r["white"],
        "date": r["date"], "opening": r["opening"], "opening_family": r["opening_family"],
        "my_result": r["my_result"], "time_class": r["time_class"],
        "prev_move_uci": r["prev_move_uci"], "status": r["status"],
        "hint": hint(r["kind"]), "playout_moves": _playout_moves(r["kind"], before, cfg),
        "card_box": r["card_box"], "card_due": r["card_due"],
    }


def _filter_options(c: sqlite3.Connection) -> dict:
    def distinct(sql: str) -> list[str]:
        return [r[0] for r in c.execute(sql) if r[0]]
    return {
        "opening": distinct("""SELECT g.opening_family FROM games g JOIN mistakes m ON m.game_id = g.id
                               WHERE m.is_mine = 1 AND m.judgement != 'good'
                               GROUP BY g.opening_family ORDER BY COUNT(*) DESC"""),
        "phase": ["opening", "middlegame", "endgame"],
        "result": ["win", "draw", "loss"],
        "time_class": distinct("SELECT DISTINCT time_class FROM games ORDER BY time_class"),
        "kind": distinct("""SELECT kind FROM mistakes WHERE is_mine = 1 AND kind IS NOT NULL
                            GROUP BY kind ORDER BY COUNT(*) DESC"""),
        "color": ["white", "black"],
        "status": ["unsolved", "new", "failed", "solved"],
    }
