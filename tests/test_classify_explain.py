import chess
import pytest

from chessreview.classify import Eval, judge
from chessreview.config import load_config
from chessreview.explain import MoveContext, explain


def ctx(fen_or_moves, move, best, best_line, reply_line, before, after):
    if "/" in fen_or_moves:
        board = chess.Board(fen_or_moves)
    else:
        board = chess.Board()
        for san in fen_or_moves.split():
            board.push_san(san)
    u = chess.Move.from_uci
    return MoveContext(board=board, move=u(move), best=u(best) if best else None,
                       best_line=[u(m) for m in best_line], reply_line=[u(m) for m in reply_line],
                       before=before, after=after)


@pytest.mark.parametrize("before_cp,after_cp,expected", [
    (0, -50, "good"), (0, -100, "inaccuracy"), (0, -120, "mistake"), (0, -200, "blunder"),
    (30, -40, "inaccuracy"), (900, 700, "good"),  # still clearly winning: small drop
])
def test_judge_winchance(before_cp, after_cp, expected):
    judgement, _, _ = judge(Eval(before_cp, None), Eval(after_cp, None), load_config())
    assert judgement == expected


@pytest.mark.parametrize("before,after,expected", [
    (Eval(None, 1), Eval(750, None), "mistake"),     # missed mate in 1 but still +7.5
    (Eval(None, 3), Eval(1200, None), "inaccuracy"), # missed mate, still totally winning
    (Eval(None, 2), Eval(300, None), "blunder"),
    (Eval(None, 2), Eval(None, 4), "good"),          # still mating, just slower
    (Eval(50, None), Eval(None, -3), "blunder"),     # walked into mate
    (Eval(-800, None), Eval(None, -5), "mistake"),   # already lost, now mated
    (Eval(None, -4), Eval(None, -2), "good"),        # was already getting mated
])
def test_judge_mate_rules(before, after, expected):
    assert judge(before, after, load_config())[0] == expected


def test_hanging_piece():
    kind, text = explain(ctx("e4 e5 Nf3 Nc6", "f3e5", "f1b5", ["f1b5"], ["c6e5"],
                             Eval(30, None), Eval(-250, None)))
    assert kind == "hanging_piece"
    assert "knight to e5" in text and "a knight for a pawn" in text


def test_fork():
    kind, text = explain(ctx("4k3/8/8/8/1n6/8/7P/R3K3 w - - 0 1", "h2h3", "e1d2", ["e1d2"],
                             ["b4c2", "e1d2", "c2a1"], Eval(0, None), Eval(-500, None)))
    assert kind == "fork"
    assert "Nc2+" in text and "king" in text and "rook" in text


def test_missed_mate():
    kind, text = explain(ctx("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1", "h2h3", "a1a8", ["a1a8"], [],
                             Eval(None, 1), Eval(400, None)))
    assert kind == "missed_mate" and "Ra8#" in text


def test_allowed_mate():
    kind, text = explain(ctx("r5k1/5ppp/8/8/8/2N5/5PPP/6K1 w - - 0 1", "c3d5", "h2h3", ["h2h3"],
                             ["a8a1"], Eval(0, None), Eval(None, -1)))
    assert kind == "allowed_mate" and "Ra1#" in text


def test_missed_capture_of_undefended_piece():
    kind, text = explain(ctx("4k3/8/8/3q4/8/8/8/3QK3 w - - 0 1", "e1e2", "d1d5", ["d1d5"], [],
                             Eval(800, None), Eval(-800, None)))
    assert kind == "missed_capture"
    assert "Qxd5" in text and "undefended" in text and "queen" in text


def test_exposed_king():
    kind, text = explain(ctx("e4 e5", "f2f3", "g1f3", ["g1f3"], ["d8h4"],
                             Eval(30, None), Eval(-150, None)))
    assert kind == "king_safety" and "Qh4" in text and "Best was Nf3" in text


def test_positional_when_nothing_forcing():
    kind, text = explain(ctx("e4 e5", "a2a3", "g1f3", ["g1f3"], ["g8f6"],
                             Eval(30, None), Eval(-90, None)))
    assert kind == "positional" and "Best was Nf3" in text


def test_missed_opportunity_when_winning():
    kind, text = explain(ctx("e4 e5", "a2a3", "g1f3", ["g1f3", "b8c6"], ["g8f6"],
                             Eval(500, None), Eval(100, None)))
    assert kind == "missed_opportunity" and "Stronger was" in text


def test_discovered_attack():
    # Nb5 moves off the d-file and uncovers the rook on d1 against the undefended queen on d8.
    kind, text = explain(ctx("3q4/6k1/8/8/3N4/8/8/3RK3 b - - 0 1", "g7g6", "d8d4", ["d8d4"],
                             ["d4b5", "g6f5", "d1d8"], Eval(800, None), Eval(-900, None)))
    assert kind == "discovered_attack"
    assert "Nb5" in text and "rook on d1" in text and "queen on d8" in text
