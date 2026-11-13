"""The "Today" workout: a short daily plan built from my own data, sized to a time budget.

Blocks:
  reviews   due spaced-repetition cards, most overdue first (srs.due_cards)
  convert   one endgame to play against Stockfish on the Convert page (convert.next_endgame):
            a due essential endgame or won position, else the next essential endgame, else a
            won position from my games never tried
  endgame   endgame puzzles: my own endgame mistakes (due ones first, then new ones) and, with
            the library loaded, Lichess endgame puzzles near my puzzle rating, one endgame type
            after another (library.pick_endgames)
  real      real-game positions: today's new mistake cards plus calm positions I played well
  focus     puzzles on my #1 weakness (insights.weaknesses over my last 100 games), recomputed
            daily, so the focus moves as I improve: half my own mistakes of that kind, half
            themed puzzles from the library near my puzzle rating (when the library is loaded)
Openings are practised elsewhere (Chessly), so Today has no opening lines (E2); /openings stays.

Sizing (`build_plan`). The budget is 10, 20 or 30 minutes (meta `today_budget`, default
coach.budget_minutes). Endgames come first: ENDGAME_SHARE of the budget holds the endgame to
play (ENDGAME_SECONDS for an essential endgame, CONVERT_SECONDS for a won position) and, in the
time left of that share, endgame puzzles. The rest is puzzles at my own pace (`pace`: my time per
attempt in a sitting, times my attempts per puzzle, so retries of failed puzzles fit in the
budget too).
  - Reviews come first: at most coach.review_share of the puzzles, or all of them when more is
    due than the day holds, so a backlog shrinks instead of growing. Due endgame cards are
    reviewed in the endgame block.
  - New cards only while the due reviews fill less than coach.new_share of the puzzles (and at
    most training.new_per_day a day, endgame and focus mistakes never studied included). Each new
    card is reviewed for months, so this is what keeps tomorrow's load inside the budget too
    (`python -m chessreview simulate` runs it for 90 days).
  - The rest: real-game positions (new cards + calm positions) and the focus block, half each.
    Places the focus block can't fill go to waiting reviews, then to calm positions.

The plan is frozen for the day (the training day, from 04:00: srs.today) the first time it is
built (stored in meta), so finishing items doesn't reshuffle it. An item counts as done once
it has a review result today; the endgame to play once its planned card was played today. The
plan is rebuilt the same day only when it has to be (`today_plan`): on request ("Refresh plan",
a new budget), when some of its positions or puzzles no longer exist (Delete all games, a
restore), when it was stored by an older planner (PLAN_VERSION) or before plans had a budget, or
when more games have been analysed since it was built and nothing in it has been tried yet (the
first sync). Otherwise new games mark it `stale`, and the Today page offers a refresh. Items
already tried today are carried over into a rebuilt plan (their time counts against the budget,
endgame items against the endgame share too), so no progress is lost.
"""

import json
import sqlite3
from collections.abc import Set as AbstractSet
from datetime import date, datetime
from itertools import pairwise
from statistics import mean
from typing import Any

from . import convert, db, insights, library, session, srs
from .config import Config

PLAN_KEY = "today_plan"
BUDGET_KEY = "today_budget"
BUDGETS = (10, 20, 30)  # minutes offered on the Today page
FOCUS_JUDGEMENTS = ("mistake", "blunder")

PLAN_VERSION = 3  # 2: no opening block, endgame blocks (E2); 3: focus `calc` link (E6)
ENDGAME_SHARE = 0.30  # endgames take this share of the budget
ENDGAME_SECONDS = 120  # one essential endgame
CONVERT_SECONDS = 180  # one won position from my games
SITTING_GAP = 300  # seconds: a longer pause between two attempts is a break
MIN_GAPS = 30  # attempts needed before my own pace replaces coach.seconds_per_puzzle
PACE_RANGE = (20.0, 180.0)  # seconds per puzzle, whatever the data says


