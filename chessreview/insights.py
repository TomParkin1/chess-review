"""Aggregations for the Insights page and the Home dashboard.

Everything is computed from stored data (games, my moves in `mistakes`, clocks), for an
optional time class and "last N games" window. `weaknesses()` ranks candidate problems
(mistake types, game phases, openings, clock trouble, colour) with simple, explainable
severity scores and attaches a "Train this" filter for the Practice page.
"""

import sqlite3
from collections import Counter, defaultdict

import chess

from . import level, report

KIND_LABELS = {
    "missed_opportunity": "Letting advantages slip", "positional": "Positional slips",
    "loses_material": "Losing material", "hanging_piece": "Hanging pieces",
    "missed_tactic": "Missed tactics", "king_safety": "King safety", "missed_capture": "Missed captures",
    "allowed_mate": "Walking into mates", "missed_mate": "Missed checkmates", "fork": "Allowing forks",
    "discovered_attack": "Allowing discovered attacks",
}


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


def in_time_trouble(clocks_ms: str | None, tc: str | None, ply: int) -> bool:
    """Was I in time trouble (report.is_low_clock) before my move at `ply`? Needs chess.com clocks."""
    if not clocks_ms:
        return False
    base, _ = report.parse_time_control(tc)
    return report.is_low_clock(clock_before(clocks_ms.split(), base, ply), tc)


def fast_move(clocks_ms: str | None, tc: str | None, ply: int) -> bool:
    """Did I play my move at `ply` very fast (report.fast_move_ms)? Needs chess.com clocks."""
    if not clocks_ms or report.game_budget_ms(tc) is None:
        return False
    spent = report.time_spent(clocks_ms, tc)
    used = spent[ply] if ply < len(spent) else None
    return used is not None and used < report.fast_move_ms(tc)


def _ratio(bad: int, moves: int, base_bad: int, base_moves: int) -> float:
    """How many times as often `bad` happens as `base_bad`, per move. The base rate is smoothed
    (one extra blunder in one extra move), so none in the base doesn't give a near-infinite ratio."""
    return (bad / moves) / ((base_bad + 1) / (base_moves + 1))


RATIO_CAP = 10      # beyond this, "N× as often" is just noise from small counts


def _times(ratio: float) -> str:
    return f"over {RATIO_CAP}×" if ratio >= RATIO_CAP else f"{ratio:.1f}×"


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

    # ---- clock: blunders per 100 moves by the share of my time left, and very fast moves (all
    # relative to each game's time control, see report.CLOCK_BUCKETS)
    bucket_moves, bucket_blunders = Counter(), Counter()
    fast_moves = fast_blunders = slow_moves = slow_blunders = 0
    controls = Counter()
    for g in games:
        tc = g["time_control"]
        if not g["clocks_ms"] or report.game_budget_ms(tc) is None:
            continue
        controls[tc] += 1
        clocks = g["clocks_ms"].split()
        base, _ = report.parse_time_control(tc)
        spent = report.time_spent(g["clocks_ms"], tc)
        fast_ms = report.fast_move_ms(tc)
        for m in by_game[g["id"]]:
            label = report.clock_bucket(clock_before(clocks, base, m["ply"]), tc)
            if label is not None:
                bucket_moves[label] += 1
                bucket_blunders[label] += m["judgement"] == "blunder"
            used = spent[m["ply"]] if m["ply"] < len(spent) else None
            if used is not None:
                if used < fast_ms:
                    fast_moves += 1
                    fast_blunders += m["judgement"] == "blunder"
                else:
                    slow_moves += 1
                    slow_blunders += m["judgement"] == "blunder"
    usual = controls.most_common(1)[0][0] if controls else None
    clock = {
        "buckets": [{"label": lbl, "low": share < report.LOW_CLOCK_SHARE, "moves": bucket_moves[lbl],
                     "blunders": bucket_blunders[lbl],
                     "rate": 100 * bucket_blunders[lbl] / bucket_moves[lbl] if bucket_moves[lbl] else None}
                    for share, lbl in report.CLOCK_BUCKETS],
        "fast": {"moves": fast_moves, "blunders": fast_blunders,
                 "rate": 100 * fast_blunders / fast_moves if fast_moves else None},
        "slow": {"moves": slow_moves, "blunders": slow_blunders,
                 "rate": 100 * slow_blunders / slow_moves if slow_moves else None},
        "games_with_clocks": sum(controls.values()),
        # What the shares mean in my most common time control, e.g. 10+0: 60 s and 5 s.
        "usual": usual and {"label": report.tc_label(usual), "low_ms": report.low_clock_ms(usual),
                            "fast_ms": report.fast_move_ms(usual),
                            "bucket_ms": [round(share * report.game_budget_ms(usual))
                                          for share, _ in report.CLOCK_BUCKETS[:-1]]},
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
            "clock": clock, "openings": openings, "colors": colors, "endgames": endgames(conn, games, by_game),
            "time_classes": [r[0] for r in conn.execute(
                "SELECT time_class FROM games GROUP BY time_class ORDER BY COUNT(*) DESC")]}
    data["weaknesses"] = weaknesses(data)
    return data


