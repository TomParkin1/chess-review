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
        "clock": {"buckets": [{"label": "3+ min", "moves": 200, "blunders": 4},
                              {"label": "1–3 min", "moves": 100, "blunders": 2},
                              {"label": "20–60 s", "moves": 30, "blunders": 6},
                              {"label": "<20 s", "moves": 10, "blunders": 3}],
                  "fast": {"moves": 0, "blunders": 0, "rate": None}, "slow": {"moves": 0, "blunders": 0, "rate": None}},
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
