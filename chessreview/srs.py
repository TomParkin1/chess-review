"""Spaced repetition for my mistakes (Leitner boxes).

Each mistake I should learn gets a card. A card that is solved first time without help
moves up a box and comes back after a longer interval; solved with hints/retries it
comes back tomorrow; failed or skipped it drops to box 0 (and the session repeats it).
After passing the longest interval the card is mastered and retired.

    box after a clean solve:  1    2    3     4     5     6 -> mastered
    next review in (days):    1    3    7    14    30    60

The daily queue is every due card plus a few new ones (config training.new_per_day).
Mistakes from a "new games" session that hasn't been done yet are left to that session.
"""

import sqlite3
from datetime import date, timedelta

from .config import Config

INTERVALS = [1, 3, 7, 14, 30, 60]          # days, indexed by box - 1
MASTERED_BOX = len(INTERVALS)               # passing this box retires the card
SOLVED = {"solved", "solved_with_help"}


def today() -> date:
    return date.today()


def ensure_cards(conn: sqlite3.Connection, cfg: Config) -> int:
    """Create a card for every one of my mistakes that doesn't have one. Returns new cards."""
    judgements = ("'mistake', 'blunder'" + (", 'inaccuracy'" if cfg.training.include_inaccuracies else ""))
    with conn:
        cur = conn.execute(f"""
            INSERT OR IGNORE INTO cards (kind, game_id, ply)
            SELECT 'mistake', m.game_id, m.ply FROM mistakes m
            WHERE m.is_mine = 1 AND m.judgement IN ({judgements})
        """)
    return cur.rowcount


def schedule(box: int, result: str, on: date) -> tuple[int, str | None, str | None]:
    """Next (box, due_on, mastered_on) for a card in `box` after `result` on day `on`."""
    if result == "solved":
        box += 1
        if box > MASTERED_BOX:
            return MASTERED_BOX, None, on.isoformat()
        return box, (on + timedelta(days=INTERVALS[box - 1])).isoformat(), None
    if result == "solved_with_help":
        return max(box, 1), (on + timedelta(days=1)).isoformat(), None
    return 0, (on + timedelta(days=1)).isoformat(), None      # failed / skipped


def record(conn: sqlite3.Connection, game_id: int, ply: int, result: str, on: date | None = None) -> None:
    """Update the card of a mistake after an attempt (creating the card if needed)."""
    on = on or today()
    card = conn.execute("SELECT * FROM cards WHERE game_id = ? AND ply = ?", (game_id, ply)).fetchone()
    if card is None:
        with conn:
            conn.execute("INSERT INTO cards (kind, game_id, ply) VALUES ('mistake', ?, ?)", (game_id, ply))
        card = conn.execute("SELECT * FROM cards WHERE game_id = ? AND ply = ?", (game_id, ply)).fetchone()
    box, due_on, mastered_on = schedule(card["box"], result, on)
    with conn:
        conn.execute(
            """UPDATE cards SET box = ?, due_on = ?, mastered_on = ?, last_result = ?, reps = reps + 1,
                   lapses = lapses + ?, introduced_on = COALESCE(introduced_on, ?) WHERE id = ?""",
            (box, due_on, mastered_on, result, int(result not in SOLVED), on.isoformat(), card["id"]))


def _open_session_games() -> str:
    """SQL for games whose 'new games' session hasn't been done (their mistakes wait for it)."""
    return """SELECT g.id FROM games g JOIN imports i ON i.id = g.batch_id WHERE i.session_done_at IS NULL"""


