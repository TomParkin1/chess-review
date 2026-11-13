from dataclasses import replace

import pytest

from chessreview.config import load_config
from chessreview.live import LiveEngine


@pytest.fixture
def live(stockfish):
    base = load_config()
    cfg = replace(base, review=replace(base.review, live_time=0.2),
                  analysis=replace(base.analysis, max_seconds=1, multipv=3))
    engine = LiveEngine(cfg)
    yield engine
    engine.close()


MATE_IN_ONE = "6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1"


def test_best_move_is_good_and_reply_is_none_after_mate(live):
    r = live.judge(MATE_IN_ONE, "a1a8")
    assert r.is_best and r.judgement == "good" and r.game_over
    assert r.san == "Ra8#" and r.reply_uci is None


def test_blunder_is_explained(live):
    r = live.judge(MATE_IN_ONE, "h2h3")
    assert r.judgement != "good"
    assert r.kind == "missed_mate" and "checkmate" in r.explanation
    assert "Ra8" not in r.explanation  # explanations of attempts never give the answer away


def test_reasonable_alternative_counts_as_good(live):
    start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    for uci in ("e2e4", "d2d4"):
        r = live.judge(start, uci)
        assert r.judgement == "good"
        assert r.reply_uci and r.fen_after_reply  # the opponent's answer, ready to play


def test_illegal_move_raises(live):
    with pytest.raises(ValueError):
        live.judge(MATE_IN_ONE, "a1h8")


def test_stream_deepens_and_new_stream_cancels_old(live):
    start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    snapshots = list(live.stream(start))
    depths = [s["depth"] for s in snapshots]
    assert depths == sorted(depths) and depths[-1] > 5
    assert len(snapshots[-1]["lines"]) == 3

    first = live.stream(start)
    next(first)                      # first stream is running and its reader is paused
    second = list(live.stream(MATE_IN_ONE))
    assert second[-1]["lines"][0]["uci"] == "a1a8"
    assert not any(s.get("done") for s in first)  # cancelled: ends without a final snapshot