# Weaknesses (mistake kinds) that calculation trains: Today's focus row then links to /calculate (E6),
# an extra outside the plan (not counted, no budget).
CALC_KINDS = ("missed_tactic", "fork", "discovered_attack", "missed_mate", "missed_opportunity")
BLOCK_ORDER = ["reviews", "convert", "endgame", "real", "focus"]
ELSEWHERE = ("convert",)  # blocks played on their own page


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
        if "basis" not in plan:  # a plan stored before plans had a basis
            plan = _save(conn, plan, basis)
        items = [i for b in plan["blocks"] for i in b["items"]]
        attempted = attempted_today(conn, on)
        vanished = len(_servable(conn, items)) < len({_key(i) for i in items})
        untouched = not any(_key(i) in attempted for i in items)
        unsized = "budget" not in plan  # a plan stored before the time budget existed
        outdated = plan.get("version") != PLAN_VERSION
        if refresh or vanished or unsized or outdated or (plan["basis"] != basis and untouched):
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
    per_puzzle = len(rows) / len({(key, day) for key, _, day in rows}) if rows else 1.0
    return min(max(attempt_seconds(conn, cfg) * per_puzzle, PACE_RANGE[0]), PACE_RANGE[1])


def attempt_seconds(conn: sqlite3.Connection, cfg: Config) -> float:
    """My average time between two attempts in one sitting (positions and library puzzles, my last
    200), or coach.seconds_per_puzzle until there are MIN_GAPS such gaps."""
    stamps = sorted(datetime.fromisoformat(at) for _, at, _ in _recent_attempts(conn))
    gaps = [g for g in ((b - a).total_seconds() for a, b in pairwise(stamps)) if 0 < g <= SITTING_GAP]
    return mean(gaps) if len(gaps) >= MIN_GAPS else cfg.coach.seconds_per_puzzle


def _recent_attempts(conn: sqlite3.Connection, n: int = 200) -> list[tuple[str, str, str]]:
    """(what was tried, when, its training day) for my last `n` attempts."""
    return [
        (r[0], r[1], r[2])
        for r in conn.execute(
            f"""SELECT key, at, {srs.day_sql("at")} FROM (SELECT game_id || ':' || ply AS key, at FROM review_results
                                UNION ALL SELECT puzzle_id, at FROM puzzle_attempts)
            WHERE at IS NOT NULL ORDER BY at DESC LIMIT ?""",
            (n,),
        )
    ]


def _save(conn: sqlite3.Connection, plan: dict, basis: int) -> dict:
    plan["basis"] = basis
    with conn:
        db.set_meta(conn, PLAN_KEY, json.dumps(plan))
    return plan


def _rebuild(conn: sqlite3.Connection, cfg: Config, on: date, old: dict, attempted: set[tuple]) -> dict:
    """A fresh plan for the same day, with the items of `old` already tried today kept in their
    blocks (first), so the day's progress stays. Their time, and that of an endgame already
    played, comes off the budget (endgame work off the endgame share too), so a rebuild never
    adds a second workout. Blocks the planner no longer has (opening lines) are dropped."""
    pace_s = pace(conn, cfg)
    known = [b for b in old["blocks"] if b["id"] in BLOCK_ORDER]
    tried = [i for b in known for i in b["items"] if _key(i) in attempted]
    still_there = _servable(conn, tried)
    kept = {_key(i) for i in tried if _key(i) in still_there}
    played = {b["id"]: b for b in known if b["id"] in ELSEWHERE and _block_done(conn, b, on)}
    played_s = sum(_elsewhere_seconds(b) for b in played.values())
    endgame_kept = sum(_key(i) in kept for b in known if b["id"] == "endgame" for i in b["items"])
    plan = build_plan(
        conn,
        cfg,
        on,
        spent=len(kept) * pace_s + played_s,
        exclude=kept,
        skip=set(played),
        endgame_spent=endgame_kept * pace_s + played_s,
    )
    blocks = {b["id"]: b for b in plan["blocks"]} | played
    for old_block in known:
        carried = [i for i in old_block["items"] if _key(i) in kept]
        if not carried:
            continue
        block = blocks.setdefault(old_block["id"], {**old_block, "items": []})
        block["items"] = carried + [i for i in block["items"] if _key(i) not in kept]
    plan["blocks"] = sorted(
        (b for b in blocks.values() if b["items"] or b["id"] in ELSEWHERE), key=lambda b: BLOCK_ORDER.index(b["id"])
    )
    return plan


