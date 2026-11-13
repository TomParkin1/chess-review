"""Practice: my mistakes as puzzles, judging tries, threat checks and live analysis."""

import json
import sqlite3
from collections import Counter
from collections.abc import Iterator
from typing import Any

import chess
from flask import Blueprint, Response, jsonify, request
from flask.typing import ResponseReturnValue

from .. import coach, insights, level, srs
from ..classify import Eval, refresh
from ..engine import find_stockfish
from ..threats import gain_of_move, threat_of
from .common import REVIEW_RESULTS, board_from_fen, config, conn, int_field, json_body, live_engine
from .puzzles import STATUS_SQL, items_for, last_capture_square, query_mistakes, stored_analysis

JUDGEMENT_ORDER = ["good", "inaccuracy", "mistake", "blunder"]

THREAT_RESULTS = {"correct", "wrong", "skipped"}

# Practice filters that narrow each other (B16): name -> column; fixed display order where it matters.


FACETS = [
    ("opening", "g.opening_family"),
    ("phase", "m.phase"),
    ("result", "g.my_result"),
    ("color", "g.my_color"),
    ("time_class", "g.time_class"),
    ("kind", "m.kind"),
]

FACET_ORDER = {
    "phase": ["opening", "middlegame", "endgame"],
    "result": ["win", "draw", "loss"],
    "color": ["white", "black"],
}

STATUS_FILTERS = {
    "new": f"{STATUS_SQL} IS NULL",
    "unsolved": f"({STATUS_SQL} IS NULL OR {STATUS_SQL} IN ('failed', 'skipped'))",
    "failed": f"{STATUS_SQL} IN ('failed', 'skipped')",
    "solved": f"{STATUS_SQL} IN ('solved', 'solved_with_help')",
}

MAX_LIMIT = 200  # puzzle items built in one request (one SQL term each)


bp = Blueprint("practice", __name__)


@bp.get("/api/mistakes")
def mistakes() -> ResponseReturnValue:
    """My mistakes for Practice, filtered. `keys` lists every match in order ("<game>:<ply>"),
    `items` the first `limit` of them as puzzles (all of them without `limit`), and `options`
    the values of each facet that have matches under the other filters, with counts."""
    cfg = config()
    c = conn()
    refresh(c, cfg)
    args = request.args
    where: list[str] = ["m.is_mine = 1"]
    params: list[Any] = []
    if args.get("judgement") == "miss":  # mistakes/blunders that let the opponent's error go
        where.append("m.mark = 'miss'")
    else:
        judgements = [j for j in args.get("judgement", "mistake,blunder").split(",") if j in JUDGEMENT_ORDER]
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
    rows = c.execute(
        f"""SELECT m.game_id, m.ply, {columns} FROM mistakes m JOIN games g ON g.id = m.game_id
                         WHERE {" AND ".join(where)} ORDER BY {order}""",
        params,
    ).fetchall()
    # In time trouble / played very fast when I made the mistake (needs chess.com clocks).
    for flag, test in (("low_clock", insights.in_time_trouble), ("fast", insights.fast_move)):
        if args.get(flag):
            clocks = {
                r["id"]: r
                for r in c.execute("SELECT id, clocks_ms, time_control FROM games WHERE clocks_ms IS NOT NULL")
            }
            rows = [
                r
                for r in rows
                if r["game_id"] in clocks
                and test(clocks[r["game_id"]]["clocks_ms"], clocks[r["game_id"]]["time_control"], r["ply"])
            ]
    chosen = {name: args[name] for name, _ in FACETS if args.get(name)}
    hits = [r for r in rows if _facets_match(r, chosen)]
    limit = args.get("limit", type=int)
    if limit is None:
        by_key = {
            (i["game_id"], i["ply"]): i
            for i in query_mistakes(
                c,
                cfg,
                where + [f"{column} = ?" for name, column in FACETS if name in chosen],
                params + [chosen[name] for name, _ in FACETS if name in chosen],
                order,
            )
        }
        items = [by_key[(r["game_id"], r["ply"])] for r in hits]
    else:
        items = items_for(
            c, cfg, [((r["game_id"], r["ply"]), "practice") for r in hits[: min(max(0, limit), MAX_LIMIT)]]
        )
    return jsonify(
        total=len(hits),
        keys=[f"{r['game_id']}:{r['ply']}" for r in hits],
        items=items,
        options=_facet_options(rows, chosen),
        analysed=coach.analysed_games(c),
    )


