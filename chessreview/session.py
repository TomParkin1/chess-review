"""The "new games" session: after a sync/upload, review just those games' mistakes.

All import batches whose session isn't done yet form one session. It starts with a
self-review: for the games that went wrong (lost, drawn, or with a blunder of mine; at most
coach.self_review_games, losses first) I pick the move where I think the game turned before
seeing any engine verdict. The turning point is my move with the biggest win-chance drop (so
what counts scales with my level through the judgement thresholds); a pick within
SELF_REVIEW_TOLERANCE plies counts. Then come the puzzles (my mistakes and blunders, in game
order); when finished the batches are marked done and the summary shows what they taught me.
"""

import sqlite3
from collections import Counter

from . import report
from .config import Config

CLOCK_BUCKETS = [(180_000, "over 3 min"), (60_000, "1–3 min"), (20_000, "20–60 s"), (0, "under 20 s")]
SELF_REVIEW_TOLERANCE = 2      # plies: my move right before or after the turning point also counts


def open_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""SELECT * FROM imports WHERE session_done_at IS NULL AND games_added > 0
                           ORDER BY id""").fetchall()


def status(conn: sqlite3.Connection, cfg: Config) -> dict:
    batches = open_batches(conn)
    ids = [b["id"] for b in batches]
    if not ids:
        return {"open": False, "batch_ids": []}
    marks = ",".join("?" * len(ids))
    games = conn.execute(f"""SELECT COUNT(*) AS n, SUM(analysed_at IS NOT NULL) AS analysed
                             FROM games WHERE batch_id IN ({marks})""", ids).fetchone()
    puzzles = conn.execute(f"""
        SELECT COUNT(*) FROM mistakes m JOIN games g ON g.id = m.game_id
        WHERE g.batch_id IN ({marks}) AND m.is_mine = 1 AND m.judgement IN ({_judgements(cfg)})""",
        ids).fetchone()[0]
    return {"open": True, "batch_ids": ids, "games": games["n"], "analysed": games["analysed"] or 0,
            "puzzles": puzzles}


def puzzle_where(cfg: Config, batch_ids: list[int]) -> tuple[str, list]:
    """WHERE clause (for the mistakes query in web.py) selecting this session's puzzles."""
    marks = ",".join("?" * len(batch_ids))
    return (f"g.batch_id IN ({marks}) AND m.judgement IN ({_judgements(cfg)})", list(batch_ids))


def turning_point(conn: sqlite3.Connection, game_id: int) -> sqlite3.Row | None:
    """My worst move of a game (largest win-chance drop), if it was at least a mistake."""
    return conn.execute("""
        SELECT m.*, p.move_san, p.move_uci, p.best_uci, p.fen FROM mistakes m
        JOIN positions p ON p.game_id = m.game_id AND p.ply = m.ply
        WHERE m.game_id = ? AND m.is_mine = 1 AND m.judgement IN ('mistake', 'blunder')
        ORDER BY m.win_drop DESC LIMIT 1""", (game_id,)).fetchone()


def self_review_games(conn: sqlite3.Connection, cfg: Config, batch_ids: list[int]) -> list[dict]:
    """Games of this session to self-review, with their moves (no evals, no marks)."""
    if not batch_ids:
        return []
    marks = ",".join("?" * len(batch_ids))
    games = conn.execute(f"""
        SELECT g.* FROM games g WHERE g.batch_id IN ({marks}) AND g.analysed_at IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM self_reviews s WHERE s.game_id = g.id)
          AND (g.my_result != 'win' OR EXISTS (SELECT 1 FROM mistakes m WHERE m.game_id = g.id
               AND m.is_mine = 1 AND m.judgement = 'blunder'))
        ORDER BY CASE g.my_result WHEN 'loss' THEN 0 WHEN 'draw' THEN 1 ELSE 2 END, g.end_time""",
        batch_ids).fetchall()
    out = []
    for g in games:
        if turning_point(conn, g["id"]) is None:
            continue
        positions = conn.execute("SELECT ply, fen, move_uci, move_san FROM positions WHERE game_id = ? ORDER BY ply",
                                 (g["id"],)).fetchall()
        out.append({
            "game_id": g["id"], "my_color": g["my_color"], "result": g["my_result"],
            "opponent": g["black"] if g["my_color"] == "white" else g["white"], "date": g["date"],
            "opening": g["opening"],
            "moves": [{"ply": p["ply"], "san": p["move_san"], "uci": p["move_uci"], "fen": p["fen"]}
                      for p in positions if p["move_uci"]],
            "final_fen": positions[-1]["fen"] if positions else None,
        })
        if len(out) >= cfg.coach.self_review_games:
            break
    return out


