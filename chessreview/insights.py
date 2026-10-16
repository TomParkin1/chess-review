"""Aggregations for the Insights page and the Home dashboard.

Everything is computed from stored data (games, my moves in `mistakes`, clocks), for an
optional time class and "last N games" window. `weaknesses()` ranks candidate problems
(mistake types, game phases, openings, clock trouble, colour) with simple, explainable
severity scores and attaches a "Train this" filter for the Practice page.
"""

import sqlite3
from collections import Counter, defaultdict

from . import report

KIND_LABELS = {
    "missed_opportunity": "Letting advantages slip", "positional": "Positional slips",
    "loses_material": "Losing material", "hanging_piece": "Hanging pieces",
    "missed_tactic": "Missed tactics", "king_safety": "King safety", "missed_capture": "Missed captures",
    "allowed_mate": "Walking into mates", "missed_mate": "Missed checkmates", "fork": "Allowing forks",
    "discovered_attack": "Allowing discovered attacks",
}
CLOCK_BUCKETS = [(180_000, "3+ min"), (60_000, "1–3 min"), (20_000, "20–60 s"), (0, "<20 s")]
LOW_CLOCK_MS = 60_000


def _games(conn: sqlite3.Connection, time_class: str | None, last: int | None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM games WHERE analysed_at IS NOT NULL"
    params: list = []
    if time_class:
        sql += " AND time_class = ?"
        params.append(time_class)
    sql += " ORDER BY end_time DESC, id DESC"
    if last:
        sql += " LIMIT ?"
        params.append(int(last))
    return list(reversed(conn.execute(sql, params).fetchall()))   # oldest first


def my_accuracy(g: sqlite3.Row) -> float | None:
    return g["accuracy_white"] if g["my_color"] == "white" else g["accuracy_black"]


def my_elo(g: sqlite3.Row) -> int | None:
    return g["white_elo"] if g["my_color"] == "white" else g["black_elo"]


def score(results: list[str]) -> float | None:
    if not results:
        return None
    return 100 * sum({"win": 1, "draw": 0.5}.get(r, 0) for r in results) / len(results)


def clock_before(clocks: list[str], base_ms: int | None, ply: int) -> int | None:
    """My remaining clock (ms) before the move at `ply`, from per-ply clocks."""
    if ply >= 2:
        value = clocks[ply - 2] if ply - 2 < len(clocks) else "-"
        return None if value == "-" else int(value)
    return base_ms


def compute(conn: sqlite3.Connection, time_class: str | None = None, last: int | None = None) -> dict:
    games = _games(conn, time_class, last)
    ids = [g["id"] for g in games]
    if not ids:
        return {"games": 0}
    moves = conn.execute(f"""SELECT * FROM mistakes WHERE is_mine = 1 AND game_id IN ({','.join('?' * len(ids))})""",
                         ids).fetchall()
    by_game = defaultdict(list)
    for m in moves:
        by_game[m["game_id"]].append(m)

    # ---- trends (per game, oldest first)
    trend = [{"id": g["id"], "date": (g["end_time"] or g["date"] or "")[:10], "accuracy": my_accuracy(g),
              "elo": my_elo(g), "result": g["my_result"], "time_class": g["time_class"],
              "opponent": g["black"] if g["my_color"] == "white" else g["white"]} for g in games]

    # ---- overall
    accs = [t["accuracy"] for t in trend if t["accuracy"] is not None]
    bad = [m for m in moves if m["judgement"] in ("mistake", "blunder")]
    overall = {
        "games": len(games), "score": score([g["my_result"] for g in games]),
        "accuracy": sum(accs) / len(accs) if accs else None,
        "mistakes_per_game": sum(m["judgement"] == "mistake" for m in moves) / len(games),
        "blunders_per_game": sum(m["judgement"] == "blunder" for m in moves) / len(games),
        "move_accuracy": _mean(m["accuracy"] for m in moves),
    }

    # ---- phases: average accuracy of my moves and errors per game
    phases = []
    for phase in ("opening", "middlegame", "endgame"):
        pm = [m for m in moves if m["phase"] == phase]
        phases.append({"phase": phase, "moves": len(pm), "accuracy": _mean(m["accuracy"] for m in pm),
                       "errors_per_game": sum(m["judgement"] in ("mistake", "blunder") for m in pm) / len(games)})

    # ---- mistake types
    kinds = Counter(m["kind"] for m in bad if m["kind"])
    kind_rows = [{"kind": k, "label": KIND_LABELS.get(k, k), "count": n, "share": n / len(bad)}
                 for k, n in kinds.most_common()]

    # ---- clock: blunders per 100 moves by time left, and fast blunders
    bucket_moves, bucket_blunders = Counter(), Counter()
    fast_moves = fast_blunders = slow_moves = slow_blunders = 0
    for g in games:
        if not g["clocks_ms"]:
            continue
        clocks = g["clocks_ms"].split()
        base, _ = report.parse_time_control(g["time_control"])
        spent = report.time_spent(g["clocks_ms"], g["time_control"])
        for m in by_game[g["id"]]:
            left = clock_before(clocks, base, m["ply"])
            if left is not None:
                label = next(lbl for limit, lbl in CLOCK_BUCKETS if left >= limit)
                bucket_moves[label] += 1
                bucket_blunders[label] += m["judgement"] == "blunder"
            used = spent[m["ply"]] if m["ply"] < len(spent) else None
            if used is not None:
                if used < 5000:
                    fast_moves += 1
                    fast_blunders += m["judgement"] == "blunder"
                else:
                    slow_moves += 1
                    slow_blunders += m["judgement"] == "blunder"
    clock = {
        "buckets": [{"label": lbl, "moves": bucket_moves[lbl], "blunders": bucket_blunders[lbl],
                     "rate": 100 * bucket_blunders[lbl] / bucket_moves[lbl] if bucket_moves[lbl] else None}
                    for _, lbl in CLOCK_BUCKETS],
        "fast": {"moves": fast_moves, "blunders": fast_blunders,
                 "rate": 100 * fast_blunders / fast_moves if fast_moves else None},
        "slow": {"moves": slow_moves, "blunders": slow_blunders,
                 "rate": 100 * slow_blunders / slow_moves if slow_moves else None},
        "games_with_clocks": sum(1 for g in games if g["clocks_ms"]),
    }

    # ---- openings (by family and my colour)
    groups = defaultdict(list)
    for g in games:
        groups[(g["opening_family"] or "Unknown", g["my_color"])].append(g)
    openings = []
    for (family, color), gs in groups.items():
        g_accs = [my_accuracy(g) for g in gs if my_accuracy(g) is not None]
        opening_moves = [m for g in gs for m in by_game[g["id"]] if m["phase"] == "opening"]
        openings.append({
            "family": family, "color": color, "games": len(gs),
            "wins": sum(g["my_result"] == "win" for g in gs), "draws": sum(g["my_result"] == "draw" for g in gs),
            "losses": sum(g["my_result"] == "loss" for g in gs), "score": score([g["my_result"] for g in gs]),
            "accuracy": sum(g_accs) / len(g_accs) if g_accs else None,
            "opening_accuracy": _mean(m["accuracy"] for m in opening_moves),
            "errors_per_game": sum(m["judgement"] in ("mistake", "blunder")
                                   for g in gs for m in by_game[g["id"]]) / len(gs),
        })
    openings.sort(key=lambda o: -o["games"])

    # ---- colours
    colors = []
    for color in ("white", "black"):
        gs = [g for g in games if g["my_color"] == color]
        g_accs = [my_accuracy(g) for g in gs if my_accuracy(g) is not None]
        colors.append({"color": color, "games": len(gs), "score": score([g["my_result"] for g in gs]),
                       "accuracy": sum(g_accs) / len(g_accs) if g_accs else None})

    data = {"games": len(games), "overall": overall, "trend": trend, "phases": phases, "kinds": kind_rows,
            "clock": clock, "openings": openings, "colors": colors,
            "time_classes": [r[0] for r in conn.execute(
                "SELECT time_class FROM games GROUP BY time_class ORDER BY COUNT(*) DESC")]}
    data["weaknesses"] = weaknesses(data)
    return data


def weaknesses(d: dict, top: int = 3) -> list[dict]:
    """Rank candidate weaknesses. Severity scores are heuristic but comparable in size:
    roughly 'how far from my own average', so the list adapts to my level."""
    out = []
    o = d["overall"]
    vague = {"positional", "missed_opportunity"}   # real but less actionable than e.g. hanging pieces
    for k in d["kinds"][:4]:
        if k["count"] >= 3:
            out.append({"title": k["label"], "severity": 100 * k["share"] * (0.7 if k["kind"] in vague else 1),
                        "detail": f"{k['count']} of your mistakes and blunders ({k['share']:.0%}).",
                        "train": {"kind": k["kind"]}})
    if o["move_accuracy"] is not None:
        for p in d["phases"]:
            if p["accuracy"] is not None and p["moves"] >= 30 and o["move_accuracy"] - p["accuracy"] >= 3:
                gap = o["move_accuracy"] - p["accuracy"]
                out.append({"title": f"The {p['phase']}", "severity": 2.5 * gap,
                            "detail": f"Your move accuracy there is {p['accuracy']:.0f}%, {gap:.0f} points below "
                                      f"your average; {p['errors_per_game']:.1f} mistakes+blunders per game.",
                            "train": {"phase": p["phase"]}})
    if o["score"] is not None:
        for op in d["openings"]:
            if op["games"] >= 5 and op["score"] is not None and o["score"] - op["score"] >= 10:
                gap = o["score"] - op["score"]
                out.append({"title": f"{op['family']} as {op['color'].capitalize()}", "severity": 0.8 * gap,
                            "detail": f"You score {op['score']:.0f}% in {op['games']} games "
                                      f"({gap:.0f} points below your average).",
                            "train": {"opening": op["family"], "color": op["color"]}})
    low = [b for b in d["clock"]["buckets"] if b["label"] in ("20–60 s", "<20 s") and b["moves"]]
    high = [b for b in d["clock"]["buckets"] if b["label"] in ("3+ min", "1–3 min") and b["moves"]]
    low_moves, low_bl = sum(b["moves"] for b in low), sum(b["blunders"] for b in low)
    high_moves, high_bl = sum(b["moves"] for b in high), sum(b["blunders"] for b in high)
    if low_moves >= 20 and low_bl >= 3 and high_moves:
        ratio = (low_bl / low_moves) / max(high_bl / high_moves, 1e-6)
        if ratio >= 1.5:
            out.append({"title": "Time trouble", "severity": 15 * (ratio - 1),
                        "detail": f"With under a minute left you blunder {ratio:.1f}× as often "
                                  f"({low_bl} blunders in {low_moves} moves).",
                        "train": {"low_clock": "1"}})
    fast, slow = d["clock"]["fast"], d["clock"]["slow"]
    if fast["moves"] >= 30 and fast["blunders"] >= 3 and slow["rate"]:
        ratio = fast["rate"] / slow["rate"]
        if ratio >= 1.5:
            out.append({"title": "Moving too fast", "severity": 12 * (ratio - 1),
                        "detail": f"Moves played in under 5 seconds are {ratio:.1f}× as likely to be blunders.",
                        "train": {}})
    white, black = d["colors"]
    if white["score"] is not None and black["score"] is not None and min(white["games"], black["games"]) >= 5:
        weaker, stronger = (white, black) if white["score"] < black["score"] else (black, white)
        if stronger["score"] - weaker["score"] >= 12:
            out.append({"title": f"Playing {weaker['color'].capitalize()}",
                        "severity": 0.6 * (stronger["score"] - weaker["score"]),
                        "detail": f"You score {weaker['score']:.0f}% as {weaker['color']} vs "
                                  f"{stronger['score']:.0f}% as {stronger['color']}.",
                        "train": {"color": weaker["color"]}})
    out.sort(key=lambda w: -w["severity"])
    return out[:top]


def _mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None
