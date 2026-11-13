"""Spaced repetition for my mistakes (Leitner boxes).

Each mistake I should learn gets a card. A card that is solved first time without help
moves up a box and comes back after a longer interval; solved with hints/retries it
comes back tomorrow; failed or skipped it drops to box 0 (and the session repeats it).
After passing the longest interval the card is mastered and retired.

    box after a clean solve:  1    2    3     4     5     6 -> mastered
    next review in (days):    1    3    7    14    30    60

Only the first attempt of a training day moves a card (Retry, Today's "Again" and repeated
drill lines are practice), and before a card is due only a failure counts: solving it early
doesn't skip ahead. So no card can be mastered in fewer than sum(INTERVALS) days.
The training day starts at DAY_START_HOUR (04:00, as in Anki): a late workout stays "today".

The daily queue is every due card plus a few new ones (coach.py sizes both from the time
budget). Due cards come most overdue first, relative to their interval (`_review_order`).
Mistakes from a "new games" session that hasn't been done yet are left to that session.
New cards come best-lesson first (`PUZZLE_VALUE`), and "decided" positions are left out.

Card kinds: 'mistake' (my mistakes/blunders), 'position' (a position I handled well in the
game but failed in real-game mode), 'line' (opening repertoire positions).

`calm_positions()` picks good moves of mine (with and without a real threat) that Today mixes
into the real-game block, so a puzzle never gives away that there is a trick.
"""

import random
import sqlite3
from datetime import date, datetime, timedelta

import chess

from .config import Config
from .level import current_thresholds
from .threats import training_threat

INTERVALS = [1, 3, 7, 14, 30, 60]  # days, indexed by box - 1
MASTERED_BOX = len(INTERVALS)  # passing this box retires the card
SOLVED = {"solved", "solved_with_help"}

# How much there is to learn from a mistake, for ordering new cards: the size of the drop,
# vague kinds (as in insights.weaknesses) weighted down but never removed, plus a bonus when
# the right move was clearly the only good one (sharpness, from the second-best line).
PUZZLE_VALUE = """(m.win_drop * (CASE WHEN m.kind IN ('positional', 'missed_opportunity') THEN 0.7 ELSE 1 END)
                   + 0.5 * MIN(COALESCE(m.sharpness, 0), 0.5))"""


DAY_START_HOUR = 4  # the training day runs from 04:00 to 04:00


def today(now: datetime | None = None) -> date:
    """The training day: before 04:00 it is still the day before."""
    return ((now or datetime.now()) - timedelta(hours=DAY_START_HOUR)).date()


def day_sql(column: str) -> str:
    """SQL for the training day of a timestamp column (stored in UTC by CURRENT_TIMESTAMP)."""
    return f"date({column}, 'localtime', '-{DAY_START_HOUR} hours')"


