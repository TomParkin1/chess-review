"""My level *now*, so training scales with me instead of with the rating I had in an old game.

Training attempts, threat checks and "clearly winning" are judged at my current rating
through the same rating bands as classification (classify.thresholds_for), which run
from under 800 to 2000+. Nothing here is tuned to one rating.
"""

import sqlite3

from .classify import Eval, Thresholds, adaptive_enabled, thresholds_for
from .config import Config


def current_rating(conn: sqlite3.Connection, time_class: str | None = None) -> int | None:
    """My rating in my latest rated game, in `time_class` or else the time class I play most."""
    if time_class is None:
        row = conn.execute("""SELECT time_class FROM games WHERE time_class IS NOT NULL
                              GROUP BY time_class ORDER BY COUNT(*) DESC LIMIT 1""").fetchone()
        time_class = row[0] if row else None
    row = conn.execute("""
        SELECT CASE WHEN my_color = 'white' THEN white_elo ELSE black_elo END AS elo FROM games
        WHERE (time_class = ? OR ? IS NULL) AND COALESCE(rated, 1) = 1
          AND (CASE WHEN my_color = 'white' THEN white_elo ELSE black_elo END) IS NOT NULL
        ORDER BY end_time DESC, id DESC LIMIT 1""", (time_class, time_class)).fetchone()
    return row["elo"] if row else None


def current_thresholds(conn: sqlite3.Connection, cfg: Config) -> Thresholds:
    """Judgement thresholds for my current rating (or the standard ones in standard mode)."""
    return thresholds_for(current_rating(conn), cfg, adaptive_enabled(conn, cfg))


# "Clearly winning" by rating: stronger players are expected to convert smaller edges.
# (rating below, centipawns); used for conversion stats (progress.py) and Convert (later).
WINNING_BANDS = [(1200, 500), (1800, 400), (None, 300)]


def winning_cp(rating: int | None) -> int:
    for below, cp in WINNING_BANDS:
        if below is None or (rating or 0) < below:
            return cp
    raise AssertionError("unreachable")


def winning_chance(rating: int | None) -> float:
    """Win chance (Lichess -1..1 scale) from which I count as clearly winning."""
    return Eval(winning_cp(rating), None).win_chance()
