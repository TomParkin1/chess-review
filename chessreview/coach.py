"""The "Today" workout: a short, structured daily plan built from my own data.

Blocks (sizes in config [coach]):
  reviews   due spaced-repetition cards, at most `due_cap` (the rest roll over to tomorrow)
  real      real-game positions: today's new mistake cards plus calm positions I played well
  focus     puzzles on my #1 weakness (insights.weaknesses over my last 100 games), recomputed
            daily, so the focus moves as I improve
  convert   one won position from my games to win against Stockfish (played on the Convert page)
  opening   due opening-line drills (played on the Openings page)

The plan is frozen for the day the first time it is built (stored in meta), so finishing
items doesn't reshuffle it. An item counts as done once it has a review result today.
"""

import json
import sqlite3
from datetime import date

from . import convert, db, insights, report, srs
from .config import Config

PLAN_KEY = "today_plan"
FOCUS_JUDGEMENTS = ("mistake", "blunder")


def today_plan(conn: sqlite3.Connection, cfg: Config, on: date | None = None) -> dict:
    """Today's plan with progress: {day, blocks: [{id, title, ..., items: [[game_id, ply, tag]], done}]}."""
    on = on or srs.today()
    day = on.isoformat()
    stored = db.get_meta(conn, PLAN_KEY)
    plan = json.loads(stored) if stored else None
    if not plan or plan.get("day") != day:
        plan = build_plan(conn, cfg, on)
        with conn:
            db.set_meta(conn, PLAN_KEY, json.dumps(plan))
    return with_progress(conn, plan, on)


def build_plan(conn: sqlite3.Connection, cfg: Config, on: date) -> dict:
    c = cfg.coach
    q = srs.queue(conn, cfg, on)
    reviews = [[g, p, "due"] for g, p in q["due"][: c.due_cap]]

    n_calm = round(c.real_game * min(max(c.calm_ratio, 0.0), 0.8))
    new = [[g, p, "new"] for g, p in q["new"][: max(0, c.real_game - n_calm)]]
    calm = [[g, p, "calm"] for g, p in srs.calm_positions(conn, cfg, c.real_game - len(new), on.isoformat())]
    real = srs.spread(new, calm)

    taken = {(g, p) for g, p, _ in reviews + real}
    weakness, focus = _focus_items(conn, cfg, c.focus, taken)

    blocks = [
        {"id": "reviews", "title": "Review", "items": reviews,
         "subtitle": "Positions you got wrong before, back at the right time."},
        {"id": "real", "title": "Real-game positions", "items": real,
         "subtitle": "Spot the threat, then play your move. Not every position hides a mistake."},
        {"id": "focus", "title": f"Focus: {weakness['title']}" if weakness else "Focus", "items": focus,
         "subtitle": weakness["detail"] if weakness else "", "train": weakness["train"] if weakness else None},
    ]
    key = convert.next_convert(conn, on)
    task = convert.task_for(conn, key) if key else None
    if task:
        blocks.append({"id": "convert", "title": "Convert a won position", "items": [], "key": key,
                       "subtitle": f"{task.title} ({task.start_eval}): win it against Stockfish. {task.subtitle}."})
    lines = len(srs.due_lines(conn, "white", on)) + len(srs.due_lines(conn, "black", on))
    if lines:
        blocks.append({"id": "opening", "title": "Opening lines", "items": [], "lines_due": lines,
                       "subtitle": f"{lines} position{'s' if lines != 1 else ''} of your repertoire to drill."})
    return {"day": on.isoformat(), "blocks": [b for b in blocks if b["items"] or b["id"] in ("opening", "convert")]}


