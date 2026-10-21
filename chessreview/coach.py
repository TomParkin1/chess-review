"""The "Today" workout: a short daily plan built from my own data, sized to a time budget.

Blocks:
  reviews   due spaced-repetition cards, most overdue first (srs.due_cards)
  real      real-game positions: today's new mistake cards plus calm positions I played well
  focus     puzzles on my #1 weakness (insights.weaknesses over my last 100 games), recomputed
            daily, so the focus moves as I improve: half my own mistakes of that kind, half
            themed puzzles from the library near my puzzle rating (when the library is loaded)
  convert   one won position from my games to win against Stockfish (played on the Convert page)
  opening   due opening-line drills (played on the Openings page)

Sizing (`build_plan`). The budget is 10, 20 or 30 minutes (meta `today_budget`, default
coach.budget_minutes). Opening lines (LINE_SECONDS each, at most OPENING_SHARE of the budget)
and a Convert game (CONVERT_SECONDS, from CONVERT_MIN_BUDGET minutes, not while reviews are
waiting) come off first; the rest
is puzzles at my own pace (`pace`: my time per attempt in a sitting, times my attempts per
puzzle, so retries of failed puzzles fit in the budget too).
  - Reviews come first: at most coach.review_share of the puzzles, or all of them when more is
    due than the day holds, so a backlog shrinks instead of growing.
  - New cards only while the due reviews fill less than coach.new_share of the puzzles (and at
    most training.new_per_day a day, focus mistakes never studied included). Each new card is
    reviewed for months, so this is what keeps tomorrow's load inside the budget too
    (`python -m chessreview simulate` runs it for 90 days).
  - The rest: real-game positions (new cards + calm positions) and the focus block, half each.
    Places the focus block can't fill go to waiting reviews, then to calm positions.

The plan is frozen for the day (the training day, from 04:00: srs.today) the first time it is
built (stored in meta), so finishing items doesn't reshuffle it. An item counts as done once
it has a review result today; the opening block once its planned lines were drilled today or
are no longer due. The plan is rebuilt the same day only when it has to be (`today_plan`): on
request ("Refresh plan", a new budget), when some of its positions or puzzles no longer exist
(Delete all games, a restore), when it was stored before plans had a budget, or when more games have been analysed since it was built and
nothing in it has been tried yet (the first sync). Otherwise new games mark it `stale`, and the
Today page offers a refresh. Items already tried today are carried over into a rebuilt plan
(their time counts against the budget), so no progress is lost.
"""

import json
import sqlite3
from datetime import date, datetime
from statistics import mean

from . import convert, db, insights, library, report, repertoire, session, srs
from .config import Config

PLAN_KEY = "today_plan"
BUDGET_KEY = "today_budget"
BUDGETS = (10, 20, 30)          # minutes offered on the Today page
FOCUS_JUDGEMENTS = ("mistake", "blunder")

LINE_SECONDS = 15               # one opening position (the moves before it play themselves)
OPENING_SHARE = 0.15            # opening lines take at most this share of the budget
CONVERT_SECONDS = 180           # one Convert game
CONVERT_MIN_BUDGET = 20         # minutes: shorter budgets leave Convert out
SITTING_GAP = 300               # seconds: a longer pause between two attempts is a break
MIN_GAPS = 30                   # attempts needed before my own pace replaces coach.seconds_per_puzzle
PACE_RANGE = (20.0, 180.0)      # seconds per puzzle, whatever the data says


BLOCK_ORDER = ["reviews", "real", "focus", "convert", "opening"]
ELSEWHERE = ("opening", "convert")      # blocks played on their own page