def build_plan(
    conn: sqlite3.Connection,
    cfg: Config,
    on: date,
    spent: float = 0.0,
    exclude: set | frozenset = frozenset(),
    skip: set | frozenset = frozenset(),
    endgame_spent: float = 0.0,
) -> dict:
    """Today's blocks, sized to the budget (see the module docstring). For a rebuild, `spent`
    seconds of the budget are already used by the items in `exclude` and the blocks in `skip`,
    `endgame_spent` of them on endgames."""
    c = cfg.coach
    minutes = budget(conn, cfg)
    seconds = minutes * 60 - spent
    pace_s = pace(conn, cfg)
    due = [k for k in srs.due_cards(conn, cfg, on) if k not in exclude]

    # Endgames first: their share of the budget (less what today's endgames took already).
    endgame_s = min(seconds, max(0.0, minutes * 60 * ENDGAME_SHARE - endgame_spent))
    # No endgame to play before the first games are analysed (the first-run screens expect an empty plan).
    key = None if "convert" in skip or not analysed_games(conn) else convert.next_endgame(conn, on)
    task = convert.task_for(conn, key) if key else None
    if task and key and drill_seconds(key) <= endgame_s:
        seconds -= drill_seconds(key)
        endgame_s -= drill_seconds(key)
    else:
        task = None
    slots = max(0, int(seconds // pace_s))
    endgame_slots = min(slots, int(endgame_s // pace_s))
    # New cards: while the due reviews fill less than new_share of the day, at most new_per_day.
    new_room = max(
        0, min(round(slots * c.new_share) - len(due), cfg.training.new_per_day - srs.introduced_on(conn, on))
    )
    endgame, endgame_new = _endgame_items(conn, endgame_slots, due, set(exclude), on, new_room)
    in_endgame = {_key(i) for i in endgame}
    due = [k for k in due if k not in in_endgame]
    slots -= len(endgame)  # places the endgame block can't fill go to the other blocks

    reviews = due[: slots if len(due) > slots else round(slots * c.review_share)]
    fresh = slots - len(reviews)
    allowance = max(0, min(new_room - endgame_new, fresh))
    real_size = (fresh + 1) // 2
    calm_min = round(real_size * min(max(c.calm_ratio, 0.0), 0.8))
    taken = set(due) | set(exclude) | in_endgame
    new = srs.new_cards(conn, cfg, min(allowance, real_size - calm_min), taken)
    taken |= set(new)
    weakness, focus = _focus_items(
        conn, cfg, fresh - real_size, taken, on, allowance - len(new), {i[1] for i in endgame if i[0] == "lib"}
    )
    # Places the focus block can't fill go to waiting reviews first, then to calm positions.
    spare = fresh - real_size - len(focus)
    extra = min(spare, len(due) - len(reviews))
    reviews = due[: len(reviews) + extra]
    calm = srs.calm_positions(conn, cfg, real_size - len(new) + spare - extra, on.isoformat(), exclude)
    real = srs.spread([[g, p, "new"] for g, p in new], [[g, p, "calm"] for g, p in calm])
    waiting = len(due) - len(reviews)

    blocks: list[dict[str, Any]] = [
        {
            "id": "reviews",
            "title": "Review",
            "items": [[g, p, "due"] for g, p in reviews],
            "subtitle": "Positions you got wrong before, back at the right time."
            + (
                f" {waiting} more {'is' if waiting == 1 else 'are'} waiting: they come first tomorrow."
                if waiting
                else ""
            ),
        }
    ]
    if task:
        blocks.append(_drill_block(task))
    blocks += [
        {"id": "endgame", "title": "Endgame puzzles", "items": endgame, "subtitle": _endgame_subtitle(conn, endgame)},
        {
            "id": "real",
            "title": "Real-game positions",
            "items": real,
            "subtitle": "Spot the threat, then play your move. Not every position hides a mistake.",
        },
        {
            "id": "focus",
            "title": f"Focus: {weakness['title']}" if weakness else "Focus",
            "items": focus,
            "subtitle": weakness["detail"] if weakness else "",
            "train": weakness["train"] if weakness else None,
            "calc": weakness is not None and (weakness["train"] or {}).get("kind") in CALC_KINDS,
        },
    ]
    servable = _servable(conn, [i for b in blocks for i in b["items"]])
    for b in blocks:
        b["items"] = [i for i in b["items"] if _key(i) in servable]
    return {
        "day": on.isoformat(),
        "budget": minutes,
        "pace": round(pace_s, 1),
        "version": PLAN_VERSION,
        "blocks": [b for b in blocks if b["items"] or b["id"] in ELSEWHERE],
    }


def drill_seconds(key: str) -> float:
    """Planned time of the endgame to play: an essential endgame or a won position."""
    return ENDGAME_SECONDS if key.startswith("endgame/") else CONVERT_SECONDS


def _drill_block(task: convert.Task) -> dict:
    if task.kind == "endgame":
        return {
            "id": "convert",
            "title": f"Endgame: {task.title}",
            "items": [],
            "key": task.key,
            "subtitle": f"{task.subtitle}. Play it out against Stockfish.",
        }
    return {
        "id": "convert",
        "title": "Convert a won position",
        "items": [],
        "key": task.key,
        "subtitle": f"{task.title} ({task.start_eval}): win it against Stockfish. {task.subtitle}.",
    }


# ------------------------------------------------------------ endgame block


def _endgame_items(
    conn: sqlite3.Connection, size: int, due: list[tuple], exclude: set[tuple], on: date, new_left: int
) -> tuple[list[list], int]:
    """Up to `size` endgame puzzles, and how many of them are new cards: my due endgame cards
    (my positions and library puzzles) first, as many as there are (capped at half the block,
    they piled up: simulation, 30 min, backlog 55 -> 104), then my endgame mistakes never studied
    (at most `new_left`) up to half the block while the library has endgame puzzles, Lichess
    endgame puzzles for the rest (my own fill in when the library runs short)."""
    if size <= 0:
        return [], 0
    endgame_due = [k for k in due if k in _endgame_keys(conn, due)]
    fresh = _matching_mistakes(conn, {"phase": "endgame"}, set(due) | exclude, new_left, recent=None, unseen_only=True)
    own = endgame_due + fresh
    has_library = library.count(conn) > 0
    own_n = min(len(own), max(len(endgame_due), (size + 1) // 2) if has_library else size)
    lib = library.pick_endgames(conn, size - own_n, on, {k[1] for k in exclude if k[0] == "lib"}) if has_library else []
    own_n = min(len(own), size - len(lib))
    picked = own[:own_n]
    fresh_set = set(fresh)
    items = [[g, p, "new" if (g, p) in fresh_set else "due"] for g, p in picked] + [["lib", i, "endgame"] for i in lib]
    return srs.spread(items[:own_n], items[own_n:]), sum(k in fresh_set for k in picked)


def _endgame_keys(conn: sqlite3.Connection, keys: list[tuple]) -> set[tuple]:
    """The keys among these that are endgames: my positions in the endgame phase, library
    puzzles tagged as endgames."""
    out: set[tuple] = set()
    mine = [k for k in keys if k[0] != "lib"]
    for chunk in range(0, len(mine), 400):
        part = mine[chunk : chunk + 400]
        rows = conn.execute(
            f"""SELECT game_id, ply FROM mistakes WHERE phase = 'endgame' AND is_mine = 1
                                AND (game_id, ply) IN (VALUES {",".join("(?, ?)" for _ in part)})""",
            [v for k in part for v in k],
        )
        out |= {(r["game_id"], r["ply"]) for r in rows}
    lib = [k[1] for k in keys if k[0] == "lib"]
    if lib:
        rows = conn.execute(
            f"""SELECT id FROM puzzles WHERE themes LIKE '% endgame %'
                                AND id IN ({",".join("?" * len(lib))})""",
            lib,
        )
        out |= {("lib", r["id"]) for r in rows}
    return out


def _endgame_subtitle(conn: sqlite3.Connection, items: list[list]) -> str:
    """ "2 from your own games. Rook endgame, pawn endgame puzzles near your rating." """
    mine = sum(i[0] != "lib" for i in items)
    types = library.endgame_types(conn, [i[1] for i in items if i[0] == "lib"])
    parts = [f"{mine} from your own games"] if mine else []
    if types:
        parts.append(", ".join(types).lower() + " puzzles near your rating")
    return " ".join(p[0].upper() + p[1:] + "." for p in parts)


def _key(item: list) -> tuple:
    """(game_id, ply) of my position or ("lib", puzzle_id) of a plan item [.., .., tag]."""
    return item[0], item[1]


def _servable(conn: sqlite3.Connection, items: list[list]) -> set[tuple]:
    """Keys of the items that can still be served as puzzles (same rows web._items_for needs):
    my analysed move with the position after it, or a puzzle still in the library."""
    mine = list({_key(i) for i in items if i[0] != "lib"})
    out: set[tuple] = set()
    for chunk in range(0, len(mine), 400):
        part = mine[chunk : chunk + 400]
        rows = conn.execute(
            f"""
            SELECT m.game_id, m.ply FROM mistakes m
            JOIN positions p ON p.game_id = m.game_id AND p.ply = m.ply
            JOIN positions p2 ON p2.game_id = m.game_id AND p2.ply = m.ply + 1
            WHERE m.is_mine = 1 AND (m.game_id, m.ply) IN (VALUES {",".join("(?, ?)" for _ in part)})""",
            [v for k in part for v in k],
        )
        out |= {(r["game_id"], r["ply"]) for r in rows}
    lib = list({i[1] for i in items if i[0] == "lib"})
    if lib:
        rows = conn.execute(f"SELECT id FROM puzzles WHERE id IN ({','.join('?' * len(lib))})", lib)
        out |= {("lib", r["id"]) for r in rows}
    return out


def with_progress(conn: sqlite3.Connection, plan: dict, on: date) -> dict:
    """Mark items and blocks done from today's review results (and today's endgame played),
    with the minutes left at my pace."""
    attempted = attempted_today(conn, on)
    pace_s = plan.get("pace") or PACE_RANGE[1] / 2
    total = done = 0
    seconds_left = 0.0
    for block in plan["blocks"]:
        if block["id"] in ELSEWHERE:
            block["done"] = _block_done(conn, block, on)
            block["count"], block["finished"] = 1, int(block["done"])
            seconds_left += 0 if block["done"] else _elsewhere_seconds(block)
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
    """The endgame block is done once its planned endgame or won position was played today (not
    any other one: E2)."""
    kind, _, key = block.get("key", "").partition("/")
    return (
        conn.execute(
            "SELECT 1 FROM cards WHERE kind = ? AND key = ? AND last_on = ? LIMIT 1", (kind, key, on.isoformat())
        ).fetchone()
        is not None
    )


def _elsewhere_seconds(block: dict) -> float:
    """Planned time of the endgame to play."""
    return drill_seconds(block.get("key", ""))


def attempted_today(conn: sqlite3.Connection, on: date) -> set[tuple]:
    """(game_id, ply) of my positions and ("lib", puzzle_id) of library puzzles tried today."""
    day = on.isoformat()
    mine = {
        (r["game_id"], r["ply"])
        for r in conn.execute(f"SELECT game_id, ply FROM review_results WHERE {srs.day_sql('at')} = ?", (day,))
    }
    return mine | {
        ("lib", r["puzzle_id"])
        for r in conn.execute(f"SELECT puzzle_id FROM puzzle_attempts WHERE {srs.day_sql('at')} = ?", (day,))
    }


def block_items(plan: dict, block_id: str) -> list[tuple[tuple[int, int], str]]:
    """A block's items as ((game_id, ply), tag) pairs, in order ([] for an unknown block)."""
    block = next((b for b in plan["blocks"] if b["id"] == block_id), None)
    return [((g, p), tag) for g, p, tag in block["items"]] if block else []


# ------------------------------------------------------------ focus block


def _focus_items(
    conn: sqlite3.Connection,
    cfg: Config,
    size: int,
    taken: set[tuple],
    on: date,
    new_left: int,
    lib_taken: AbstractSet[str] = frozenset(),
) -> tuple[dict | None, list[list]]:
    """My #1 weakness that has puzzles, and up to `size` of them: my own mistakes of that kind
    (unsolved first, best lessons first; at most `new_left` never studied, as they become new
    cards) and, with the library loaded, themed library puzzles."""
    if size <= 0:
        return None, []
    has_library = library.count(conn) > 0
    for weakness in insights.compute(conn, last=100).get("weaknesses", []):  # none before any analysis
        train = weakness.get("train") or {}
        own = _matching_mistakes(conn, train, taken, new_left)
        themes = library.themes_for(train) if has_library else []
        if not own and not themes:
            continue
        own_n = min(len(own), (size + 1) // 2 if themes else size)
        lib = library.pick(conn, size - own_n, themes, exclude=set(lib_taken), seed=on.isoformat()) if themes else []
        items = [[g, p, "focus"] for g, p in own[:own_n]] + [["lib", i, "focus"] for i in lib]
        if items:
            return weakness, srs.spread(items[:own_n], items[own_n:])
    return None, []


def _matching_mistakes(
    conn: sqlite3.Connection,
    train: dict,
    taken: set[tuple],
    new_left: int,
    recent: int | None = 100,
    unseen_only: bool = False,
) -> list[tuple[int, int]]:
    """My mistakes matching a weakness' Practice filter (see insights.weaknesses) in my last
    `recent` games (None: all), at most `new_left` of them never studied (`unseen_only`: only
    those). Mistakes of an open new-games session wait for it."""
    if not train:
        return []  # a weakness without a position-level filter
    where = [
        "m.is_mine = 1",
        "m.judgement IN ('mistake', 'blunder')",
        "m.decided = 0",
        "g.id IN (SELECT id FROM games ORDER BY end_time DESC, id DESC LIMIT ?)",
        f"g.id NOT IN ({srs.OPEN_SESSION_GAMES})",
        "NOT EXISTS (SELECT 1 FROM cards c WHERE c.game_id = m.game_id AND c.ply = m.ply"
        " AND c.mastered_on IS NOT NULL)",
    ]
    params: list = [recent if recent is not None else -1]  # LIMIT -1: no limit
    for key, column in (
        ("kind", "m.kind"),
        ("phase", "m.phase"),
        ("opening", "g.opening_family"),
        ("color", "g.my_color"),
    ):
        if train.get(key):
            where.append(f"{column} = ?")
            params.append(train[key])
    rows = conn.execute(
        f"""
        SELECT m.game_id, m.ply, g.clocks_ms, g.time_control,
               (SELECT r.result FROM review_results r WHERE r.game_id = m.game_id AND r.ply = m.ply
                ORDER BY r.id DESC LIMIT 1) AS status,
               COALESCE((SELECT c.due_on IS NULL FROM cards c WHERE c.game_id = m.game_id AND c.ply = m.ply), 1)
                   AS unseen
        FROM mistakes m JOIN games g ON g.id = m.game_id
        WHERE {" AND ".join(where)}
        ORDER BY (status IN ('solved', 'solved_with_help')), {srs.PUZZLE_VALUE} DESC""",
        params,
    ).fetchall()
    out = []
    for r in rows:
        if (r["game_id"], r["ply"]) in taken:
            continue
        if train.get("low_clock") and not insights.in_time_trouble(r["clocks_ms"], r["time_control"], r["ply"]):
            continue
        if train.get("fast") and not insights.fast_move(r["clocks_ms"], r["time_control"], r["ply"]):
            continue
        if r["unseen"]:
            if new_left <= 0:
                continue
            new_left -= 1
        elif unseen_only:
            continue
        out.append((r["game_id"], r["ply"]))
    return out
