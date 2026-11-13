"""A simulated run of daily training, to check that Today's load stays inside the time budget.

`run()` plays day after day on a database (always a copy: `simulate_copy`): it builds Today's
plan with the real planner (coach.today_plan) and answers every item at my own rates from my
training history (`rates`), the way the app would record them: review results, library attempts,
the endgame played on the Convert page (an essential endgame or a won position), plus the "Again"
retry of a failed puzzle at the end of its block. Some days are skipped, as in real life. Each day
reports what was due, what the plan
held, how long it took at my pace and how many due reviews had to wait (the backlog).

    python -m chessreview simulate --days 90 --budget 20
"""

import random
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from . import coach, db, srs
from .config import Config

# (clean solve, solved with help) per kind of attempt when my history is too thin to tell; the
# rest are failures. Calm positions and puzzles have no "with help" in the app's sense here.
DEFAULT_RATES = {
    "first": (0.45, 0.35),
    "review": (0.65, 0.30),
    "calm": (0.70, 0.0),
    "library": (0.55, 0.15),
    "convert": (0.50, 0.10),
}
MIN_ATTEMPTS = 20  # attempts of a kind needed before my own rate replaces the default


@dataclass
class Day:
    """One simulated day of Today: what was due, planned, done and left waiting."""

    day: date
    skipped: bool = False
    due: int = 0  # due reviews at the start of the day (all of them)
    reviews: int = 0  # of those, in the plan
    new: int = 0  # cards studied for the first time
    items: int = 0  # puzzles in the plan
    again: int = 0  # failed puzzles tried once more at the end of their block
    convert: str = ""  # the endgame played: "endgame" | "convert" | ""
    endgame_share: float = 0.0  # planned endgame time (the endgame played + endgame puzzles) / budget
    endgame_library: int = 0  # Lichess endgame puzzles in the plan
    planned_min: float = 0.0  # the plan's own estimate (with my usual retries)
    spent_min: float = 0.0  # what the day took: every attempt and the endgame played
    learning: int = 0  # cards in the review cycle after the day
    mastered: int = 0

    @property
    def backlog(self) -> int:
        return self.due - self.reviews


@dataclass
class Result:
    """A simulation run: its settings and every day."""

    budget: int
    pace: float
    rates: dict
    days: list[Day] = field(default_factory=list)

    def summary(self) -> dict:
        played = [d for d in self.days if not d.skipped]
        return {
            "days": len(self.days),
            "played": len(played),
            "budget": self.budget,
            "max_planned_min": max((d.planned_min for d in played), default=0),
            "max_spent_min": max((d.spent_min for d in played), default=0),
            "mean_spent_min": sum(d.spent_min for d in played) / len(played) if played else 0,
            "over_budget_days": sum(d.planned_min > self.budget + 0.01 for d in played),
            "backlog_max": max((d.backlog for d in self.days), default=0),
            "endgame_share": sum(d.endgame_share for d in played) / len(played) if played else 0,
            "endgame_library_days": sum(d.endgame_library > 0 for d in played),
            "backlog_end": self.days[-1].backlog if self.days else 0,
            "new_cards": sum(d.new for d in self.days),
            "learning_end": self.days[-1].learning if self.days else 0,
            "mastered_end": self.days[-1].mastered if self.days else 0,
        }


def rates(conn: sqlite3.Connection) -> dict:
    """My (clean, with help) rates per kind of attempt, from my history where there is enough."""
    out = dict(DEFAULT_RATES)
    rows = conn.execute("""
        SELECT r.result, m.judgement,
               r.id = (SELECT MIN(r2.id) FROM review_results r2 WHERE r2.game_id = r.game_id AND r2.ply = r.ply)
                   AS first
        FROM review_results r JOIN mistakes m ON m.game_id = r.game_id AND m.ply = r.ply""").fetchall()
    groups = {
        "first": [r["result"] for r in rows if r["judgement"] != "good" and r["first"]],
        "review": [r["result"] for r in rows if r["judgement"] != "good" and not r["first"]],
        "calm": [r["result"] for r in rows if r["judgement"] == "good"],
        "library": [r[0] for r in conn.execute("SELECT result FROM puzzle_attempts")],
        "convert": [r[0] for r in conn.execute("SELECT result FROM training_log WHERE kind IN ('convert', 'endgame')")],
    }
    for kind, results in groups.items():
        if len(results) >= MIN_ATTEMPTS:
            out[kind] = (results.count("solved") / len(results), results.count("solved_with_help") / len(results))
    return out


