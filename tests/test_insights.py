from chessreview import insights, report
from chessreview.insights import weaknesses


def base():
    return {
        "overall": {"score": 60.0, "move_accuracy": 88.0},
        "kinds": [
            {"kind": "hanging_piece", "label": "Hanging pieces", "count": 30, "share": 0.30},
            {"kind": "positional", "label": "Positional slips", "count": 30, "share": 0.30},
        ],
        "phases": [
            {"phase": "opening", "moves": 100, "accuracy": 90.0, "errors_per_game": 0.5},
            {"phase": "middlegame", "moves": 200, "accuracy": 80.0, "errors_per_game": 2.0},
            {"phase": "endgame", "moves": 10, "accuracy": 50.0, "errors_per_game": 0.1},
        ],
        "openings": [
            {"family": "Dutch Defense", "color": "black", "games": 6, "score": 30.0},
            {"family": "Rare Opening", "color": "white", "games": 2, "score": 0.0},
        ],
        "clock": {
            "buckets": [
                {"label": "30%+ left", "low": False, "moves": 200, "blunders": 4},
                {"label": "10–30% left", "low": False, "moves": 100, "blunders": 2},
                {"label": "3–10% left", "low": True, "moves": 30, "blunders": 6},
                {"label": "under 3% left", "low": True, "moves": 10, "blunders": 3},
            ],
            "fast": {"moves": 0, "blunders": 0, "rate": None},
            "slow": {"moves": 0, "blunders": 0, "rate": None},
            "usual": None,
        },
        "colors": [{"color": "white", "games": 10, "score": 60.0}, {"color": "black", "games": 10, "score": 58.0}],
    }


def test_weaknesses_ranked_and_filtered():
    found = weaknesses(base(), top=10)
    titles = [w["title"] for w in found]
    assert titles[0] == "Time trouble"  # 9/40 vs 6/300 blunders: ~11x as often
    assert "Dutch Defense as Black" in titles
    assert "Rare Opening as White" not in titles  # too few games to judge
    assert "The middlegame" in titles
    assert "The endgame" not in titles  # too few moves to judge
    assert "Playing Black" not in titles  # 2 points apart is noise
    # vague kinds weigh less than concrete ones with the same share
    sev = {w["title"]: w["severity"] for w in found}
    assert sev["Hanging pieces"] > sev["Positional slips"]
    assert found[0]["train"] == {"low_clock": "1"}


def test_clock_rules_follow_the_time_control():
    """F7 / P13: time trouble and fast moves are shares of base + 40 x increment, not fixed seconds."""
    assert report.game_budget_ms("600") == 600_000
    assert report.game_budget_ms("900+10") == 1_300_000
    assert report.game_budget_ms("1/86400") is None and report.game_budget_ms("-") is None
    # 10+0 keeps the old numbers: under 60 s is time trouble, under 5 s a very fast move.
    assert report.low_clock_ms("600") == 60_000 and report.fast_move_ms("600") == 5_000
    assert report.is_low_clock(59_000, "600") and not report.is_low_clock(61_000, "600")
    # 3+0: 40 s left is fine, 15 s is trouble. 15+10: 2 minutes is already trouble.
    assert not report.is_low_clock(40_000, "180") and report.is_low_clock(15_000, "180")
    assert report.is_low_clock(120_000, "900+10") and not report.is_low_clock(140_000, "900+10")
    assert report.fast_move_ms("180") == 1_500
    assert not report.is_low_clock(1_000, "1/86400")  # daily games have no budget
    assert [report.clock_bucket(ms, "600") for ms in (200_000, 100_000, 30_000, 10_000)] == [
        "30%+ left",
        "10–30% left",
        "3–10% left",
        "under 3% left",
    ]
    assert report.clock_bucket(30_000, "180") == "10–30% left"  # 30 s of 3 minutes is 17%
    assert report.clock_bucket(30_000, None) is None
    assert report.tc_label("600") == "10+0" and report.tc_label("900+10") == "15+10" and report.tc_label("180") == "3+0"