def with_progress(conn: sqlite3.Connection, plan: dict, on: date) -> dict:
    """Mark items and blocks done from today's review results (and today's line drills)."""
    day = on.isoformat()
    attempted = attempted_today(conn, on)
    total = done = 0
    for block in plan["blocks"]:
        if block["id"] in ("opening", "convert"):
            kind = "line" if block["id"] == "opening" else "convert"
            played = conn.execute("SELECT COUNT(*) FROM cards WHERE kind = ? AND last_on = ?",
                                  (kind, day)).fetchone()[0]
            block["done"] = played > 0
            block["count"], block["finished"] = 1, int(block["done"])
        else:
            block["count"] = len(block["items"])
            block["finished"] = sum((g, p) in attempted for g, p, _ in block["items"])
            block["done"] = block["finished"] == block["count"]
        total += block["count"]
        done += block["finished"]
    plan["total"], plan["finished"] = total, done
    plan["complete"] = all(b["done"] for b in plan["blocks"])
    return plan


def attempted_today(conn: sqlite3.Connection, on: date) -> set[tuple[int, int]]:
    return {(r["game_id"], r["ply"]) for r in conn.execute(
        "SELECT game_id, ply FROM review_results WHERE date(at, 'localtime') = ?", (on.isoformat(),))}


def block_items(plan: dict, block_id: str) -> list[tuple[tuple[int, int], str]]:
    """A block's items as ((game_id, ply), tag) pairs, in order ([] for an unknown block)."""
    block = next((b for b in plan["blocks"] if b["id"] == block_id), None)
    return [((g, p), tag) for g, p, tag in block["items"]] if block else []


# ------------------------------------------------------------ focus block

def _focus_items(conn: sqlite3.Connection, cfg: Config, size: int,
                 taken: set[tuple[int, int]]) -> tuple[dict | None, list[list]]:
    """My #1 weakness that has puzzles, and up to `size` of them (unsolved first, best lessons first)."""
    for weakness in insights.compute(conn, last=100)["weaknesses"]:
        keys = _matching_mistakes(conn, weakness.get("train") or {}, taken)
        if keys:
            return weakness, [[g, p, "focus"] for g, p in keys[:size]]
    return None, []


def _matching_mistakes(conn: sqlite3.Connection, train: dict, taken: set[tuple[int, int]]) -> list[tuple[int, int]]:
    """My mistakes matching a weakness' Practice filter (see insights.weaknesses)."""
    if not train:
        return []    # e.g. "moving too fast" has no position-level filter
    where = ["m.is_mine = 1", "m.judgement IN ('mistake', 'blunder')", "m.decided = 0",
             "g.id IN (SELECT id FROM games ORDER BY end_time DESC, id DESC LIMIT 100)",
             "NOT EXISTS (SELECT 1 FROM cards c WHERE c.game_id = m.game_id AND c.ply = m.ply"
             " AND c.mastered_on IS NOT NULL)"]
    params: list = []
    for key, column in (("kind", "m.kind"), ("phase", "m.phase"), ("opening", "g.opening_family"),
                        ("color", "g.my_color")):
        if train.get(key):
            where.append(f"{column} = ?")
            params.append(train[key])
    rows = conn.execute(f"""
        SELECT m.game_id, m.ply, g.clocks_ms, g.time_control,
               (SELECT r.result FROM review_results r WHERE r.game_id = m.game_id AND r.ply = m.ply
                ORDER BY r.id DESC LIMIT 1) AS status
        FROM mistakes m JOIN games g ON g.id = m.game_id
        WHERE {' AND '.join(where)}
        ORDER BY (status IN ('solved', 'solved_with_help')), {srs.PUZZLE_VALUE} DESC""", params).fetchall()
    out = []
    for r in rows:
        if (r["game_id"], r["ply"]) in taken:
            continue
        if train.get("low_clock") and not _low_clock(r):
            continue
        out.append((r["game_id"], r["ply"]))
    return out


def _low_clock(row: sqlite3.Row) -> bool:
    if not row["clocks_ms"]:
        return False
    base, _ = report.parse_time_control(row["time_control"])
    left = insights.clock_before(row["clocks_ms"].split(), base, row["ply"])
    return left is not None and left < insights.LOW_CLOCK_MS