def ensure_cards(conn: sqlite3.Connection, cfg: Config) -> int:
    """Create a card for every one of my mistakes that doesn't have one. Returns new cards."""
    with conn:
        cur = conn.execute(f"""
            INSERT OR IGNORE INTO cards (kind, game_id, ply)
            SELECT 'mistake', m.game_id, m.ply FROM mistakes m
            WHERE m.is_mine = 1 AND m.judgement IN ({judgements_sql(cfg)})
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
    return 0, (on + timedelta(days=1)).isoformat(), None  # failed / skipped


def next_state(card: sqlite3.Row, result: str, on: date) -> tuple[int, str | None, str | None] | None:
    """The card's next (box, due_on, mastered_on) after an attempt, or None when the attempt
    doesn't change its schedule: it was already practised today (the first attempt of the day
    decides), or it isn't due yet and wasn't failed (early practice never promotes)."""
    day = on.isoformat()
    if card["last_on"] == day:
        return None
    not_due = card["mastered_on"] is not None or (card["due_on"] is not None and card["due_on"] > day)
    if not_due and result != "failed":
        return None
    return schedule(card["box"], result, on)


def _apply(conn: sqlite3.Connection, card: sqlite3.Row, result: str, on: date) -> None:
    """Store an attempt on a card: its schedule (see next_state) and the bookkeeping."""
    day = on.isoformat()
    state = next_state(card, result, on)
    box, due_on, mastered_on = state or (card["box"], card["due_on"], card["mastered_on"])
    lapse = int(state is not None and result not in SOLVED)
    with conn:
        conn.execute(
            """UPDATE cards SET box = ?, due_on = ?, mastered_on = ?, last_result = ?, reps = reps + 1,
                   lapses = lapses + ?, introduced_on = COALESCE(introduced_on, ?), last_on = ? WHERE id = ?""",
            (box, due_on, mastered_on, result, lapse, day, day, card["id"]),
        )


def record(
    conn: sqlite3.Connection, game_id: int, ply: int, result: str, on: date | None = None, kind: str = "mistake"
) -> None:
    """Update the card of a position after an attempt, creating it if needed. A calm position
    (kind 'position') only becomes a card once I fail it."""
    on = on or today()
    card = conn.execute("SELECT * FROM cards WHERE game_id = ? AND ply = ?", (game_id, ply)).fetchone()
    if card is None:
        if kind == "position" and result in SOLVED:
            return
        with conn:
            # OR IGNORE: two results for the same new position at once (two tabs) both get here.
            conn.execute("INSERT OR IGNORE INTO cards (kind, game_id, ply) VALUES (?, ?, ?)", (kind, game_id, ply))
        card = conn.execute("SELECT * FROM cards WHERE game_id = ? AND ply = ?", (game_id, ply)).fetchone()
    _apply(conn, card, result, on)


# SQL for games whose "new games" session isn't done (their mistakes wait for it; session.expire
# closes sessions after coach.session_days).
OPEN_SESSION_GAMES = """SELECT g.id FROM games g JOIN imports i ON i.id = g.batch_id WHERE i.session_done_at IS NULL"""


def queue(conn: sqlite3.Connection, cfg: Config, on: date | None = None, new_limit: int | None = None) -> dict:
    """Today's training: due cards ("due", see due_cards) and new cards ("new", best lessons
    first): at most `new_limit`, or what is left of training.new_per_day today."""
    on = on or today()
    if new_limit is None:
        new_limit = cfg.training.new_per_day - introduced_on(conn, on)
    return {"due": due_cards(conn, cfg, on), "new": new_cards(conn, cfg, new_limit)}


def judgements_sql(cfg: Config) -> str:
    """The judgements that become training cards, as an SQL list: "'mistake', 'blunder'"."""
    return "'mistake', 'blunder'" + (", 'inaccuracy'" if cfg.training.include_inaccuracies else "")


def due_cards(conn: sqlite3.Connection, cfg: Config, on: date) -> list[tuple]:
    """Due cards, most overdue first: (game_id, ply) of my positions, ("lib", puzzle_id) of
    library puzzles. Mistake cards only while the move is still a mistake under the current
    thresholds (they can change); calm-position cards always; library cards while the puzzle
    is still in the library."""
    ensure_cards(conn, cfg)
    day = on.isoformat()
    mine = conn.execute(
        f"""
        SELECT c.id, c.game_id, c.ply, c.box, c.due_on, {PUZZLE_VALUE} AS value
        FROM cards c JOIN mistakes m ON m.game_id = c.game_id AND m.ply = c.ply
        WHERE c.mastered_on IS NULL AND c.due_on IS NOT NULL AND c.due_on <= ?
          AND ((c.kind = 'mistake' AND m.judgement IN ({judgements_sql(cfg)})) OR c.kind = 'position')""",
        (day,),
    ).fetchall()
    library = conn.execute(
        """
        SELECT id, 'lib' AS game_id, key AS ply, box, due_on, 0 AS value FROM cards
        WHERE kind = 'library' AND mastered_on IS NULL AND due_on IS NOT NULL AND due_on <= ?
          AND key IN (SELECT id FROM puzzles)""",
        (day,),
    ).fetchall()
    return [(r["game_id"], r["ply"]) for r in _review_order(mine + library, on)]


def new_cards(conn: sqlite3.Connection, cfg: Config, limit: int, exclude: set | frozenset = frozenset()) -> list[tuple]:
    """Up to `limit` mistakes never studied, best lessons first (not from an open session)."""
    if limit <= 0:
        return []
    ensure_cards(conn, cfg)
    rows = conn.execute(
        f"""
        SELECT c.game_id, c.ply FROM cards c JOIN mistakes m ON m.game_id = c.game_id AND m.ply = c.ply
        WHERE c.kind = 'mistake' AND c.due_on IS NULL AND c.mastered_on IS NULL
          AND m.judgement IN ({judgements_sql(cfg)}) AND m.decided = 0
          AND c.game_id NOT IN ({OPEN_SESSION_GAMES})
        ORDER BY {PUZZLE_VALUE} DESC LIMIT ?""",
        (limit + len(exclude),),
    ).fetchall()
    return [k for k in ((r["game_id"], r["ply"]) for r in rows) if k not in exclude][:limit]


def _review_order(rows: list[sqlite3.Row], on: date) -> list[sqlite3.Row]:
    """Due cards by value: the most overdue relative to their interval first (a box-1 card a
    day late before a box-5 card three days late, and nothing waits for ever), then the
    bigger lesson, then the oldest."""

    def key(r: sqlite3.Row) -> tuple:
        late = (on - date.fromisoformat(r["due_on"])).days + 1
        return -late / INTERVALS[max(r["box"], 1) - 1], -(r["value"] or 0), r["due_on"], r["id"]

    return sorted(rows, key=key)


def introduced_on(conn: sqlite3.Connection, on: date) -> int:
    """Positions from my games (mistakes and failed calm positions) studied for the first time on
    day `on`: what training.new_per_day limits. Failed library puzzles, drilled opening lines and
    endgames don't use it up."""
    return conn.execute(
        "SELECT COUNT(*) FROM cards WHERE introduced_on = ? AND kind IN ('mistake', 'position')", (on.isoformat(),)
    ).fetchone()[0]


