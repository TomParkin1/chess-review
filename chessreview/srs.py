"""Spaced repetition for my mistakes (Leitner boxes).

Each mistake I should learn gets a card. A card that is solved first time without help
moves up a box and comes back after a longer interval; solved with hints/retries it
comes back tomorrow; failed or skipped it drops to box 0 (and the session repeats it).
After passing the longest interval the card is mastered and retired.

    box after a clean solve:  1    2    3     4     5     6 -> mastered
    next review in (days):    1    3    7    14    30    60

The daily queue is every due card plus a few new ones (config training.new_per_day).
Mistakes from a "new games" session that hasn't been done yet are left to that session.
New cards come best-lesson first (`PUZZLE_VALUE`), and "decided" positions are left out.

Card kinds: 'mistake' (my mistakes/blunders), 'position' (a position I handled well in the
game but failed in real-game mode), 'line' (opening repertoire positions).

`real_game_deck()` mixes calm positions (good moves of mine, with and without a real threat)
into the queue so a puzzle never gives away that there is a trick.
"""

import random
import sqlite3
from datetime import date, timedelta

import chess

from .config import Config
from .level import current_thresholds
from .threats import training_threat

INTERVALS = [1, 3, 7, 14, 30, 60]          # days, indexed by box - 1
MASTERED_BOX = len(INTERVALS)               # passing this box retires the card
SOLVED = {"solved", "solved_with_help"}

# How much there is to learn from a mistake, for ordering new cards: the size of the drop,
# vague kinds (as in insights.weaknesses) weighted down but never removed, plus a bonus when
# the right move was clearly the only good one (sharpness, from the second-best line).
PUZZLE_VALUE = """(m.win_drop * (CASE WHEN m.kind IN ('positional', 'missed_opportunity') THEN 0.7 ELSE 1 END)
                   + 0.5 * MIN(COALESCE(m.sharpness, 0), 0.5))"""


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


def record(conn: sqlite3.Connection, game_id: int, ply: int, result: str, on: date | None = None,
           kind: str = "mistake") -> None:
    """Update the card of a position after an attempt, creating it if needed. A calm position
    (kind 'position') only becomes a card once I fail it."""
    on = on or today()
    card = conn.execute("SELECT * FROM cards WHERE game_id = ? AND ply = ?", (game_id, ply)).fetchone()
    if card is None:
        if kind == "position" and result in SOLVED:
            return
        with conn:
            conn.execute("INSERT INTO cards (kind, game_id, ply) VALUES (?, ?, ?)", (kind, game_id, ply))
        card = conn.execute("SELECT * FROM cards WHERE game_id = ? AND ply = ?", (game_id, ply)).fetchone()
    box, due_on, mastered_on = schedule(card["box"], result, on)
    with conn:
        conn.execute(
            """UPDATE cards SET box = ?, due_on = ?, mastered_on = ?, last_result = ?, reps = reps + 1,
                   lapses = lapses + ?, introduced_on = COALESCE(introduced_on, ?), last_on = ? WHERE id = ?""",
            (box, due_on, mastered_on, result, int(result not in SOLVED), on.isoformat(), on.isoformat(), card["id"]))


def _open_session_games() -> str:
    """SQL for games whose 'new games' session hasn't been done (their mistakes wait for it)."""
    return """SELECT g.id FROM games g JOIN imports i ON i.id = g.batch_id WHERE i.session_done_at IS NULL"""


def queue(conn: sqlite3.Connection, cfg: Config, on: date | None = None) -> dict:
    """Today's training: due cards first (oldest due), then new cards (worst mistakes first)."""
    on = on or today()
    ensure_cards(conn, cfg)
    day = on.isoformat()
    judgements = ("'mistake', 'blunder'" + (", 'inaccuracy'" if cfg.training.include_inaccuracies else ""))
    # Mistake cards only while the move is still a mistake under the current thresholds (they
    # can change); calm-position cards always.
    due = conn.execute(f"""
        SELECT c.game_id, c.ply FROM cards c JOIN mistakes m ON m.game_id = c.game_id AND m.ply = c.ply
        WHERE c.mastered_on IS NULL AND c.due_on IS NOT NULL AND c.due_on <= ?
          AND ((c.kind = 'mistake' AND m.judgement IN ({judgements})) OR c.kind = 'position')
        ORDER BY c.due_on, c.box, c.id""", (day,)).fetchall()
    introduced_today = conn.execute(
        "SELECT COUNT(*) FROM cards WHERE introduced_on = ?", (day,)).fetchone()[0]
    new_slots = max(0, cfg.training.new_per_day - introduced_today)
    new = conn.execute(f"""
        SELECT c.game_id, c.ply FROM cards c JOIN mistakes m ON m.game_id = c.game_id AND m.ply = c.ply
        WHERE c.kind = 'mistake' AND c.due_on IS NULL AND c.mastered_on IS NULL
          AND m.judgement IN ({judgements}) AND m.decided = 0
          AND c.game_id NOT IN ({_open_session_games()})
        ORDER BY {PUZZLE_VALUE} DESC LIMIT ?""", (new_slots,)).fetchall()
    return {"due": [(r["game_id"], r["ply"]) for r in due],
            "new": [(r["game_id"], r["ply"]) for r in new]}


