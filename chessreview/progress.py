"""Is training working? Game metrics over my last N games compared with the N before.

Windows are counted in games (config coach.progress_window), not weeks, so the comparison
is fair however often I play. Every yardstick is my *current* level (threat threshold,
"clearly winning"), applied to all games alike, and compared with my own baseline, never
with fixed targets.

Metrics (per window):
  blunders per game, share of my blunders made right after an opponent error,
  threats missed per game (my mistake/blunder while a real threat was on the board),
  conversion (games where I was clearly winning that I won), accuracy, rating.
Plus mistake kinds per game (biggest gain / biggest slip) and weekly training activity.
"""

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

import chess

from . import level, session, srs
from .config import Config
from .insights import KIND_LABELS, my_accuracy, my_elo
from .threats import training_threat

MIN_GAMES = 10  # a window needs at least this many games to be shown


@dataclass
class GameStats:
    date: str
    result: str
    blunders: int = 0
    after_error: int = 0
    threats_missed: int = 0
    reached_win: bool = False
    accuracy: float | None = None
    rating: int | None = None
    kinds: Counter = field(default_factory=Counter)


METRICS = [
    # key, label, higher is better, format
    ("blunders", "Blunders per game", False, "1"),
    ("after_error", "Blunders right after their mistake", False, "%"),
    ("threats_missed", "Threats missed per game", False, "1"),
    ("conversion", "Won from winning positions", True, "%"),
    ("accuracy", "Accuracy", True, "0"),
    ("rating", "Rating", True, "0"),
]


def game_stats(conn: sqlite3.Connection, cfg: Config) -> list[GameStats]:
    """Per-game numbers for my analysed games, oldest first."""
    th = level.current_thresholds(conn, cfg)
    winning = level.winning_chance(level.current_rating(conn))
    games = conn.execute("""SELECT * FROM games WHERE analysed_at IS NOT NULL
                            ORDER BY end_time, id""").fetchall()
    stats = {
        g["id"]: GameStats(
            date=(g["end_time"] or g["date"] or "")[:10],
            result=g["my_result"],
            accuracy=my_accuracy(g),
            rating=my_elo(g),
        )
        for g in games
    }
    rows = conn.execute("""
        SELECT m.game_id, m.ply, m.judgement, m.kind, m.win_before, g.my_color, p.fen, p.score_cp, p.score_mate,
               p.threat_cp, p.threat_mate, p.threat_uci, p.threat_pv, p0.fen AS prev_fen,
               p0.move_uci AS prev_move_uci, m0.judgement AS prev_judgement
        FROM mistakes m JOIN games g ON g.id = m.game_id
        LEFT JOIN positions p ON p.game_id = m.game_id AND p.ply = m.ply
        LEFT JOIN positions p0 ON p0.game_id = m.game_id AND p0.ply = m.ply - 1
        LEFT JOIN mistakes m0 ON m0.game_id = m.game_id AND m0.ply = m.ply - 1
        WHERE m.is_mine = 1""").fetchall()
    for r in rows:
        s = stats.get(r["game_id"])
        if s is None:
            continue
        if r["win_before"] is not None and r["win_before"] >= winning:
            s.reached_win = True
        if r["judgement"] not in ("mistake", "blunder"):
            continue
        s.kinds[r["kind"]] += 1
        me = chess.WHITE if r["my_color"] == "white" else chess.BLACK
        threat = training_threat(r, me, th)
        if threat is not None and threat.real:
            s.threats_missed += 1
        if r["judgement"] == "blunder":
            s.blunders += 1
            if r["prev_judgement"] in ("mistake", "blunder"):
                s.after_error += 1
    return [stats[g["id"]] for g in games]


def window_metrics(games: list[GameStats]) -> dict:
    n = len(games)
    blunders = sum(g.blunders for g in games)
    reached = [g for g in games if g.reached_win]
    accs = [g.accuracy for g in games if g.accuracy is not None]
    ratings = [g.rating for g in games if g.rating]
    return {
        "games": n,
        "blunders": blunders / n if n else None,
        "after_error": 100 * sum(g.after_error for g in games) / blunders if blunders else None,
        "threats_missed": sum(g.threats_missed for g in games) / n if n else None,
        "conversion": 100 * sum(g.result == "win" for g in reached) / len(reached) if reached else None,
        "accuracy": sum(accs) / len(accs) if accs else None,
        "rating": ratings[-1] if ratings else None,
        "winning_games": len(reached),
    }