def today_plan(conn: sqlite3.Connection, cfg: Config, on: date | None = None, refresh: bool = False) -> dict:
    """Today's plan with progress: {day, blocks: [{id, title, ..., items: [[game_id, ply, tag]], done}],
    budget, pace, minutes_left, basis, stale, new_games}. `refresh` rebuilds it (keeping what was
    already tried today)."""
    on = on or srs.today()
    day = on.isoformat()
    session.expire(conn, cfg)
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
        unsized = "budget" not in plan         # a plan stored before the time budget existed
        if refresh or vanished or unsized or (plan["basis"] != basis and untouched):
            plan = _save(conn, _rebuild(conn, cfg, on, plan, attempted), basis)
    plan["new_games"] = max(0, basis - plan["basis"])
    plan["stale"] = plan["new_games"] > 0
    return with_progress(conn, plan, on)


def analysed_games(conn: sqlite3.Connection) -> int:
    """What a plan was built from: the number of analysed games (more = the plan may be stale)."""
    return conn.execute("SELECT COUNT(*) FROM games WHERE analysed_at IS NOT NULL").fetchone()[0]


def budget(conn: sqlite3.Connection, cfg: Config) -> int:
    """Today's time budget in minutes (chosen on the Today page, else coach.budget_minutes)."""
    stored = db.get_meta(conn, BUDGET_KEY)
    return int(stored) if stored else cfg.coach.budget_minutes


def set_budget(conn: sqlite3.Connection, minutes: int) -> None:
    if minutes not in BUDGETS:
        raise ValueError(f"budget must be one of {BUDGETS} minutes")
    with conn:
        db.set_meta(conn, BUDGET_KEY, str(minutes))


def pace(conn: sqlite3.Connection, cfg: Config) -> float:
    """Seconds a puzzle of the plan takes me: my time per attempt (`attempt_seconds`) times my
    attempts per puzzle (a retry, Today's "Again"), both over my last 200 attempts."""
    rows = _recent_attempts(conn)
    per_puzzle = len(rows) / len({(key, at[:10]) for key, at in rows}) if rows else 1.0
    return min(max(attempt_seconds(conn, cfg) * per_puzzle, PACE_RANGE[0]), PACE_RANGE[1])


def attempt_seconds(conn: sqlite3.Connection, cfg: Config) -> float:
    """My average time between two attempts in one sitting (positions and library puzzles, my last
    200), or coach.seconds_per_puzzle until there are MIN_GAPS such gaps."""
    stamps = sorted(datetime.fromisoformat(at) for _, at in _recent_attempts(conn))
    gaps = [g for g in ((b - a).total_seconds() for a, b in zip(stamps, stamps[1:])) if 0 < g <= SITTING_GAP]
    return mean(gaps) if len(gaps) >= MIN_GAPS else cfg.coach.seconds_per_puzzle


def _recent_attempts(conn: sqlite3.Connection, n: int = 200) -> list[tuple[str, str]]:
    """(what was tried, when) for my last `n` attempts."""
    return [(r[0], r[1]) for r in conn.execute(
        """SELECT key, at FROM (SELECT game_id || ':' || ply AS key, at FROM review_results
                                UNION ALL SELECT puzzle_id, at FROM puzzle_attempts)
           WHERE at IS NOT NULL ORDER BY at DESC LIMIT ?""", (n,))]


def _save(conn: sqlite3.Connection, plan: dict, basis: int) -> dict:
    plan["basis"] = basis
    with conn:
        db.set_meta(conn, PLAN_KEY, json.dumps(plan))
    return plan


def _rebuild(conn: sqlite3.Connection, cfg: Config, on: date, old: dict, attempted: set[tuple]) -> dict:
    """A fresh plan for the same day, with the items of `old` already tried today kept in their
    blocks (first), so the day's progress stays. Their time, and that of an opening or Convert
    block already played, comes off the budget, so a rebuild never adds a second workout."""
    tried = [i for b in old["blocks"] for i in b["items"] if _key(i) in attempted]
    still_there = _servable(conn, tried)
    kept = {_key(i) for i in tried if _key(i) in still_there}
    played = {b["id"]: b for b in old["blocks"] if b["id"] in ELSEWHERE and _block_done(conn, b, on)}
    spent = len(kept) * pace(conn, cfg) + sum(_elsewhere_seconds(b) for b in played.values())
    plan = build_plan(conn, cfg, on, spent=spent, exclude=kept, skip=set(played))
    blocks = {b["id"]: b for b in plan["blocks"]} | played
    for old_block in old["blocks"]:
        carried = [i for i in old_block["items"] if _key(i) in kept]
        if not carried:
            continue
        block = blocks.setdefault(old_block["id"], {**old_block, "items": []})
        block["items"] = carried + [i for i in block["items"] if _key(i) not in kept]
    plan["blocks"] = sorted((b for b in blocks.values() if b["items"] or b["id"] in ELSEWHERE),
                            key=lambda b: BLOCK_ORDER.index(b["id"]) if b["id"] in BLOCK_ORDER else 99)
    return plan


