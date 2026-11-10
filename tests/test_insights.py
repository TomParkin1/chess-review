from chessreview import insights, report
from chessreview.insights import weaknesses


def base():
    return {
        "overall": {"score": 60.0, "move_accuracy": 88.0},
        "kinds": [{"kind": "hanging_piece", "label": "Hanging pieces", "count": 30, "share": 0.30},
                  {"kind": "positional", "label": "Positional slips", "count": 30, "share": 0.30}],
        "phases": [{"phase": "opening", "moves": 100, "accuracy": 90.0, "errors_per_game": 0.5},
                   {"phase": "middlegame", "moves": 200, "accuracy": 80.0, "errors_per_game": 2.0},
                   {"phase": "endgame", "moves": 10, "accuracy": 50.0, "errors_per_game": 0.1}],
        "openings": [{"family": "Dutch Defense", "color": "black", "games": 6, "score": 30.0},
                     {"family": "Rare Opening", "color": "white", "games": 2, "score": 0.0}],
        "clock": {"buckets": [{"label": "30%+ left", "low": False, "moves": 200, "blunders": 4},
                              {"label": "10–30% left", "low": False, "moves": 100, "blunders": 2},
                              {"label": "3–10% left", "low": True, "moves": 30, "blunders": 6},
                              {"label": "under 3% left", "low": True, "moves": 10, "blunders": 3}],
                  "fast": {"moves": 0, "blunders": 0, "rate": None}, "slow": {"moves": 0, "blunders": 0, "rate": None},
                  "usual": None},
        "colors": [{"color": "white", "games": 10, "score": 60.0}, {"color": "black", "games": 10, "score": 58.0}],
    }


def test_weaknesses_ranked_and_filtered():
    found = weaknesses(base(), top=10)
    titles = [w["title"] for w in found]
    assert titles[0] == "Time trouble"            # 9/40 vs 6/300 blunders: ~11x as often
    assert "Dutch Defense as Black" in titles
    assert "Rare Opening as White" not in titles  # too few games to judge
    assert "The middlegame" in titles
    assert "The endgame" not in titles            # too few moves to judge
    assert "Playing Black" not in titles          # 2 points apart is noise
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
    assert not report.is_low_clock(1_000, "1/86400")    # daily games have no budget
    assert [report.clock_bucket(ms, "600") for ms in (200_000, 100_000, 30_000, 10_000)] == \
        ["30%+ left", "10–30% left", "3–10% left", "under 3% left"]
    assert report.clock_bucket(30_000, "180") == "10–30% left"     # 30 s of 3 minutes is 17%
    assert report.clock_bucket(30_000, None) is None
    assert report.tc_label("600") == "10+0" and report.tc_label("900+10") == "15+10" and report.tc_label("180") == "3+0"


def test_time_trouble_uses_the_clock_before_my_move():
    # Per-ply clocks: my clock before ply 4 is the one after ply 2 ("170000" in a 3+0 game).
    clocks = "179000 178000 170000 175000 14000 160000"
    assert not insights.in_time_trouble(clocks, "180", 4)       # 170 s of 180
    assert insights.in_time_trouble(clocks, "180", 6)           # 14 s of 180
    assert not insights.in_time_trouble(clocks, "600", 2)       # 179 s of 10 minutes
    assert insights.in_time_trouble(clocks, "1800", 2)          # 179 s of 30 minutes is under 10% already
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
    clocks = "600000 600000 598000 590000 597000 589000"   # my move at ply 4 took 1 s, at ply 2 2 s
    assert insights.fast_move(clocks, "600", 4)
    assert not insights.fast_move(clocks, "1/86400", 4)      # daily: no budget, never "fast"
    assert not insights.fast_move(None, "600", 4)