# ------------------------------------------------------------ endgames (E4)

ENDGAME_TYPES = {"pawn": "Pawn endgames", "rook": "Rook endgames", "minor": "Minor-piece endgames",
                 "rook_minor": "Rook + minor piece", "queen": "Queen endgames", "mixed": "Queen + other pieces"}
MIN_CONVERSION_GAMES = 5      # winning endgames needed before conversion counts as a weakness
CONVERSION_MISS = 0.25        # ... and the share of them not won from which it does


def endgame_type(board: chess.Board) -> str:
    """The material of an endgame position: pawns only, rooks, minor pieces, rooks and minor
    pieces, queens, or queens with other pieces."""
    pieces = {pt for pt in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN) if board.pieces_mask(pt, True)
              or board.pieces_mask(pt, False)}
    if not pieces:
        return "pawn"
    if pieces == {chess.ROOK}:
        return "rook"
    if pieces <= {chess.KNIGHT, chess.BISHOP}:
        return "minor"
    if chess.QUEEN not in pieces:
        return "rook_minor"
    if pieces == {chess.QUEEN}:
        return "queen"
    return "mixed"


def _outcomes(gs: list[sqlite3.Row]) -> dict:
    won = sum(g["my_result"] == "win" for g in gs)
    drawn = sum(g["my_result"] == "draw" for g in gs)
    return {"games": len(gs), "won": won, "drawn": drawn, "lost": len(gs) - won - drawn,
            "rate": 100 * won / len(gs) if gs else None}


def endgames(conn: sqlite3.Connection, games: list[sqlite3.Row], by_game: dict) -> dict:
    """How my endgames go: the games that reached one (my first move in the endgame phase), by
    type of endgame (its material then), how often I won the ones I entered clearly winning and
    saved the ones I entered clearly losing ("clearly" = level.winning_chance at my current
    rating, the bar Progress uses for conversion), and accuracy against the middlegame."""
    winning = level.winning_chance(level.current_rating(conn))
    first = {}
    for g in games:
        moves = [m for m in by_game[g["id"]] if m["phase"] == "endgame"]
        if moves:
            first[g["id"]] = min(moves, key=lambda m: m["ply"])
    fens = {}
    keys = [(gid, m["ply"]) for gid, m in first.items()]
    for chunk in range(0, len(keys), 400):
        part = keys[chunk:chunk + 400]
        rows = conn.execute(f"""SELECT game_id, fen FROM positions WHERE (game_id, ply) IN
                                (VALUES {",".join("(?, ?)" for _ in part)})""", [v for k in part for v in k])
        fens |= {r["game_id"]: r["fen"] for r in rows}
    reached = [g for g in games if g["id"] in first]
    by_type = defaultdict(list)
    for g in reached:
        if g["id"] in fens:
            by_type[endgame_type(chess.Board(fens[g["id"]]))].append(g)
    types = []
    for key, label in ENDGAME_TYPES.items():
        gs = by_type.get(key, [])
        if not gs:
            continue
        mine = [m for g in gs for m in by_game[g["id"]] if m["phase"] == "endgame"]
        types.append({"type": key, "label": label, **_outcomes(gs), "score": score([g["my_result"] for g in gs]),
                      "accuracy": _mean(m["accuracy"] for m in mine),
                      "errors_per_game": sum(m["judgement"] in ("mistake", "blunder") for m in mine) / len(gs)})
    start = {gid: m["win_before"] for gid, m in first.items() if m["win_before"] is not None}
    won_any = [g for g in games if any((m["win_before"] or -1) >= winning for m in by_game[g["id"]])]
    return {
        "games": len(reached), "share": len(reached) / len(games) if games else None,
        "score": score([g["my_result"] for g in reached]),
        "types": types,
        "conversion": _outcomes(won_any),                                                   # winning at any point
        "endgame_conversion": _outcomes([g for g in reached if start.get(g["id"], -1) >= winning]),
        "saves": _outcomes([g for g in reached if start.get(g["id"], 1) <= -winning]),      # entered clearly losing
        "winning_cp": level.winning_cp(level.current_rating(conn)),
    }