def test_time_trouble_uses_the_clock_before_my_move():
    # Per-ply clocks: my clock before ply 4 is the one after ply 2 ("170000" in a 3+0 game).
    clocks = "179000 178000 170000 175000 14000 160000"
    assert not insights.in_time_trouble(clocks, "180", 4)  # 170 s of 180
    assert insights.in_time_trouble(clocks, "180", 6)  # 14 s of 180
    assert not insights.in_time_trouble(clocks, "600", 2)  # 179 s of 10 minutes
    assert insights.in_time_trouble(clocks, "1800", 2)  # 179 s of 30 minutes is under 10% already
    assert not insights.in_time_trouble(None, "180", 6)


def test_no_blunders_with_time_left_doesnt_explode_the_ratio():
    """E1: with 0 blunders on a healthy clock the ratio was divided by 1e-6 (150,000x, severity in
    the millions), so "Time trouble" was always #1 and Today's focus."""
    d = base()
    for b in d["clock"]["buckets"]:
        if not b["low"]:
            b["blunders"] = 0
    tt = next(w for w in weaknesses(d, top=10) if w["title"] == "Time trouble")
    assert tt["severity"] <= 15 * (insights.RATIO_CAP - 1)
    assert "over 10×" in tt["detail"]


def test_moving_too_fast_shows_when_slow_moves_never_blunder_and_can_be_trained():
    """E1: the rule was skipped exactly when it was most pronounced (no blunders on slow moves),
    and it had no Practice filter, so Today's focus could never train it."""
    d = base()
    d["clock"]["fast"] = {"moves": 40, "blunders": 6, "rate": 15.0}
    d["clock"]["slow"] = {"moves": 400, "blunders": 0, "rate": 0.0}
    fast = next(w for w in weaknesses(d, top=10) if w["title"] == "Moving too fast")
    assert fast["train"] == {"fast": "1"}


def test_fast_move_is_relative_to_the_time_control():
    clocks = "600000 600000 598000 590000 597000 589000"  # my move at ply 4 took 1 s, at ply 2 2 s
    assert insights.fast_move(clocks, "600", 4)
    assert not insights.fast_move(clocks, "1/86400", 4)  # daily: no budget, never "fast"
    assert not insights.fast_move(None, "600", 4)


# ------------------------------------------------------------ E4: endgames

import chess  # noqa: E402

from chessreview import db  # noqa: E402

ENDGAME_FENS = {
    "pawn": "8/5k2/8/3p4/3P4/8/5K2/8 w - - 0 40",
    "rook": "8/5k2/8/3p4/3P4/8/5K2/R6r w - - 0 40",
    "minor": "8/5k2/2n5/3p4/3P4/8/5K2/2B5 w - - 0 40",
    "rook_minor": "8/5k2/2n5/3p4/3P4/8/5K2/R6r w - - 0 40",
    "queen": "8/5k2/8/3p4/3P4/8/5K2/Q6q w - - 0 40",
    "mixed": "8/5k2/8/3p4/3P4/8/5K2/Q6r w - - 0 40",
}


def test_endgame_types_by_material():
    for kind, fen in ENDGAME_FENS.items():
        assert insights.endgame_type(chess.Board(fen)) == kind, kind


