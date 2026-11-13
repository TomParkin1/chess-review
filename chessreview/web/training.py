"""Today's plan, the new-games session, Insights, Progress and the Openings explorer."""

import sqlite3

from flask import Blueprint, Response, jsonify, request
from flask.typing import ResponseReturnValue

from .. import coach, db, insights, progress, repertoire, session, srs
from ..classify import refresh
from .common import REVIEW_RESULTS, board_from_fen, color_field, config, conn, int_field, json_body
from .puzzles import items_for, query_mistakes

bp = Blueprint("training", __name__)


@bp.get("/api/insights")
def insights_data() -> ResponseReturnValue:
    cfg = config()
    c = conn()
    refresh(c, cfg)
    return jsonify(insights.compute(c, request.args.get("time_class") or None, request.args.get("last", type=int)))


@bp.get("/api/insights/endgame-training")
def endgame_training() -> ResponseReturnValue:
    """Insights' endgame card: my endgame drills (essential endgames, won positions, puzzles)."""
    return jsonify(insights.drill_progress(conn()))


def _tree(color: str) -> dict[str, repertoire.Node]:
    return repertoire.build(conn(), color, request.args.get("family") or None)


@bp.get("/api/openings")
def openings_overview() -> ResponseReturnValue:
    """Families I play per colour, repertoire problems and drill status."""
    cfg = config()
    c = conn()
    refresh(c, cfg)
    color = color_field(request.args.get("color"))
    tree = _tree(color)
    families = [
        dict(r)
        for r in c.execute(
            """SELECT opening_family AS family, COUNT(*) AS games FROM games WHERE my_color = ?
           GROUP BY opening_family ORDER BY games DESC""",
            (color,),
        )
    ]
    return jsonify(
        families=families,
        problems=repertoire.problems(tree),
        stats=srs.line_stats(c, color),
        positions=sum(1 for n in tree.values() if n.my_turn and n.repertoire()[1]),
    )


@bp.get("/api/openings/node")
def openings_node() -> ResponseReturnValue:
    cfg = config()
    c = conn()
    refresh(c, cfg)
    color = color_field(request.args.get("color"))
    tree = _tree(color)
    fen = request.args.get("fen")
    epd = board_from_fen(fen).epd() if fen else repertoire.root_epd()
    node = repertoire.node_json(tree, epd)
    # Not an error (an empty database, or a move off my repertoire): the page says so.
    return jsonify(node or {"reached": False})


@bp.get("/api/openings/drill")
def openings_drill() -> ResponseReturnValue:
    cfg = config()
    c = conn()
    refresh(c, cfg)
    color = color_field(request.args.get("color"))
    mode = request.args.get("mode")
    tree = _tree(color)
    if mode == "due":
        lines = repertoire.due_exercises(tree, color, srs.due_lines(c, color))
    else:
        lines = repertoire.drill_lines(tree, color, count=request.args.get("count", 6, type=int))
    return jsonify(lines=lines)


@bp.post("/api/openings/result")
def openings_result() -> ResponseReturnValue:
    data = json_body()
    if data.get("result") not in REVIEW_RESULTS or not isinstance(data.get("key"), str) or not data["key"]:
        return jsonify(error="key and a valid result are required"), 400
    srs.record_line(conn(), data["key"], data["result"])
    return jsonify(ok=True)


@bp.get("/api/train/stats")
def train_stats() -> ResponseReturnValue:
    cfg = config()
    c = conn()
    refresh(c, cfg)
    out = srs.stats(c, cfg)
    out["session"] = session.status(c, cfg)
    out["lines_due"] = len(srs.due_lines(c, "white")) + len(srs.due_lines(c, "black"))
    return jsonify(out)


@bp.get("/api/today")
def today() -> ResponseReturnValue:
    """Today's workout plan with progress, plus streak, the real-game default and how far
    the games are (for the first run: no games yet / still analysing)."""
    cfg = config()
    c = conn()
    refresh(c, cfg)
    return _today_json(c, coach.today_plan(c, cfg))


@bp.post("/api/today/refresh")
def today_refresh() -> ResponseReturnValue:
    """Rebuild today's plan (e.g. after new games), keeping what was already tried today."""
    cfg = config()
    c = conn()
    refresh(c, cfg)
    return _today_json(c, coach.today_plan(c, cfg, refresh=True))