def run(
    conn: sqlite3.Connection,
    cfg: Config,
    days: int = 90,
    budget: int = 20,
    skip: float = 0.15,
    seed: int = 1,
    start: date | None = None,
    my_rates: dict | None = None,
    progress: Callable[[Day], None] | None = None,
) -> Result:
    """Simulate `days` days of Today on `conn` (it is changed: pass a copy). Returns every day."""
    rng = random.Random(seed)
    my_rates = my_rates or rates(conn)
    with conn:
        db.set_meta(conn, coach.BUDGET_KEY, str(budget))
    start = start or srs.today() + timedelta(days=1)
    result = Result(budget, coach.pace(conn, cfg), my_rates)
    gap = coach.attempt_seconds(conn, cfg)  # the clock of the simulated attempts
    for n in range(days):
        on = start + timedelta(days=n)
        due = len(srs.due_cards(conn, cfg, on))
        if rng.random() < skip:
            result.days.append(Day(on, skipped=True, due=due, **_counts(conn)))
            continue
        plan = coach.today_plan(conn, cfg, on)
        stats = Day(on, due=due, planned_min=_planned_minutes(plan), **_endgame_stats(plan))
        clock = _Clock(on, gap)
        for block in plan["blocks"]:
            if block["id"] == "convert":
                kind, _, key = block["key"].partition("/")
                srs.record_key(conn, kind, key, _answer(rng, my_rates["convert"]), on)
                stats.convert = kind
            else:
                again = []
                for g, p, tag in block["items"]:
                    stats.items += 1
                    stats.reviews += tag == "due"
                    if _attempt(conn, rng, my_rates, g, p, tag, on, clock) not in srs.SOLVED:
                        again.append((g, p, tag))
                for g, p, tag in again:  # Today shows a failed puzzle once more at the end of its block
                    _attempt(conn, rng, my_rates, g, p, tag, on, clock)
                    stats.again += 1
        stats.new = srs.introduced_on(conn, on)
        stats.spent_min = _spent_minutes(plan, stats, gap)
        for k, v in _counts(conn).items():
            setattr(stats, k, v)
        result.days.append(stats)
        if progress:
            progress(stats)
    return result


