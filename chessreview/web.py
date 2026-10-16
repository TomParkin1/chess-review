"""Flask web app: upload PGNs, watch analysis progress, browse games, review mistakes.

Pages render a small HTML shell; the data comes from the JSON API below and is
drawn by the scripts in static/.
"""

import sqlite3
import threading

import chess
import chess.engine
from flask import Flask, abort, g, jsonify, render_template, request

from . import db
from .classify import Eval, ensure_classified, judge
from .config import Config, load_config
from .engine import AnalysisJob, open_engine
from .explain import san_line
from .pgn_import import ImportStats, import_pgn_text

JUDGEMENT_ORDER = ["good", "inaccuracy", "mistake", "blunder"]


def create_app(cfg: Config | None = None) -> Flask:
    cfg = cfg or load_config()
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB of PGN is plenty

    state = {"job": AnalysisJob(cfg)}
    quick_engine = QuickEngine(cfg)

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

    # ------------------------------------------------------------ upload + analysis

    @app.post("/api/upload")
    def upload():
        files = request.files.getlist("files")
        if not files:
            return jsonify(error="No files received"), 400
        total = ImportStats()
        for f in files:
            text = f.read().decode("utf-8", errors="replace")
            total.merge(import_pgn_text(conn(), text, cfg, source=f.filename or "upload"))
        _start_job()
        return jsonify(files=total.files, games_seen=total.games_seen, imported=total.imported,
                       duplicates=total.duplicates, skipped=total.skipped)

    def _start_job():
        job = state["job"]
        if not job.is_running():
            state["job"] = job = AnalysisJob(cfg)
            job.start()

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
        p["pending"] = c.execute("SELECT COUNT(*) FROM games WHERE analysed_at IS NULL").fetchone()[0]
        return jsonify(p)

    # ------------------------------------------------------------ games

    @app.get("/api/games")
    def games():
        c = conn()
        ensure_classified(c, cfg)
        rows = c.execute("""
            SELECT g.id, g.date, g.white, g.black, g.white_elo, g.black_elo, g.my_color,
                   g.my_result, g.result, g.time_class, g.eco, g.opening, g.opening_family,
                   g.num_plies, g.termination, g.analysed_at IS NOT NULL AS analysed,
                   SUM(m.judgement = 'inaccuracy') AS inaccuracies,
                   SUM(m.judgement = 'mistake') AS mistakes,
                   SUM(m.judgement = 'blunder') AS blunders
            FROM games g LEFT JOIN mistakes m ON m.game_id = g.id
            GROUP BY g.id ORDER BY g.date DESC, g.id DESC
        """).fetchall()
        return jsonify([dict(r) for r in rows])

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
        game_json = {k: row[k] for k in row.keys() if k not in ("pgn", "moves_uci")}
        return jsonify(game=game_json, positions=out_positions)

    # ------------------------------------------------------------ review

    @app.get("/api/mistakes")
    def mistakes():
        c = conn()
        ensure_classified(c, cfg)
        args = request.args
        where, params = ["1 = 1"], []
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
        order = {
            "worst": "m.win_drop DESC",
            "newest": "g.date DESC, g.id DESC, m.ply",
            "oldest": "g.date, g.id, m.ply",
        }.get(args.get("sort", "worst"), "m.win_drop DESC")

        rows = c.execute(f"""
            SELECT m.*, p.fen, p.move_uci, p.move_san, p.best_uci, p.pv_uci,
                   p.score_cp AS before_cp, p.score_mate AS before_mate,
                   p2.score_cp AS after_cp, p2.score_mate AS after_mate,
                   g.my_color, g.white, g.black, g.date, g.opening, g.opening_family,
                   g.my_result, g.time_class
            FROM mistakes m
            JOIN positions p ON p.game_id = m.game_id AND p.ply = m.ply
            JOIN positions p2 ON p2.game_id = m.game_id AND p2.ply = m.ply + 1
            JOIN games g ON g.id = m.game_id
            WHERE {' AND '.join(where)}
            ORDER BY {order}
        """, params).fetchall()

        items = [_mistake_json(r) for r in rows]
        return jsonify(items=items, options=_filter_options(c))

    @app.post("/api/evaluate")
    def evaluate():
        """Judge a move I try in review mode against the stored eval of that position."""
        data = request.get_json(force=True)
        c = conn()
        pos = c.execute("SELECT * FROM positions WHERE game_id = ? AND ply = ?",
                        (data["game_id"], data["ply"])).fetchone()
        if pos is None:
            abort(404)
        board = chess.Board(pos["fen"])
        try:
            move = chess.Move.from_uci(data["uci"])
        except ValueError:
            abort(400)
        if move not in board.legal_moves:
            return jsonify(error="Illegal move"), 400
        me = board.turn
        san = board.san(move)
        board.push(move)
        after = quick_engine.evaluate(board, me)
        before = Eval.from_row(pos, me)
        judgement, drop, _ = judge(before, after, cfg)
        return jsonify(san=san, judgement=judgement, is_best=move.uci() == pos["best_uci"],
                       eval_before=str(before), eval_after=str(after), win_drop=drop)

    return app


class QuickEngine:
    """Evaluates single positions for /api/evaluate.

    A fresh Stockfish process is started per request (about 0.1s overhead): a long-lived
    one would keep Python from exiting, and these requests are rare.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.Lock()  # one evaluation at a time keeps the CPU free for analysis

    def evaluate(self, board: chess.Board, color: chess.Color) -> Eval:
        if board.is_checkmate():  # whoever is to move has been mated
            return Eval(None, 1 if board.turn != color else -1)
        if board.is_game_over():
            return Eval(0, None)
        with self._lock:
            engine = open_engine(self.cfg)
            try:
                info = engine.analyse(board, chess.engine.Limit(time=self.cfg.engine.time_per_position))
            finally:
                engine.quit()
        score = info["score"].pov(color)
        return Eval(score.score(), score.mate())


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


def _mistake_json(r: sqlite3.Row) -> dict:
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
        "eval_before": str(before), "eval_after": str(after),
        "my_color": r["my_color"],
        "opponent": r["black"] if r["my_color"] == "white" else r["white"],
        "date": r["date"], "opening": r["opening"], "opening_family": r["opening_family"],
        "my_result": r["my_result"], "time_class": r["time_class"],
    }


def _filter_options(c: sqlite3.Connection) -> dict:
    def distinct(sql: str) -> list[str]:
        return [r[0] for r in c.execute(sql) if r[0]]
    return {
        "opening": distinct("""SELECT g.opening_family FROM games g JOIN mistakes m ON m.game_id = g.id
                               WHERE m.judgement != 'good'
                               GROUP BY g.opening_family ORDER BY COUNT(*) DESC"""),
        "phase": ["opening", "middlegame", "endgame"],
        "result": ["win", "draw", "loss"],
        "time_class": distinct("SELECT DISTINCT time_class FROM games ORDER BY time_class"),
        "kind": distinct("""SELECT kind FROM mistakes WHERE kind IS NOT NULL
                            GROUP BY kind ORDER BY COUNT(*) DESC"""),
        "color": ["white", "black"],
    }