def stats(conn: sqlite3.Connection, cfg: Config, on: date | None = None) -> dict:
    on = on or today()
    q = queue(conn, cfg, on)
    row = conn.execute("""
        SELECT SUM(mastered_on IS NOT NULL) AS mastered,
               SUM(mastered_on IS NULL AND due_on IS NOT NULL) AS learning,
               SUM(due_on IS NULL AND mastered_on IS NULL) AS unseen
        FROM cards WHERE kind IN ('mistake', 'position')""").fetchone()
    done_today = conn.execute(
        "SELECT COUNT(*) FROM review_results WHERE date(at, 'localtime') = ?", (on.isoformat(),)).fetchone()[0]
    return {"due": len(q["due"]), "new": len(q["new"]), "done_today": done_today,
            "mastered": row["mastered"] or 0, "learning": row["learning"] or 0, "unseen": row["unseen"] or 0,
            "streak": streak(conn, on)}


def calm_positions(conn: sqlite3.Connection, cfg: Config, count: int, seed: str) -> list[tuple[int, int]]:
    """`count` positions from my recent games where I played a good move: half where the
    opponent had a real threat (I defended it), half quiet. Deterministic per `seed` (the day)."""
    if count <= 0:
        return []
    th = current_thresholds(conn, cfg)
    rows = conn.execute(f"""
        SELECT m.game_id, m.ply, g.my_color, p.fen, p.score_cp, p.score_mate, p.threat_cp, p.threat_mate,
               p.threat_uci, p.threat_pv, p0.fen AS prev_fen, p0.move_uci AS prev_move_uci
        FROM mistakes m JOIN positions p ON p.game_id = m.game_id AND p.ply = m.ply
        LEFT JOIN positions p0 ON p0.game_id = m.game_id AND p0.ply = m.ply - 1
        JOIN games g ON g.id = m.game_id
        WHERE m.is_mine = 1 AND m.judgement = 'good' AND m.decided = 0
          AND COALESCE(m.mark, '') NOT IN ('book', 'forced')
          AND m.game_id IN (SELECT id FROM games WHERE analysed_at IS NOT NULL
                            ORDER BY end_time DESC, id DESC LIMIT ?)
          AND m.game_id NOT IN ({_open_session_games()})
          AND NOT EXISTS (SELECT 1 FROM cards c WHERE c.game_id = m.game_id AND c.ply = m.ply)
        ORDER BY m.game_id, m.ply""", (cfg.coach.recent_games,)).fetchall()
    threatened, quiet = [], []
    for r in rows:
        # Mid-exchange positions are left out: taking back is no test.
        threat = training_threat(r, chess.WHITE if r["my_color"] == "white" else chess.BLACK, th)
        if threat is not None:
            (threatened if threat.real else quiet).append((r["game_id"], r["ply"]))
    rng = random.Random(seed)
    want_threat = min(len(threatened), (count + 1) // 2)
    picks = rng.sample(threatened, want_threat)
    picks += rng.sample(quiet, min(len(quiet), count - want_threat))
    rng.shuffle(picks)
    return picks


def real_game_deck(conn: sqlite3.Connection, cfg: Config, on: date | None = None) -> list[tuple[tuple[int, int], str]]:
    """Today's queue for real-game mode: [((game_id, ply), 'due' | 'new' | 'calm')], with calm
    positions spread evenly through it (cfg.coach.calm_ratio of the deck)."""
    on = on or today()
    q = queue(conn, cfg, on)
    cards = [(k, "due") for k in q["due"]] + [(k, "new") for k in q["new"]]
    if not cards:
        return []
    ratio = min(max(cfg.coach.calm_ratio, 0.0), 0.8)
    n_calm = round(len(cards) * ratio / (1 - ratio))
    calm = [(k, "calm") for k in calm_positions(conn, cfg, n_calm, on.isoformat())]
    return spread(cards, calm)


def spread(cards: list, calm: list) -> list:
    """Interleave calm positions evenly among the cards (card i sits at i, calm j in between)."""
    slots = [(float(i), c) for i, c in enumerate(cards)]
    slots += [((j + 1) * len(cards) / (len(calm) + 1) - 0.5, c) for j, c in enumerate(calm)]
    return [c for _, c in sorted(slots, key=lambda s: s[0])]


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
    record_key(conn, "line", key, result, on)


def record_key(conn: sqlite3.Connection, kind: str, key: str, result: str, on: date | None = None) -> None:
    """Update (or create) a keyed card: 'line', 'convert' or 'endgame'."""
    on = on or today()
    with conn:
        conn.execute("INSERT OR IGNORE INTO cards (kind, key) VALUES (?, ?)", (kind, key))
    card = conn.execute("SELECT * FROM cards WHERE kind = ? AND key = ?", (kind, key)).fetchone()
    box, due_on, mastered_on = schedule(card["box"], result, on)
    with conn:
        conn.execute(
            """UPDATE cards SET box = ?, due_on = ?, mastered_on = ?, last_result = ?, reps = reps + 1,
                   lapses = lapses + ?, introduced_on = COALESCE(introduced_on, ?), last_on = ? WHERE id = ?""",
            (box, due_on, mastered_on, result, int(result not in SOLVED), on.isoformat(), on.isoformat(), card["id"]))


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