def stats(conn: sqlite3.Connection, cfg: Config, on: date | None = None) -> dict:
    """The Train page's counts: due and new cards, attempts today, mastered, learning, unseen and the streak."""
    on = on or today()
    q = queue(conn, cfg, on)
    row = conn.execute("""
        SELECT SUM(mastered_on IS NOT NULL) AS mastered,
               SUM(mastered_on IS NULL AND due_on IS NOT NULL) AS learning,
               SUM(due_on IS NULL AND mastered_on IS NULL) AS unseen
        FROM cards WHERE kind IN ('mistake', 'position')""").fetchone()
    done_today = conn.execute(
        f"SELECT COUNT(*) FROM review_results WHERE {day_sql('at')} = ?", (on.isoformat(),)
    ).fetchone()[0]
    return {
        "due": len(q["due"]),
        "new": len(q["new"]),
        "done_today": done_today,
        "mastered": row["mastered"] or 0,
        "learning": row["learning"] or 0,
        "unseen": row["unseen"] or 0,
        "streak": streak(conn, on),
    }


def calm_positions(
    conn: sqlite3.Connection, cfg: Config, count: int, seed: str, exclude: set | frozenset = frozenset()
) -> list[tuple[int, int]]:
    """`count` positions from my recent games where I played a good move: half where the
    opponent had a real threat (I defended it), half quiet. Deterministic per `seed` (the day).
    Games of an open new-games session count too: only their mistakes wait for the session."""
    if count <= 0:
        return []
    th = current_thresholds(conn, cfg)
    rows = conn.execute(
        """
        SELECT m.game_id, m.ply, g.my_color, p.fen, p.score_cp, p.score_mate, p.threat_cp, p.threat_mate,
               p.threat_uci, p.threat_pv, p0.fen AS prev_fen, p0.move_uci AS prev_move_uci
        FROM mistakes m JOIN positions p ON p.game_id = m.game_id AND p.ply = m.ply
        LEFT JOIN positions p0 ON p0.game_id = m.game_id AND p0.ply = m.ply - 1
        JOIN games g ON g.id = m.game_id
        WHERE m.is_mine = 1 AND m.judgement = 'good' AND m.decided = 0
          AND COALESCE(m.mark, '') NOT IN ('book', 'forced')
          AND m.game_id IN (SELECT id FROM games WHERE analysed_at IS NOT NULL
                            ORDER BY end_time DESC, id DESC LIMIT ?)
          AND NOT EXISTS (SELECT 1 FROM cards c WHERE c.game_id = m.game_id AND c.ply = m.ply)
        ORDER BY m.game_id, m.ply""",
        (cfg.coach.recent_games,),
    ).fetchall()
    threatened: list[tuple[int, int]] = []  # (game, ply)
    quiet: list[tuple[int, int]] = []
    for r in rows:
        if (r["game_id"], r["ply"]) in exclude:
            continue
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