def drill_progress(conn: sqlite3.Connection) -> dict:
    """My endgame training: essential endgames, won positions from my games, Lichess endgame
    puzzles (first tries) and my own endgame mistakes in spaced repetition."""
    from . import convert       # convert -> level -> ...: imported late, only Insights needs it
    essentials = convert.endgame_overview(conn)
    positions = convert.convert_overview(conn)
    first_tries = [r["result"] for r in conn.execute("""
        SELECT a.result FROM puzzle_attempts a JOIN puzzles p ON p.id = a.puzzle_id
        WHERE p.themes LIKE '% endgame %'
          AND a.id = (SELECT MIN(a2.id) FROM puzzle_attempts a2 WHERE a2.puzzle_id = a.puzzle_id)""")]
    cards = conn.execute("""SELECT COUNT(*) AS n, SUM(c.mastered_on IS NOT NULL) AS mastered,
                                   SUM(c.due_on IS NOT NULL OR c.mastered_on IS NOT NULL) AS studied
                            FROM cards c JOIN mistakes m ON m.game_id = c.game_id AND m.ply = c.ply
                            WHERE c.kind = 'mistake' AND m.phase = 'endgame'""").fetchone()
    count = lambda rows, *states: sum(r["status"] in states for r in rows)
    return {
        "essentials": {"total": len(essentials), "solved": count(essentials, "solved", "mastered"),
                       "mastered": count(essentials, "mastered"), "unlocked": sum(e["unlocked"] for e in essentials)},
        "positions": {"total": len(positions), "tried": len(positions) - count(positions, "new"),
                      "converted": count(positions, "solved", "mastered")},
        "puzzles": {"tried": len(first_tries), "clean": first_tries.count("solved"),
                    "rate": 100 * first_tries.count("solved") / len(first_tries) if first_tries else None},
        "mistakes": {"total": cards["n"] or 0, "studied": cards["studied"] or 0, "mastered": cards["mastered"] or 0},
    }


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
    low = [b for b in d["clock"]["buckets"] if b["low"] and b["moves"]]
    high = [b for b in d["clock"]["buckets"] if not b["low"] and b["moves"]]
    low_moves, low_bl = sum(b["moves"] for b in low), sum(b["blunders"] for b in low)
    high_moves, high_bl = sum(b["moves"] for b in high), sum(b["blunders"] for b in high)
    if low_moves >= 20 and low_bl >= 3 and high_moves:
        ratio = min(_ratio(low_bl, low_moves, high_bl, high_moves), RATIO_CAP)
        if ratio >= 1.5:
            out.append({"title": "Time trouble", "severity": 15 * (ratio - 1),
                        "detail": f"In time trouble (under {report.LOW_CLOCK_SHARE:.0%} of your clock) you blunder "
                                  f"{_times(ratio)} as often ({low_bl} blunders in {low_moves} moves).",
                        "train": {"low_clock": "1"}})
    fast, slow = d["clock"]["fast"], d["clock"]["slow"]
    if fast["moves"] >= 30 and fast["blunders"] >= 3 and slow["moves"]:
        ratio = min(_ratio(fast["blunders"], fast["moves"], slow["blunders"], slow["moves"]), RATIO_CAP)
        if ratio >= 1.5:
            usual = d["clock"]["usual"]
            example = f" (under {usual['fast_ms'] / 1000:g} s at {usual['label']})" if usual else ""
            out.append({"title": "Moving too fast", "severity": 12 * (ratio - 1),
                        "detail": f"Very fast moves{example} are {_times(ratio)} as likely to be blunders.",
                        "train": {"fast": "1"}})
    white, black = d["colors"]
    if white["score"] is not None and black["score"] is not None and min(white["games"], black["games"]) >= 5:
        weaker, stronger = (white, black) if white["score"] < black["score"] else (black, white)
        if stronger["score"] - weaker["score"] >= 12:
            out.append({"title": f"Playing {weaker['color'].capitalize()}",
                        "severity": 0.6 * (stronger["score"] - weaker["score"]),
                        "detail": f"You score {weaker['score']:.0f}% as {weaker['color']} vs "
                                  f"{stronger['score']:.0f}% as {stronger['color']}.",
                        "train": {"color": weaker["color"]}})
    endgame = _endgame_weakness(d)
    if endgame:     # it says what "The endgame" (phase accuracy) would, and more: one card, as urgent
        phase = [w for w in out if w["train"] == {"phase": "endgame"}]
        endgame["severity"] = max([endgame["severity"], *(w["severity"] for w in phase)])
        out = [w for w in out if w not in phase] + [endgame]
    out.sort(key=lambda w: -w["severity"])
    return out[:top]


def _endgame_weakness(d: dict) -> dict | None:
    """"Endgames" (E4): my endgame moves clearly less accurate than my middlegame ones, or the
    endgames I entered clearly winning too often not won. Trains my endgame positions (and, in
    Today's focus, endgame puzzles)."""
    phases = {p["phase"]: p for p in d["phases"]}
    end, mid = phases.get("endgame"), phases.get("middlegame")
    reasons, severity = [], 0.0
    conv = d.get("endgames", {}).get("endgame_conversion")
    if conv and conv["games"] >= MIN_CONVERSION_GAMES:
        missed = 1 - conv["won"] / conv["games"]
        if missed >= CONVERSION_MISS:
            reasons.append(f"you won {conv['won']} of the {conv['games']} endgames you started clearly winning")
            severity = max(severity, 60 * missed)
    if end and mid and end["accuracy"] is not None and mid["accuracy"] is not None and end["moves"] >= 30:
        gap = mid["accuracy"] - end["accuracy"]
        if gap >= 3:
            reasons.append(f"your endgame moves are {end['accuracy']:.0f}% accurate, {gap:.0f} points below your "
                           f"middlegame")
            severity = max(severity, 2.5 * gap)
    if not reasons:
        return None
    detail = " and ".join(reasons)
    return {"title": "Endgames", "severity": severity, "detail": detail[0].upper() + detail[1:] + ".",
            "train": {"phase": "endgame"}}


def _mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None