def queue(conn: sqlite3.Connection, cfg: Config, on: date | None = None) -> dict:
    """Today's training: due cards first (oldest due), then new cards (worst mistakes first)."""
    on = on or today()
    ensure_cards(conn, cfg)
    day = on.isoformat()
    judgements = ("'mistake', 'blunder'" + (", 'inaccuracy'" if cfg.training.include_inaccuracies else ""))
    # Only cards whose move is still a mistake under the current thresholds (they can change).
    due = conn.execute(f"""
        SELECT c.game_id, c.ply FROM cards c JOIN mistakes m ON m.game_id = c.game_id AND m.ply = c.ply
        WHERE c.kind = 'mistake' AND c.mastered_on IS NULL AND c.due_on IS NOT NULL AND c.due_on <= ?
          AND m.judgement IN ({judgements})
        ORDER BY c.due_on, c.box, c.id""", (day,)).fetchall()
    introduced_today = conn.execute(
        "SELECT COUNT(*) FROM cards WHERE introduced_on = ?", (day,)).fetchone()[0]
    new_slots = max(0, cfg.training.new_per_day - introduced_today)
    new = conn.execute(f"""
        SELECT c.game_id, c.ply FROM cards c JOIN mistakes m ON m.game_id = c.game_id AND m.ply = c.ply
        WHERE c.kind = 'mistake' AND c.due_on IS NULL AND c.mastered_on IS NULL
          AND m.judgement IN ({judgements})
          AND c.game_id NOT IN ({_open_session_games()})
        ORDER BY m.win_drop DESC LIMIT ?""", (new_slots,)).fetchall()
    return {"due": [(r["game_id"], r["ply"]) for r in due],
            "new": [(r["game_id"], r["ply"]) for r in new]}


def stats(conn: sqlite3.Connection, cfg: Config, on: date | None = None) -> dict:
    on = on or today()
    q = queue(conn, cfg, on)
    row = conn.execute("""
        SELECT SUM(mastered_on IS NOT NULL) AS mastered,
               SUM(mastered_on IS NULL AND due_on IS NOT NULL) AS learning,
               SUM(due_on IS NULL AND mastered_on IS NULL) AS unseen
        FROM cards WHERE kind = 'mistake'""").fetchone()
    done_today = conn.execute(
        "SELECT COUNT(*) FROM review_results WHERE date(at, 'localtime') = ?", (on.isoformat(),)).fetchone()[0]
    return {"due": len(q["due"]), "new": len(q["new"]), "done_today": done_today,
            "mastered": row["mastered"] or 0, "learning": row["learning"] or 0, "unseen": row["unseen"] or 0,
            "streak": streak(conn, on)}


def streak(conn: sqlite3.Connection, on: date | None = None) -> int:
    """Consecutive days with at least one attempt, ending today (or yesterday)."""
    on = on or today()
    days = {date.fromisoformat(r[0]) for r in conn.execute(
        "SELECT DISTINCT date(at, 'localtime') FROM review_results")}
    day = on if on in days else on - timedelta(days=1)
    count = 0
    while day in days:
        count += 1
        day -= timedelta(days=1)
    return count


# ---------------------------------------------------------------- opening-line cards

def record_line(conn: sqlite3.Connection, key: str, result: str, on: date | None = None) -> None:
    """Update (or create) the card for one of my repertoire positions after a drill attempt."""
    on = on or today()
    with conn:
        conn.execute("INSERT OR IGNORE INTO cards (kind, key) VALUES ('line', ?)", (key,))
    card = conn.execute("SELECT * FROM cards WHERE kind = 'line' AND key = ?", (key,)).fetchone()
    box, due_on, mastered_on = schedule(card["box"], result, on)
    with conn:
        conn.execute(
            """UPDATE cards SET box = ?, due_on = ?, mastered_on = ?, last_result = ?, reps = reps + 1,
                   lapses = lapses + ?, introduced_on = COALESCE(introduced_on, ?) WHERE id = ?""",
            (box, due_on, mastered_on, result, int(result not in SOLVED), on.isoformat(), card["id"]))


def due_lines(conn: sqlite3.Connection, color: str, on: date | None = None) -> list[str]:
    on = on or today()
    return [r[0] for r in conn.execute(
        """SELECT key FROM cards WHERE kind = 'line' AND key LIKE ? AND mastered_on IS NULL
           AND due_on IS NOT NULL AND due_on <= ? ORDER BY due_on, box""",
        (f"{color}:%", on.isoformat()))]


def line_stats(conn: sqlite3.Connection, color: str, on: date | None = None) -> dict:
    on = on or today()
    row = conn.execute(
        """SELECT COUNT(*) AS seen, SUM(mastered_on IS NOT NULL) AS mastered,
                  SUM(mastered_on IS NULL AND due_on <= ?) AS due
           FROM cards WHERE kind = 'line' AND key LIKE ?""", (on.isoformat(), f"{color}:%")).fetchone()
    return {"seen": row["seen"] or 0, "mastered": row["mastered"] or 0, "due": row["due"] or 0}
