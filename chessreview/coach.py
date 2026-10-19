"""The "Today" workout: a short, structured daily plan built from my own data.

Blocks (sizes in config [coach]):
  reviews   due spaced-repetition cards, at most `due_cap` (the rest roll over to tomorrow)
  real      real-game positions: today's new mistake cards plus calm positions I played well
  focus     puzzles on my #1 weakness (insights.weaknesses over my last 100 games), recomputed
            daily, so the focus moves as I improve: half my own mistakes of that kind, half
            themed puzzles from the library near my puzzle rating (when the library is loaded)
  convert   one won position from my games to win against Stockfish (played on the Convert page)
  opening   due opening-line drills (played on the Openings page)

The plan is frozen for the day the first time it is built (stored in meta), so finishing
items doesn't reshuffle it. An item counts as done once it has a review result today.
It is rebuilt the same day only when it has to be (`today_plan`): on request ("Refresh plan"),
when some of its positions or puzzles no longer exist (Delete all games, a restore), or when
more games have been analysed since it was built and nothing in it has been tried yet (the
first sync). Otherwise new games mark it `stale`, and the Today page offers a refresh.
Items already tried today are carried over into a rebuilt plan, so no progress is lost.
"""

import json
import sqlite3
from datetime import date

from . import convert, db, insights, library, report, srs
from .config import Config

PLAN_KEY = "today_plan"
FOCUS_JUDGEMENTS = ("mistake", "blunder")


BLOCK_ORDER = ["reviews", "real", "focus", "convert", "opening"]


def today_plan(conn: sqlite3.Connection, cfg: Config, on: date | None = None, refresh: bool = False) -> dict:
    """Today's plan with progress: {day, blocks: [{id, title, ..., items: [[game_id, ply, tag]], done}],
    basis, stale, new_games}. `refresh` rebuilds it (keeping what was already tried today)."""
    on = on or srs.today()
    day = on.isoformat()
    basis = analysed_games(conn)
    stored = db.get_meta(conn, PLAN_KEY)
    plan = json.loads(stored) if stored else None
    if not plan or plan.get("day") != day:
        plan = _save(conn, build_plan(conn, cfg, on), basis)
    else:
        if "basis" not in plan:                # a plan stored before plans had a basis
            plan = _save(conn, plan, basis)
        items = [i for b in plan["blocks"] for i in b["items"]]
        attempted = attempted_today(conn, on)
        vanished = len(_servable(conn, items)) < len({_key(i) for i in items})
        untouched = not any(_key(i) in attempted for i in items)
        if refresh or vanished or (plan["basis"] != basis and untouched):
            plan = _save(conn, _rebuild(conn, cfg, on, plan, attempted), basis)
    plan["new_games"] = max(0, basis - plan["basis"])
    plan["stale"] = plan["new_games"] > 0
    return with_progress(conn, plan, on)


def analysed_games(conn: sqlite3.Connection) -> int:
    """What a plan was built from: the number of analysed games (more = the plan may be stale)."""
    return conn.execute("SELECT COUNT(*) FROM games WHERE analysed_at IS NOT NULL").fetchone()[0]


def _save(conn: sqlite3.Connection, plan: dict, basis: int) -> dict:
    plan["basis"] = basis
    with conn:
        db.set_meta(conn, PLAN_KEY, json.dumps(plan))
    return plan


def _rebuild(conn: sqlite3.Connection, cfg: Config, on: date, old: dict, attempted: set[tuple]) -> dict:
    """A fresh plan for the same day, with the items of `old` already tried today kept in their
    blocks (first), so the day's progress stays. They count towards each block's usual size, so
    a rebuild never adds a second workout to a day."""
    plan = build_plan(conn, cfg, on)
    sizes = {"reviews": cfg.coach.due_cap, "real": cfg.coach.real_game, "focus": cfg.coach.focus}
    tried = [i for b in old["blocks"] for i in b["items"] if _key(i) in attempted]
    still_there = _servable(conn, tried)
    kept = {_key(i) for i in tried if _key(i) in still_there}
    blocks = {b["id"]: b for b in plan["blocks"]}
    for old_block in old["blocks"]:
        done_elsewhere = old_block["id"] in ("opening", "convert") and _played_today(conn, old_block["id"], on)
        if old_block["id"] not in blocks and (done_elsewhere or any(_key(i) in kept for i in old_block["items"])):
            blocks[old_block["id"]] = {**old_block, "items": []}
    for block_id, b in blocks.items():
        old_block = next((o for o in old["blocks"] if o["id"] == block_id), {"items": []})
        carried = [i for i in old_block["items"] if _key(i) in kept]
        fresh = [i for i in b["items"] if _key(i) not in kept]
        b["items"] = carried + fresh[:max(0, sizes.get(block_id, len(fresh)) - len(carried))]
    plan["blocks"] = sorted((b for b in blocks.values() if b["items"] or b["id"] in ("opening", "convert")),
                            key=lambda b: BLOCK_ORDER.index(b["id"]) if b["id"] in BLOCK_ORDER else 99)
    return plan