def simulate_copy(conn: sqlite3.Connection, cfg: Config, **kwargs: Any) -> Result:
    """`run` on a temporary copy of the database (made with SQLite's backup API); `conn` is
    only read."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "simulation.db"
        with sqlite3.connect(path) as dst:
            conn.backup(dst)
        dst.close()
        copy = db.connect(path)
        try:
            return run(copy, cfg, **kwargs)
        finally:
            copy.close()


class _Clock:
    """Timestamps for the attempts of a simulated day: from noon, one per `pace` seconds (stored
    in UTC like CURRENT_TIMESTAMP, so they fall on the right training day)."""

    def __init__(self, on: date, pace: float):
        self.at = datetime.combine(on, time(12)).astimezone(UTC)
        self.pace = pace

    def tick(self) -> str:
        self.at += timedelta(seconds=self.pace)
        return self.at.strftime("%Y-%m-%d %H:%M:%S")


def _answer(rng: random.Random, rate: tuple[float, float]) -> str:
    x = rng.random()
    return "solved" if x < rate[0] else "solved_with_help" if x < rate[0] + rate[1] else "failed"


def _attempt(
    conn: sqlite3.Connection, rng: random.Random, my_rates: dict, g: Any, p: Any, tag: str, on: date, clock: _Clock
) -> str:
    """One attempt at a plan item, stored as the app stores it."""
    if g == "lib":
        result = _answer(rng, my_rates["library"])
        with conn:
            conn.execute(
                "INSERT INTO puzzle_attempts (puzzle_id, result, at) VALUES (?, ?, ?)", (p, result, clock.tick())
            )
        has_card = conn.execute("SELECT 1 FROM cards WHERE kind = 'library' AND key = ?", (p,)).fetchone()
        if has_card or result not in srs.SOLVED:  # as library.record
            srs.record_key(conn, "library", p, result, on)
        return result
    card = conn.execute("SELECT due_on, mastered_on FROM cards WHERE game_id = ? AND ply = ?", (g, p)).fetchone()
    studied = card is not None and (card["due_on"] or card["mastered_on"])
    kind = "calm" if tag == "calm" else "review" if studied else "first"
    result = _answer(rng, my_rates[kind])
    with conn:
        conn.execute(
            "INSERT INTO review_results (game_id, ply, result, at) VALUES (?, ?, ?, ?)", (g, p, result, clock.tick())
        )
    srs.record(conn, g, p, result, on, kind="position" if tag == "calm" else "mistake")
    return result


def _planned_minutes(plan: dict) -> float:
    seconds = sum(len(b["items"]) for b in plan["blocks"]) * plan["pace"]
    for b in plan["blocks"]:
        if b["id"] == "convert":
            seconds += coach.drill_seconds(b["key"])
    return seconds / 60


def _endgame_stats(plan: dict) -> dict:
    items = [i for b in plan["blocks"] if b["id"] == "endgame" for i in b["items"]]
    seconds = len(items) * plan["pace"] + sum(
        coach.drill_seconds(b["key"]) for b in plan["blocks"] if b["id"] == "convert"
    )
    return {"endgame_share": seconds / (plan["budget"] * 60), "endgame_library": sum(i[0] == "lib" for i in items)}


def _spent_minutes(plan: dict, stats: Day, gap: float) -> float:
    """What the day really took: every attempt at the time per attempt, plus the endgame played."""
    seconds = (stats.items + stats.again) * gap
    return (seconds + (coach.drill_seconds(f"{stats.convert}/") if stats.convert else 0)) / 60


def _counts(conn: sqlite3.Connection) -> dict:
    row = conn.execute("""SELECT SUM(mastered_on IS NULL AND due_on IS NOT NULL) AS learning,
                                 SUM(mastered_on IS NOT NULL) AS mastered FROM cards""").fetchone()
    return {"learning": row["learning"] or 0, "mastered": row["mastered"] or 0}


def table(result: Result) -> str:
    """The days as a text table, and the summary."""
    lines = [
        f"{'day':<11} {'due':>4} {'rev':>4} {'wait':>5} {'new':>4} {'items':>5} {'again':>5} "
        f"{'endgame':>8} {'plan min':>8} {'took min':>8} {'learning':>8}"
    ]
    for d in result.days:
        if d.skipped:
            lines.append(f"{d.day.isoformat():<11} {d.due:>4}  (day off)")
            continue
        lines.append(
            f"{d.day.isoformat():<11} {d.due:>4} {d.reviews:>4} {d.backlog:>5} {d.new:>4} {d.items:>5} "
            f"{d.again:>5} {d.convert:>8} {d.planned_min:>8.1f} "
            f"{d.spent_min:>8.1f} {d.learning:>8}"
        )
    s = result.summary()
    lines.append("")
    lines.append(
        f"Budget {s['budget']} min, pace {result.pace:.0f} s a puzzle (retries included), "
        f"{s['played']} of {s['days']} days trained."
    )
    lines.append(
        f"Planned time: at most {s['max_planned_min']:.1f} min ({s['over_budget_days']} days over budget). "
        f"Time taken, every retry included: on average {s['mean_spent_min']:.1f} min, "
        f"at most {s['max_spent_min']:.1f} min."
    )
    lines.append(f"Reviews waiting (backlog): at most {s['backlog_max']}, {s['backlog_end']} on the last day.")
    lines.append(
        f"Endgames: {s['endgame_share']:.0%} of the planned time on average; Lichess endgame puzzles on "
        f"{s['endgame_library_days']} of {s['played']} days."
    )
    lines.append(
        f"New cards: {s['new_cards']} in {s['days']} days; {s['learning_end']} cards in the review cycle "
        f"at the end, {s['mastered_end']} mastered."
    )
    lines.append(
        "Rates used (clean / with help): " + ", ".join(f"{k} {v[0]:.0%}/{v[1]:.0%}" for k, v in result.rates.items())
    )
    return "\n".join(lines)