def report(conn: sqlite3.Connection, cfg: Config) -> dict:
    window = max(MIN_GAMES, cfg.coach.progress_window)
    games = game_stats(conn, cfg)
    current = games[-window:]
    baseline = games[-2 * window : -window] if len(games) > window else []
    cur, base = window_metrics(current), window_metrics(baseline) if len(baseline) >= MIN_GAMES else None

    headline = []
    for key, label, higher_better, fmt in METRICS:
        c, b = cur[key], (base or {}).get(key)
        better = None
        if c is not None and b is not None and abs(c - b) > 1e-9:
            better = (c > b) == higher_better
        headline.append(
            {
                "key": key,
                "label": label,
                "current": c,
                "baseline": b,
                "better": better,
                "format": fmt,
                "higher_better": higher_better,
            }
        )

    # Rolling series for the charts: each point is the window of games ending there.
    series = {key: [] for key, *_ in METRICS}
    labels = []
    for i in range(MIN_GAMES - 1, len(games)):
        m = window_metrics(games[max(0, i - window + 1) : i + 1])
        labels.append(games[i].date)
        for key in series:
            series[key].append(m[key])

    kinds = _kind_changes(current, baseline if base else [])
    return {
        "window": window,
        "games": len(games),
        "current": cur,
        "baseline": base,
        "headline": headline,
        "labels": labels,
        "series": series,
        "kinds": kinds,
        "rating_now": level.current_rating(conn),
        "winning_cp": level.winning_cp(level.current_rating(conn)),
        "training": training_weeks(conn),
        "self_review": session.self_review_rate(conn),
        "summary": summary_line(window, len(games), cur, base, kinds),
    }


def _kind_changes(current: list[GameStats], baseline: list[GameStats]) -> list[dict]:
    """Mistakes+blunders per game by kind, current window vs baseline (biggest change first)."""

    def rates(games: list[GameStats]) -> dict:
        total = Counter()
        for g in games:
            total.update(g.kinds)
        return {k: v / len(games) for k, v in total.items()} if games else {}

    cur, base = rates(current), rates(baseline)
    out = []
    for kind in set(cur) | set(base):
        if kind is None:
            continue
        c, b = cur.get(kind, 0.0), base.get(kind, 0.0) if baseline else None
        out.append(
            {
                "kind": kind,
                "label": KIND_LABELS.get(kind, kind.replace("_", " ").capitalize()),
                "current": c,
                "baseline": b,
                "change": None if b is None else c - b,
            }
        )
    out.sort(key=lambda k: -(k["current"] + (k["baseline"] or 0)))
    return out


def summary_line(window: int, total: int, cur: dict, base: dict | None, kinds: list[dict]) -> str:
    """One sentence for Home."""
    if not cur["games"]:
        return "Analyse some games to see your progress."
    if base is None:
        return (
            f"{cur['blunders']:.1f} blunders per game over your last {cur['games']} games. "
            f"Play {window + MIN_GAMES - total} more to see a trend."
        )
    text = (
        f"Last {window} games vs the {window} before: blunders per game {base['blunders']:.1f} → {cur['blunders']:.1f}."
    )
    changes = [k for k in kinds if k["change"] is not None and (k["baseline"] or 0) * window >= 3]
    if changes:
        best = min(changes, key=lambda k: k["change"])
        if best["change"] < -0.05:
            text += f" Biggest gain: {best['label'].lower()}."
    return text


def _iso_week(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def training_weeks(conn: sqlite3.Connection, weeks: int = 12) -> list[dict]:
    """Training per week (the last `weeks` weeks with any training): days trained (any kind of
    training, srs.training_days), attempts at my positions, their solve rate and threat hits."""
    # ISO weeks (Monday to Sunday), so the week around New Year isn't split in two.
    stats: dict[str, dict] = {}
    for r in conn.execute(f"""
            SELECT {srs.day_sql("at")} AS day, COUNT(*) AS attempts,
                   SUM(result IN ('solved', 'solved_with_help')) AS solved,
                   SUM(threat_result = 'correct') AS threat_ok,
                   SUM(threat_result IN ('correct', 'wrong')) AS threat_tries
            FROM review_results GROUP BY day"""):
        week = stats.setdefault(
            _iso_week(date.fromisoformat(r["day"])), {"attempts": 0, "solved": 0, "threat_ok": 0, "threat_tries": 0}
        )
        for k in week:
            week[k] += r[k] or 0
    days: dict[str, list] = {}
    for day in srs.training_days(conn):
        days.setdefault(_iso_week(day), []).append(day)
    out = []
    for week in sorted(days)[-weeks:]:
        r = stats.get(week)
        attempts = r["attempts"] if r else 0
        out.append(
            {
                "week": week,
                "from": min(days[week]).isoformat(),
                "attempts": attempts,
                "days": len(days[week]),
                "solve_rate": 100 * r["solved"] / attempts if attempts else None,
                "threat_rate": 100 * r["threat_ok"] / r["threat_tries"] if r and r["threat_tries"] else None,
            }
        )
    return out