@bp.post("/api/today/budget")
def today_budget() -> ResponseReturnValue:
    """Body: {minutes: 10 | 20 | 30}. Today's plan is rebuilt for the new budget, keeping what
    was already tried today (its time counts)."""
    cfg = config()
    c = conn()
    try:
        coach.set_budget(c, int_field(json_body(), "minutes"))
    except (TypeError, ValueError) as exc:
        return jsonify(error=str(exc)), 400
    refresh(c, cfg)
    return _today_json(c, coach.today_plan(c, cfg, refresh=True))


def _today_json(c: sqlite3.Connection, plan: dict) -> Response:
    cfg = config()
    for block in plan["blocks"]:
        del block["items"]  # the keys are internal; /api/today/<block> serves the puzzles
    games = c.execute("SELECT COUNT(*) AS total, SUM(analysed_at IS NOT NULL) AS analysed FROM games").fetchone()
    return jsonify(
        plan=plan,
        streak=srs.streak(c),
        blind=cfg.training.blind,
        budgets=coach.BUDGETS,
        session=session.status(c, cfg),
        username=db.username(c, cfg),
        games={"total": games["total"], "analysed": games["analysed"] or 0},
    )


@bp.get("/api/today/<block_id>")
def today_block(block_id: str) -> ResponseReturnValue:
    """The puzzles of one block, each marked done_today. Reviews follow the real-game toggle
    (?blind=1|0), real-game positions are always blind, focus puzzles are labelled."""
    cfg = config()
    c = conn()
    plan = coach.today_plan(c, cfg)
    block = next((b for b in plan["blocks"] if b["id"] == block_id), None)
    if block is None:
        return jsonify(error="no such block today"), 404
    attempted = coach.attempted_today(c, srs.today())
    blind = request.args.get("blind", "1" if cfg.training.blind else "0") == "1"
    mode = {"reviews": "blind" if blind else None, "real": "blind"}.get(block_id)
    items = items_for(c, coach.block_items(plan, block_id))
    for item in items:
        if item.get("mode") == "library":
            item["done_today"] = ("lib", item["puzzle_id"]) in attempted
            continue
        item["mode"] = mode
        item["done_today"] = (item["game_id"], item["ply"]) in attempted
    return jsonify(items=items, block={k: v for k, v in block.items() if k != "items"})


@bp.get("/api/progress/report")
def progress_report() -> ResponseReturnValue:
    cfg = config()
    c = conn()
    refresh(c, cfg)
    return jsonify(progress.report(c, cfg))


@bp.get("/api/session")
def session_info() -> ResponseReturnValue:
    cfg = config()
    c = conn()
    refresh(c, cfg)
    st = session.status(c, cfg)
    if not st["open"]:
        return jsonify(st)
    where, params = session.puzzle_where(cfg, st["batch_ids"])
    st["items"] = query_mistakes(c, cfg, ["m.is_mine = 1", where], params, "g.end_time, g.id, m.ply")
    return jsonify(st)


@bp.get("/api/session/self-review")
def self_review_games() -> ResponseReturnValue:
    """Games of the open session to self-review (moves only: no evals, no marks)."""
    cfg = config()
    c = conn()
    refresh(c, cfg)  # loaded alongside /api/session: both must see the new games classified
    st = session.status(c, cfg)
    return jsonify(games=session.self_review_games(c, cfg, st["batch_ids"]) if st["open"] else [])


@bp.post("/api/session/self-review")
def self_review_pick() -> ResponseReturnValue:
    """Body: {game_id, ply} (ply of my move I think the game turned on, or null = not sure)."""
    data = json_body()
    ply = data.get("ply")
    try:
        return jsonify(session.record_self_review(conn(), int(data["game_id"]), None if ply is None else int(ply)))
    except LookupError, TypeError, ValueError:
        return jsonify(error="no turning point for this game"), 404


@bp.post("/api/session/done")
def session_done() -> ResponseReturnValue:
    cfg = config()
    c = conn()
    raw = (request.get_json(silent=True) or {}).get("batch_ids", [])
    if not isinstance(raw, list):
        return jsonify(error="batch_ids must be a list"), 400
    batch_ids = [int_field({"id": i}, "id") for i in raw]
    summary = session.summary(c, cfg, batch_ids)
    session.mark_done(c, batch_ids)
    return jsonify(summary)