def spread(cards: list, calm: list) -> list:
    """Interleave calm positions evenly among the cards (card i sits at i, calm j in between)."""
    slots = [(float(i), c) for i, c in enumerate(cards)]
    slots += [((j + 1) * len(cards) / (len(calm) + 1) - 0.5, c) for j, c in enumerate(calm)]
    return [c for _, c in sorted(slots, key=lambda s: s[0])]


# Everything that counts as training, with its timestamp column.
TRAINING_SOURCES = ("review_results", "puzzle_attempts", "self_reviews", "training_log", "spar_games", "calc_attempts")


def training_days(conn: sqlite3.Connection) -> set[date]:
    """Training days with any training: positions (Today, Practice, sessions), library puzzles,
    self-reviews, opening drills and Convert/endgames (training_log), sparring games (E5),
    calculation questions (E6)."""
    union = " UNION ".join(f"SELECT DISTINCT {day_sql('at')} FROM {t}" for t in TRAINING_SOURCES)
    return {date.fromisoformat(r[0]) for r in conn.execute(union) if r[0]}


def streak(conn: sqlite3.Connection, on: date | None = None) -> int:
    """Consecutive training days with any training, ending today (or yesterday)."""
    on = on or today()
    days = training_days(conn)
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
    """Update (or create) a keyed card: 'line', 'convert', 'endgame' or 'library'. Lines,
    Convert and endgames are also logged in training_log (they have no attempts table)."""
    on = on or today()
    with conn:
        conn.execute("INSERT OR IGNORE INTO cards (kind, key) VALUES (?, ?)", (kind, key))
        if kind != "library":
            conn.execute("INSERT INTO training_log (kind, key, result) VALUES (?, ?, ?)", (kind, key, result))
    card = conn.execute("SELECT * FROM cards WHERE kind = ? AND key = ?", (kind, key)).fetchone()
    _apply(conn, card, result, on)


def due_lines(conn: sqlite3.Connection, color: str, on: date | None = None) -> list[str]:
    """Opening-line cards of one colour due by `on`, most overdue first."""
    on = on or today()
    return [
        r[0]
        for r in conn.execute(
            """SELECT key FROM cards WHERE kind = 'line' AND key LIKE ? AND mastered_on IS NULL
           AND due_on IS NOT NULL AND due_on <= ? ORDER BY due_on, box""",
            (f"{color}:%", on.isoformat()),
        )
    ]


def line_stats(conn: sqlite3.Connection, color: str, on: date | None = None) -> dict:
    """Opening-line card counts of one colour: seen, mastered and due."""
    on = on or today()
    row = conn.execute(
        """SELECT COUNT(*) AS seen, SUM(mastered_on IS NOT NULL) AS mastered,
                  SUM(mastered_on IS NULL AND due_on <= ?) AS due
           FROM cards WHERE kind = 'line' AND key LIKE ?""",
        (on.isoformat(), f"{color}:%"),
    ).fetchone()
    return {"seen": row["seen"] or 0, "mastered": row["mastered"] or 0, "due": row["due"] or 0}