def record_self_review(conn: sqlite3.Connection, game_id: int, picked_ply: int | None) -> dict:
    """Compare my pick with the real turning point and store it."""
    tp = turning_point(conn, game_id)
    if tp is None:
        raise LookupError(game_id)
    hit = picked_ply is not None and abs(picked_ply - tp["ply"]) <= SELF_REVIEW_TOLERANCE
    with conn:
        conn.execute("INSERT INTO self_reviews (game_id, picked_ply, actual_ply, hit) VALUES (?, ?, ?, ?)",
                     (game_id, picked_ply, tp["ply"], int(hit)))
    return {"hit": hit, "exact": picked_ply == tp["ply"], "ply": tp["ply"], "judgement": tp["judgement"],
            "move_san": tp["move_san"], "move_uci": tp["move_uci"], "best_uci": tp["best_uci"], "fen": tp["fen"],
            "explanation": tp["explanation"], "kind": tp["kind"], "win_drop": tp["win_drop"]}


def self_review_rate(conn: sqlite3.Connection, game_ids: list[int] | None = None) -> dict:
    """How often I find the turning point myself (for these games, or my last 20 tries)."""
    if game_ids is not None:
        marks = ",".join("?" * len(game_ids)) or "NULL"
        rows = conn.execute(f"SELECT hit FROM self_reviews WHERE game_id IN ({marks})", game_ids).fetchall()
    else:
        rows = conn.execute("SELECT hit FROM self_reviews ORDER BY id DESC LIMIT 20").fetchall()
    tries, hits = len(rows), sum(r["hit"] for r in rows)
    return {"tries": tries, "hits": hits, "rate": 100 * hits / tries if tries else None}


def mark_done(conn: sqlite3.Connection, batch_ids: list[int]) -> None:
    with conn:
        conn.executemany("UPDATE imports SET session_done_at = CURRENT_TIMESTAMP WHERE id = ?",
                         [(i,) for i in batch_ids])


def summary(conn: sqlite3.Connection, cfg: Config, batch_ids: list[int]) -> dict:
    """What the batch shows: results, accuracy, recurring mistake types, clock trouble."""
    if not batch_ids:
        return {}
    marks = ",".join("?" * len(batch_ids))
    games = conn.execute(f"SELECT * FROM games WHERE batch_id IN ({marks}) ORDER BY end_time", batch_ids).fetchall()
    mine = conn.execute(f"""
        SELECT m.*, g.clocks_ms, g.time_control, g.my_color FROM mistakes m JOIN games g ON g.id = m.game_id
        WHERE g.batch_id IN ({marks}) AND m.is_mine = 1""", batch_ids).fetchall()

    accuracies = [g["accuracy_white"] if g["my_color"] == "white" else g["accuracy_black"] for g in games]
    accuracies = [a for a in accuracies if a is not None]
    bad = [m for m in mine if m["judgement"] in ("mistake", "blunder")]
    kinds = Counter(m["kind"] for m in bad if m["kind"])
    phases = Counter(m["phase"] for m in bad)

    # Clock left when I made each blunder (from the API's clock times).
    clock_buckets: Counter = Counter()
    for m in mine:
        if m["judgement"] != "blunder" or not m["clocks_ms"]:
            continue
        left = _clock_before(m["clocks_ms"], m["time_control"], m["ply"])
        if left is not None:
            clock_buckets[next(label for limit, label in CLOCK_BUCKETS if left >= limit)] += 1

    worst = None
    scored = [(a, g) for g, a in zip(games, [g["accuracy_white"] if g["my_color"] == "white"
                                              else g["accuracy_black"] for g in games]) if a is not None]
    if scored:
        acc, g = min(scored, key=lambda t: t[0])
        worst = {"id": g["id"], "opponent": g["black"] if g["my_color"] == "white" else g["white"],
                 "accuracy": acc, "result": g["my_result"]}

    return {
        "games": len(games),
        "wins": sum(g["my_result"] == "win" for g in games),
        "draws": sum(g["my_result"] == "draw" for g in games),
        "losses": sum(g["my_result"] == "loss" for g in games),
        "accuracy": round(sum(accuracies) / len(accuracies), 1) if accuracies else None,
        "mistakes": sum(m["judgement"] == "mistake" for m in mine),
        "blunders": sum(m["judgement"] == "blunder" for m in mine),
        "top_kinds": kinds.most_common(3),
        "phases": dict(phases),
        "blunder_clock": [(label, clock_buckets.get(label, 0)) for _, label in CLOCK_BUCKETS],
        "worst_game": worst,
        "self_review": self_review_rate(conn, [g["id"] for g in games]),
    }


def _clock_before(clocks_ms: str, time_control: str | None, ply: int) -> int | None:
    """My remaining clock (ms) just before the move at `ply`."""
    clocks = clocks_ms.split()
    if ply >= 2:
        value = clocks[ply - 2] if ply - 2 < len(clocks) else "-"
        return None if value == "-" else int(value)
    base, _ = report.parse_time_control(time_control)
    return base


def _judgements(cfg: Config) -> str:
    return "'mistake', 'blunder'" + (", 'inaccuracy'" if cfg.training.include_inaccuracies else "")