def build_plan(conn: sqlite3.Connection, cfg: Config, on: date, spent: float = 0.0,
               exclude: set | frozenset = frozenset(), skip: set | frozenset = frozenset()) -> dict:
    """Today's blocks, sized to the budget (see the module docstring). For a rebuild, `spent`
    seconds of the budget are already used by the items in `exclude` and the blocks in `skip`."""
    c = cfg.coach
    minutes = budget(conn, cfg)
    seconds = minutes * 60 - spent
    pace_s = pace(conn, cfg)

    lines = [] if "opening" in skip else _due_line_keys(conn, on)
    lines = lines[: max(0, int(min(minutes * 60 * OPENING_SHARE, seconds) // LINE_SECONDS))]
    seconds -= len(lines) * LINE_SECONDS
    due = [k for k in srs.due_cards(conn, cfg, on) if k not in exclude]
    # Convert needs a long enough budget, and waits while more reviews are due than the day holds.
    key = None
    if "convert" not in skip and minutes >= CONVERT_MIN_BUDGET and len(due) <= (seconds - CONVERT_SECONDS) // pace_s:
        key = convert.next_convert(conn, on)
    task = convert.task_for(conn, key) if key else None
    if task:
        seconds -= CONVERT_SECONDS
    slots = max(0, int(seconds // pace_s))

    reviews = due[: slots if len(due) > slots else round(slots * c.review_share)]
    fresh = slots - len(reviews)
    allowance = max(0, min(round(slots * c.new_share) - len(due), fresh,
                           cfg.training.new_per_day - srs.introduced_on(conn, on)))
    real_size = (fresh + 1) // 2
    calm_min = round(real_size * min(max(c.calm_ratio, 0.0), 0.8))
    new = srs.new_cards(conn, cfg, min(allowance, real_size - calm_min), exclude)
    taken = set(due) | set(new) | set(exclude)
    weakness, focus = _focus_items(conn, cfg, fresh - real_size, taken, on, allowance - len(new))
    # Places the focus block can't fill go to waiting reviews first, then to calm positions.
    spare = fresh - real_size - len(focus)
    extra = min(spare, len(due) - len(reviews))
    reviews = due[: len(reviews) + extra]
    calm = srs.calm_positions(conn, cfg, real_size - len(new) + spare - extra, on.isoformat(), exclude)
    real = srs.spread([[g, p, "new"] for g, p in new], [[g, p, "calm"] for g, p in calm])
    waiting = len(due) - len(reviews)

    blocks = [
        {"id": "reviews", "title": "Review", "items": [[g, p, "due"] for g, p in reviews],
         "subtitle": "Positions you got wrong before, back at the right time."
                     + (f" {waiting} more {'is' if waiting == 1 else 'are'} waiting: they come first tomorrow."
                        if waiting else "")},
        {"id": "real", "title": "Real-game positions", "items": real,
         "subtitle": "Spot the threat, then play your move. Not every position hides a mistake."},
        {"id": "focus", "title": f"Focus: {weakness['title']}" if weakness else "Focus", "items": focus,
         "subtitle": weakness["detail"] if weakness else "", "train": weakness["train"] if weakness else None},
    ]
    if task:
        blocks.append({"id": "convert", "title": "Convert a won position", "items": [], "key": key,
                       "subtitle": f"{task.title} ({task.start_eval}): win it against Stockfish. {task.subtitle}."})
    if lines:
        blocks.append({"id": "opening", "title": "Opening lines", "items": [], "keys": lines,
                       "lines_due": len(lines),
                       "subtitle": f"{len(lines)} position{'s' if len(lines) != 1 else ''} of your repertoire to drill."})
    servable = _servable(conn, [i for b in blocks for i in b["items"]])
    for b in blocks:
        b["items"] = [i for i in b["items"] if _key(i) in servable]
    return {"day": on.isoformat(), "budget": minutes, "pace": round(pace_s, 1),
            "blocks": [b for b in blocks if b["items"] or b["id"] in ELSEWHERE]}


def _due_line_keys(conn: sqlite3.Connection, on: date) -> list[str]:
    """Due opening-line cards that can be drilled (their position is still in my repertoire),
    the longest due first."""
    drillable: set[str] = set()
    for color in ("white", "black"):
        due = srs.due_lines(conn, color, on)
        if due:
            tree = repertoire.build(conn, color)
            drillable |= {line[-1]["key"] for line in repertoire.due_exercises(tree, color, due)}
    return [r["key"] for r in conn.execute(
        """SELECT key FROM cards WHERE kind = 'line' AND mastered_on IS NULL AND due_on IS NOT NULL
           AND due_on <= ? ORDER BY due_on, box, id""", (on.isoformat(),)) if r["key"] in drillable]


def pending_lines(conn: sqlite3.Connection, keys: list[str], on: date) -> list[str]:
    """The planned opening lines not drilled yet today that are still due."""
    if not keys:
        return []
    day = on.isoformat()
    rows = conn.execute(f"""SELECT key FROM cards WHERE kind = 'line' AND key IN ({",".join("?" * len(keys))})
                            AND mastered_on IS NULL AND due_on <= ? AND COALESCE(last_on, '') != ?""",
                        [*keys, day, day])
    pending = {r["key"] for r in rows}
    return [k for k in keys if k in pending]


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
    """Mark items and blocks done from today's review results (and today's drills and Convert),
    with the minutes left at my pace."""
    attempted = attempted_today(conn, on)
    pace_s = plan.get("pace") or PACE_RANGE[1] / 2
    total = done = 0
    seconds_left = 0.0
    for block in plan["blocks"]:
        if block["id"] in ELSEWHERE:
            block["done"] = _block_done(conn, block, on)
            block["count"], block["finished"] = 1, int(block["done"])
            if block["id"] == "opening" and "keys" in block:
                block["lines_left"] = len(pending_lines(conn, block["keys"], on))
            seconds_left += 0 if block["done"] else _elsewhere_seconds(block, conn, on)
        else:
            block["count"] = len(block["items"])
            block["finished"] = sum((g, p) in attempted for g, p, _ in block["items"])
            block["done"] = block["finished"] == block["count"]
            seconds_left += (block["count"] - block["finished"]) * pace_s
        total += block["count"]
        done += block["finished"]
    plan["total"], plan["finished"] = total, done
    plan["complete"] = all(b["done"] for b in plan["blocks"])
    plan["minutes_left"] = round(seconds_left / 60)
    return plan


def _block_done(conn: sqlite3.Connection, block: dict, on: date) -> bool:
    """An opening block is done when its planned lines were drilled today or are no longer due;
    a Convert block once a won position was played today."""
    if block["id"] == "opening" and "keys" in block:
        return not pending_lines(conn, block["keys"], on)
    kind = "line" if block["id"] == "opening" else "convert"
    return conn.execute("SELECT 1 FROM cards WHERE kind = ? AND last_on = ? LIMIT 1",
                        (kind, on.isoformat())).fetchone() is not None


def _elsewhere_seconds(block: dict, conn: sqlite3.Connection | None = None, on: date | None = None) -> float:
    """Planned time of an opening or Convert block (what is left of it, given `conn` and `on`)."""
    if block["id"] == "convert":
        return CONVERT_SECONDS
    keys = block.get("keys", [])
    return LINE_SECONDS * (len(pending_lines(conn, keys, on)) if conn is not None else len(keys))


def attempted_today(conn: sqlite3.Connection, on: date) -> set[tuple]:
    """(game_id, ply) of my positions and ("lib", puzzle_id) of library puzzles tried today."""
    day = on.isoformat()
    mine = {(r["game_id"], r["ply"]) for r in conn.execute(
        f"SELECT game_id, ply FROM review_results WHERE {srs.day_sql('at')} = ?", (day,))}
    return mine | {("lib", r["puzzle_id"]) for r in conn.execute(
        f"SELECT puzzle_id FROM puzzle_attempts WHERE {srs.day_sql('at')} = ?", (day,))}


def block_items(plan: dict, block_id: str) -> list[tuple[tuple[int, int], str]]:
    """A block's items as ((game_id, ply), tag) pairs, in order ([] for an unknown block)."""
    block = next((b for b in plan["blocks"] if b["id"] == block_id), None)
    return [((g, p), tag) for g, p, tag in block["items"]] if block else []


# ------------------------------------------------------------ focus block

def _focus_items(conn: sqlite3.Connection, cfg: Config, size: int, taken: set[tuple],
                 on: date, new_left: int) -> tuple[dict | None, list[list]]:
    """My #1 weakness that has puzzles, and up to `size` of them: my own mistakes of that kind
    (unsolved first, best lessons first; at most `new_left` never studied, as they become new
    cards) and, with the library loaded, themed library puzzles."""
    if size <= 0:
        return None, []
    has_library = library.count(conn) > 0
    for weakness in insights.compute(conn, last=100).get("weaknesses", []):   # none before any analysis
        train = weakness.get("train") or {}
        own = _matching_mistakes(conn, train, taken, new_left)
        themes = library.themes_for(train) if has_library else []
        if not own and not themes:
            continue
        own_n = min(len(own), (size + 1) // 2 if themes else size)
        lib = library.pick(conn, size - own_n, themes, seed=on.isoformat()) if themes else []
        items = [[g, p, "focus"] for g, p in own[:own_n]] + [["lib", i, "focus"] for i in lib]
        if items:
            return weakness, srs.spread(items[:own_n], items[own_n:])
    return None, []


def _matching_mistakes(conn: sqlite3.Connection, train: dict, taken: set[tuple],
                       new_left: int) -> list[tuple[int, int]]:
    """My mistakes matching a weakness' Practice filter (see insights.weaknesses), at most
    `new_left` of them never studied. Mistakes of an open new-games session wait for it."""
    if not train:
        return []    # e.g. "moving too fast" has no position-level filter
    where = ["m.is_mine = 1", "m.judgement IN ('mistake', 'blunder')", "m.decided = 0",
             "g.id IN (SELECT id FROM games ORDER BY end_time DESC, id DESC LIMIT 100)",
             f"g.id NOT IN ({srs.OPEN_SESSION_GAMES})",
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
                ORDER BY r.id DESC LIMIT 1) AS status,
               COALESCE((SELECT c.due_on IS NULL FROM cards c WHERE c.game_id = m.game_id AND c.ply = m.ply), 1)
                   AS unseen
        FROM mistakes m JOIN games g ON g.id = m.game_id
        WHERE {' AND '.join(where)}
        ORDER BY (status IN ('solved', 'solved_with_help')), {srs.PUZZLE_VALUE} DESC""", params).fetchall()
    out = []
    for r in rows:
        if (r["game_id"], r["ply"]) in taken:
            continue
        if train.get("low_clock") and not _low_clock(r):
            continue
        if r["unseen"]:
            if new_left <= 0:
                continue
            new_left -= 1
        out.append((r["game_id"], r["ply"]))
    return out


def _low_clock(row: sqlite3.Row) -> bool:
    if not row["clocks_ms"]:
        return False
    base, _ = report.parse_time_control(row["time_control"])
    left = insights.clock_before(row["clocks_ms"].split(), base, row["ply"])
    return left is not None and left < insights.LOW_CLOCK_MS