@bp.get("/api/mistakes/items")
def mistake_items() -> ResponseReturnValue:
    """Puzzle items for `keys` ("<game>:<ply>,...", at most 50), in that order: the next
    Practice session from the key list /api/mistakes returned."""
    cfg = config()
    keyed = []
    for key in request.args.get("keys", "").split(",")[:50]:
        game_id, _, ply = key.partition(":")
        if game_id.isdigit() and ply.isdigit():
            keyed.append(((int(game_id), int(ply)), "practice"))
    c = conn()
    refresh(c, cfg)
    return jsonify(items=items_for(c, cfg, keyed))


@bp.post("/api/judge")
def judge_move() -> ResponseReturnValue:
    """Judge a move I try in review mode.

    Body: {fen, uci, game_id?, ply?, step?}. On the first step of a puzzle (step 0)
    the stored analysis of the game position is used, so the verdict matches the game.
    """
    cfg = config()
    live = live_engine()
    data = json_body()
    fen, uci = data.get("fen"), data.get("uci")
    if not (isinstance(fen, str) and isinstance(uci, str) and fen and uci):
        return jsonify(error="fen and uci are required"), 400
    if not board_from_fen(fen).is_valid():  # Stockfish exits on impossible positions (no king, say)
        return jsonify(error="not a legal position"), 400
    before = prev_capture = None
    c = conn()
    thresholds = level.current_thresholds(c, cfg)  # tries are judged at my level *now*
    if data.get("game_id") is not None and int_field(data, "step", 0) == 0 and data.get("ply") is not None:
        pos = c.execute(
            "SELECT * FROM positions WHERE game_id = ? AND ply = ?",
            (int_field(data, "game_id"), int_field(data, "ply")),
        ).fetchone()
        if pos is not None and pos["fen"] == fen:
            before = stored_analysis(pos)
            prev = c.execute(
                "SELECT fen, move_uci FROM positions WHERE game_id = ? AND ply = ?", (data["game_id"], pos["ply"] - 1)
            ).fetchone()
            prev_capture = last_capture_square(prev["fen"], prev["move_uci"]) if prev else None
    try:
        result = live.judge(fen, uci, before, thresholds, prev_capture)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(result.as_dict())


@bp.post("/api/review/result")
def review_result() -> ResponseReturnValue:
    data = json_body()
    if data.get("result") not in REVIEW_RESULTS:
        return jsonify(error="unknown result"), 400
    threat_result = data.get("threat_result")
    if threat_result is not None and threat_result not in THREAT_RESULTS:
        return jsonify(error="unknown threat_result"), 400
    game_id, ply = int_field(data, "game_id"), int_field(data, "ply")
    attempts, hints = int_field(data, "attempts", 0), int_field(data, "hints_used", 0)
    c = conn()
    if (
        c.execute("SELECT 1 FROM mistakes WHERE game_id = ? AND ply = ? AND is_mine = 1", (game_id, ply)).fetchone()
        is None
    ):
        return jsonify(error="unknown position"), 404
    with c:
        c.execute(
            """INSERT INTO review_results (game_id, ply, result, attempts, hints_used, mode, threat_result)
                     VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                game_id,
                ply,
                data["result"],
                attempts,
                hints,
                "blind" if data.get("mode") == "blind" else "labelled",
                threat_result,
            ),
        )
    srs.record(c, game_id, ply, data["result"], kind="position" if data.get("calm") else "mistake")
    return jsonify(ok=True)


@bp.post("/api/threat/check")
def threat_check() -> ResponseReturnValue:
    """Did I spot the opponent's threat? Body: {game_id, ply, uci} (the opponent move I think
    they threaten, played as if I passed) or uci = null for "no real threat"."""
    cfg = config()
    live = live_engine()
    data = json_body()
    c = conn()
    row = c.execute(
        """SELECT p.*, g.my_color FROM positions p JOIN games g ON g.id = p.game_id
                       WHERE p.game_id = ? AND p.ply = ?""",
        (int_field(data, "game_id"), int_field(data, "ply")),
    ).fetchone()
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


@bp.get("/api/analyse/stream")
def analyse_stream() -> ResponseReturnValue:
    """Server-Sent Events with live multi-line analysis of `fen`."""
    live = live_engine()
    fen = request.args.get("fen", "")
    if not board_from_fen(fen).is_valid():
        return jsonify(error="not a legal position"), 400
    find_stockfish(config().engine.path)  # StockfishNotFound is a 503 now, not an empty stream later

    def events() -> Iterator[str]:
        for snapshot in live.stream(fen):
            yield f"data: {json.dumps(snapshot)}\n\n"

    return Response(
        events(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


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
