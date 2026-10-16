"""Flask web app: upload PGNs, watch analysis progress, browse games, review mistakes.

Pages render a small HTML shell; the data comes from the JSON API below and is
drawn by the scripts in static/.
"""

import json
import sqlite3
import threading

import chess
from flask import Flask, Response, abort, g, jsonify, render_template, request

from . import chesscom, coach, convert, db, insights, level, progress, report, repertoire, session, srs
from .classify import (ADAPTIVE_BANDS, MODE_KEY, Eval, adaptive_enabled, ensure_classified,
                       thresholds_for)
from .config import Config, effective_workers, load_config
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
        return render_template("settings.html", cfg=cfg, workers=effective_workers(cfg.engine))

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
    def analysis_progress():
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

    @app.get("/api/settings/thresholds")
    def thresholds_info():
        c = conn()
        rating = level.current_rating(c)
        adaptive = adaptive_enabled(c, cfg)
        th = thresholds_for(rating, cfg, True)
        std = thresholds_for(None, cfg, False)
        pct = lambda v: round(v * 50, 1)   # -1..1 win-chance drop -> win% points
        return jsonify(adaptive=adaptive, rating=rating, band=th.band,
                       adaptive_thresholds=[pct(th.inaccuracy), pct(th.mistake), pct(th.blunder)],
                       standard_thresholds=[pct(std.inaccuracy), pct(std.mistake), pct(std.blunder)],
                       bands=[{"below": b, "values": [pct(i), pct(m), pct(x)]} for b, i, m, x in ADAPTIVE_BANDS])

    @app.post("/api/settings/thresholds")
    def thresholds_set():
        adaptive = bool((request.get_json(force=True) or {}).get("adaptive"))
        c = conn()
        with c:
            db.set_meta(c, MODE_KEY, "adaptive" if adaptive else "standard")
        n = ensure_classified(c, cfg)    # the fingerprint changed: every game is reclassified
        mine = c.execute("""SELECT SUM(judgement = 'inaccuracy') AS i, SUM(judgement = 'mistake') AS m,
                                   SUM(judgement = 'blunder') AS b FROM mistakes WHERE is_mine = 1""").fetchone()
        return jsonify(adaptive=adaptive, games=n, inaccuracies=mine["i"] or 0, mistakes=mine["m"] or 0,
                       blunders=mine["b"] or 0)

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
        """Today's queue. Real-game mode (?blind=1, default from config) mixes in calm positions
        and asks the puzzle to hide every clue that a mistake was made."""
        c = conn()
        ensure_classified(c, cfg)
        blind = request.args.get("blind", "1" if cfg.training.blind else "0") == "1"
        if blind:
            items = _items_for(c, srs.real_game_deck(c, cfg))
            for item in items:
                item["mode"] = "blind"
        else:
            q = srs.queue(c, cfg)
            items = _items_for(c, [(k, "due") for k in q["due"]] + [(k, "new") for k in q["new"]])
        return jsonify(items=items, stats=srs.stats(c, cfg), blind=blind)

    @app.get("/api/today")
    def today():
        """Today's workout plan with progress, plus streak and the real-game default."""
        c = conn()
        ensure_classified(c, cfg)
        plan = coach.today_plan(c, cfg)
        for block in plan["blocks"]:
            del block["items"]      # the keys are internal; /api/today/<block> serves the puzzles
        return jsonify(plan=plan, streak=srs.streak(c), blind=cfg.training.blind,
                       session=session.status(c, cfg))

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
        ensure_classified(c, cfg)
        rating = level.current_rating(c)
        return jsonify(positions=convert.convert_overview(c), endgames=convert.endgame_overview(c),
                       next=convert.next_convert(c), rating=rating, winning_cp=level.winning_cp(rating),
                       levels=convert.LEVELS)

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
        data = request.get_json(force=True)
        c = conn()
        task = convert.task_for(c, data.get("key", ""))
        if task is None:
            return jsonify(error="unknown position"), 404
        try:
            board = _replay(task, data.get("moves", []))
            move = chess.Move.from_uci(data.get("uci", ""))
            if move not in board.legal_moves:
                raise ValueError(f"illegal move {data.get('uci')}")
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        me = chess.WHITE if task.color == "white" else chess.BLACK
        rating = level.current_rating(c)
        out = {"san": board.san(move), "uci": move.uci()}
        board.push(move)
        st = convert.state(task, board, None, int(data.get("streak", 0)), rating)
        if st.status == "playing":
            analysis = live.analyse(board)      # Stockfish's best reply; its eval is the position after it
            reply = analysis.best
            out |= {"reply_uci": reply.uci(), "reply_san": board.san(reply)}
            board.push(reply)
            ev = analysis.for_side(me)
            out |= {"eval": str(ev), "cp": ev.as_cp()}
            st = convert.state(task, board, ev, st.streak, rating)
        out |= {"fen": board.fen(), "status": st.status, "reason": st.reason, "streak": st.streak}
        return jsonify(out)

    @app.post("/api/play/hint")
    def play_hint():
        data = request.get_json(force=True)
        task = convert.task_for(conn(), data.get("key", ""))
        if task is None:
            return jsonify(error="unknown position"), 404
        try:
            board = _replay(task, data.get("moves", []))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        best = live.analyse(board).best
        return jsonify(uci=best.uci() if best else None, san=board.san(best) if best else None)

    @app.post("/api/convert/result")
    def convert_result():
        data = request.get_json(force=True)
        task = convert.task_for(conn(), data.get("key", ""))
        if task is None or data.get("result") not in REVIEW_RESULTS:
            return jsonify(error="unknown position or result"), 400
        srs.record_key(conn(), task.kind, task.card_key, data["result"])
        return jsonify(ok=True)

    @app.get("/progress")
    def progress_page():
        return render_template("progress.html")

    @app.get("/api/progress/report")
    def progress_report():
        c = conn()
        ensure_classified(c, cfg)
        return jsonify(progress.report(c, cfg))

    def _items_for(c, keyed: list[tuple[tuple[int, int], str]]) -> list[dict]:
        """Puzzle items for ((game_id, ply), tag) pairs, in the given order, tagged due/new/calm."""
        if not keyed:
            return []
        pairs = " OR ".join("(m.game_id = ? AND m.ply = ?)" for _ in keyed)
        flat = [v for k, _ in keyed for v in k]
        by_key = {(i["game_id"], i["ply"]): i
                  for i in _query_mistakes(c, cfg, ["m.is_mine = 1", f"({pairs})"], flat, "m.game_id")}
        out = []
        for k, tag in keyed:
            if k in by_key:
                out.append({**by_key[k], "queue": tag, "calm": tag == "calm" or by_key[k]["judgement"] == "good"})
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
        c = conn()
        thresholds = level.current_thresholds(c, cfg)   # tries are judged at my level *now*
        if data.get("game_id") is not None:
            if data.get("step", 0) == 0:
                pos = c.execute("SELECT * FROM positions WHERE game_id = ? AND ply = ?",
                                (data["game_id"], data.get("ply"))).fetchone()
                if pos is not None and pos["fen"] == fen:
                    before = _stored_analysis(pos)
        try:
            result = live.judge(fen, uci, before, thresholds)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(result.as_dict())

    @app.post("/api/review/result")
    def review_result():
        data = request.get_json(force=True)
        if data.get("result") not in REVIEW_RESULTS:
            return jsonify(error="unknown result"), 400
        threat_result = data.get("threat_result")
        if threat_result is not None and threat_result not in THREAT_RESULTS:
            return jsonify(error="unknown threat_result"), 400
        c = conn()
        with c:
            c.execute("""INSERT INTO review_results (game_id, ply, result, attempts, hints_used, mode, threat_result)
                         VALUES (?, ?, ?, ?, ?, ?, ?)""",
                      (data["game_id"], data["ply"], data["result"],
                       int(data.get("attempts", 0)), int(data.get("hints_used", 0)),
                       "blind" if data.get("mode") == "blind" else "labelled", threat_result))
        srs.record(c, int(data["game_id"]), int(data["ply"]), data["result"],
                   kind="position" if data.get("calm") else "mistake")
        return jsonify(ok=True)

    @app.post("/api/threat/check")
    def threat_check():
        """Did I spot the opponent's threat? Body: {game_id, ply, uci} (the opponent move I think
        they threaten, played as if I passed) or uci = null for "no real threat"."""
        data = request.get_json(force=True)
        c = conn()
        row = c.execute("""SELECT p.*, g.my_color FROM positions p JOIN games g ON g.id = p.game_id
                           WHERE p.game_id = ? AND p.ply = ?""", (data.get("game_id"), data.get("ply"))).fetchone()
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
        move = chess.Move.from_uci(uci)
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
        reply_line=reply, before=before, after=after), r["kind"]) if best and not good else ""
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