def endgame_db(tmp_path):
    """Games entering an endgame at ply 40 (my move as White): a rook endgame entered winning and
    won, one entered winning and drawn, a pawn endgame entered losing and saved, a queen endgame
    entered losing and lost; and a game that never reached an endgame but was won from +5."""
    c = db.connect(tmp_path / "e.db")
    games = [  # id, result, endgame fen, win_before at the endgame's start
        (1, "win", ENDGAME_FENS["rook"], 0.9),
        (2, "draw", ENDGAME_FENS["rook"], 0.9),
        (3, "draw", ENDGAME_FENS["pawn"], -0.9),
        (4, "loss", ENDGAME_FENS["queen"], -0.9),
        (5, "win", None, None),
    ]
    for gid, result, fen, win in games:
        c.execute(
            """INSERT INTO games (id, dedupe_key, white, black, white_elo, black_elo, my_color, my_result, date,
                         moves_uci, num_plies, pgn, analysed_at, end_time, time_class, rated)
                     VALUES (?, ?, 'me', 'opp', 1000, 1000, 'white', ?, '2026-10-01', '', 60, '', '2026-10-01', ?,
                             'rapid', 1)""",
            (gid, f"k{gid}", result, f"2026-10-0{gid} 12:00"),
        )
        c.execute(
            """INSERT INTO mistakes (game_id, ply, judgement, phase, is_mine, accuracy, win_before)
                     VALUES (?, 20, 'good', 'middlegame', 1, 80, ?)""",
            (gid, 0.9 if gid == 5 else 0.0),
        )
        if fen:
            c.execute("INSERT INTO positions (game_id, ply, fen, score_cp) VALUES (?, 40, ?, 0)", (gid, fen))
            c.execute(
                """INSERT INTO mistakes (game_id, ply, judgement, phase, is_mine, accuracy, win_before)
                         VALUES (?, 40, 'mistake', 'endgame', 1, 60, ?), (?, 42, 'good', 'endgame', 1, 90, ?)""",
                (gid, win, gid, win),
            )
    c.commit()
    return c


def test_endgame_results_conversion_and_saves(tmp_path):
    c = endgame_db(tmp_path)
    e = insights.compute(c)["endgames"]
    assert e["games"] == 4 and e["share"] == 0.8
    types = {t["type"]: t for t in e["types"]}
    assert set(types) == {"rook", "pawn", "queen"}
    assert (types["rook"]["games"], types["rook"]["won"], types["rook"]["drawn"]) == (2, 1, 1)
    assert types["rook"]["accuracy"] == 75 and types["rook"]["errors_per_game"] == 1
    assert e["endgame_conversion"] == {"games": 2, "won": 1, "drawn": 1, "lost": 0, "rate": 50}
    assert e["saves"] == {"games": 2, "won": 0, "drawn": 1, "lost": 1, "rate": 0}
    assert e["conversion"]["games"] == 3  # winning at any point: games 1, 2 and the endgame-less 5
    c.close()


def test_endgame_training_progress(tmp_path):
    c = endgame_db(tmp_path)
    t = insights.drill_progress(c)
    assert t["essentials"]["total"] == 15 and t["essentials"]["solved"] == 0
    assert t["puzzles"] == {"tried": 0, "clean": 0, "rate": None}
    c.close()


def with_endgames(conversion=None, endgame_acc=None):
    d = base()
    if endgame_acc is not None:
        d["phases"][2] = {"phase": "endgame", "moves": 60, "accuracy": endgame_acc, "errors_per_game": 0.5}
    d["endgames"] = {"endgame_conversion": conversion or {"games": 0, "won": 0, "drawn": 0, "lost": 0, "rate": None}}
    return d


def test_endgames_weakness_from_conversion():
    found = weaknesses(with_endgames({"games": 8, "won": 4, "drawn": 2, "lost": 2, "rate": 50}), top=10)
    w = next(w for w in found if w["title"] == "Endgames")
    assert w["train"] == {"phase": "endgame"} and "won 4 of the 8 endgames you started clearly winning" in w["detail"]
    assert w["severity"] == 30
    assert "Endgames" not in [
        w["title"] for w in weaknesses(with_endgames({"games": 4, "won": 1, "drawn": 3, "lost": 0, "rate": 25}), top=10)
    ]  # too few to judge
    assert "Endgames" not in [
        w["title"] for w in weaknesses(with_endgames({"games": 8, "won": 7, "drawn": 1, "lost": 0, "rate": 88}), top=10)
    ]  # converts well


def test_endgames_weakness_from_accuracy_replaces_the_endgame_phase():
    found = weaknesses(with_endgames(endgame_acc=70.0), top=10)
    titles = [w["title"] for w in found]
    assert "Endgames" in titles and "The endgame" not in titles  # one card, not two
    w = next(w for w in found if w["title"] == "Endgames")
    assert "10 points below your middlegame" in w["detail"]
    assert w["severity"] == 2.5 * (88 - 70)  # as urgent as "The endgame" (18 below average) was