def build_plan(conn: sqlite3.Connection, cfg: Config, on: date) -> dict:
    c = cfg.coach
    q = srs.queue(conn, cfg, on)
    reviews = [[g, p, "due"] for g, p in q["due"][: c.due_cap]]
    due_library = conn.execute("""SELECT key FROM cards WHERE kind = 'library' AND mastered_on IS NULL
                                  AND due_on IS NOT NULL AND due_on <= ? AND key IN (SELECT id FROM puzzles)
                                  ORDER BY due_on, box""",
                               (on.isoformat(),)).fetchall()
    reviews += [["lib", r["key"], "due"] for r in due_library][: max(0, c.due_cap - len(reviews))]

    n_calm = round(c.real_game * min(max(c.calm_ratio, 0.0), 0.8))
    new = [[g, p, "new"] for g, p in q["new"][: max(0, c.real_game - n_calm)]]
    calm = [[g, p, "calm"] for g, p in srs.calm_positions(conn, cfg, c.real_game - len(new), on.isoformat())]
    real = srs.spread(new, calm)

    taken = {(g, p) for g, p, _ in reviews + real}
    weakness, focus = _focus_items(conn, cfg, c.focus, taken, on)

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
    servable = _servable(conn, [i for b in blocks for i in b["items"]])
    for b in blocks:
        b["items"] = [i for i in b["items"] if _key(i) in servable]
    return {"day": on.isoformat(), "blocks": [b for b in blocks if b["items"] or b["id"] in ("opening", "convert")]}


def _key(item: list) -> tuple:
    """(game_id, ply) of my position or ("lib", puzzle_id) of a plan item [.., .., tag]."""
    return item[0], item[1]


def _servable(conn: sqlite3.Connection, items: list[list]) -> set[tuple]:
    """Keys of the items that can still be served as puzzles (same rows web._items_for needs):
    my analysed move with the position after it, or a puzzle still in the library."""
    mine = list({_key(i) for i in items if i[0] != "lib"})
    out: set[tuple] = set()
    for chunk in range(0, len(mine), 400):
        part = mine[chunk:chunk + 400]
        rows = conn.execute(f"""
            SELECT m.game_id, m.ply FROM mistakes m
            JOIN positions p ON p.game_id = m.game_id AND p.ply = m.ply
            JOIN positions p2 ON p2.game_id = m.game_id AND p2.ply = m.ply + 1
            WHERE m.is_mine = 1 AND (m.game_id, m.ply) IN (VALUES {",".join("(?, ?)" for _ in part)})""",
            [v for k in part for v in k])
        out |= {(r["game_id"], r["ply"]) for r in rows}
    lib = list({i[1] for i in items if i[0] == "lib"})
    if lib:
        rows = conn.execute(f"SELECT id FROM puzzles WHERE id IN ({','.join('?' * len(lib))})", lib)
        out |= {("lib", r["id"]) for r in rows}
    return out


def with_progress(conn: sqlite3.Connection, plan: dict, on: date) -> dict:
    """Mark items and blocks done from today's review results (and today's line drills)."""
    attempted = attempted_today(conn, on)
    total = done = 0
    for block in plan["blocks"]:
        if block["id"] in ("opening", "convert"):
            block["done"] = _played_today(conn, block["id"], on)
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


def _played_today(conn: sqlite3.Connection, block_id: str, on: date) -> bool:
    """A block played on its own page (opening lines, convert) counts once something was played today."""
    kind = "line" if block_id == "opening" else "convert"
    return conn.execute("SELECT 1 FROM cards WHERE kind = ? AND last_on = ? LIMIT 1",
                        (kind, on.isoformat())).fetchone() is not None


def attempted_today(conn: sqlite3.Connection, on: date) -> set[tuple]:
    """(game_id, ply) of my positions and ("lib", puzzle_id) of library puzzles tried today."""
    day = on.isoformat()
    mine = {(r["game_id"], r["ply"]) for r in conn.execute(
        "SELECT game_id, ply FROM review_results WHERE date(at, 'localtime') = ?", (day,))}
    return mine | {("lib", r["puzzle_id"]) for r in conn.execute(
        "SELECT puzzle_id FROM puzzle_attempts WHERE date(at, 'localtime') = ?", (day,))}


def block_items(plan: dict, block_id: str) -> list[tuple[tuple[int, int], str]]:
    """A block's items as ((game_id, ply), tag) pairs, in order ([] for an unknown block)."""
    block = next((b for b in plan["blocks"] if b["id"] == block_id), None)
    return [((g, p), tag) for g, p, tag in block["items"]] if block else []


# ------------------------------------------------------------ focus block

def _focus_items(conn: sqlite3.Connection, cfg: Config, size: int, taken: set[tuple],
                 on: date) -> tuple[dict | None, list[list]]:
    """My #1 weakness that has puzzles, and up to `size` of them: my own mistakes of that kind
    (unsolved first, best lessons first) and, with the library loaded, themed library puzzles."""
    has_library = library.count(conn) > 0
    for weakness in insights.compute(conn, last=100).get("weaknesses", []):   # none before any analysis
        train = weakness.get("train") or {}
        own = _matching_mistakes(conn, train, taken)
        themes = library.themes_for(train) if has_library else []
        if not own and not themes:
            continue
        own_n = min(len(own), (size + 1) // 2 if themes else size)
        lib = library.pick(conn, size - own_n, themes, seed=on.isoformat()) if themes else []
        items = [[g, p, "focus"] for g, p in own[:own_n]] + [["lib", i, "focus"] for i in lib]
        if items:
            return weakness, srs.spread(items[:own_n], items[own_n:])
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

