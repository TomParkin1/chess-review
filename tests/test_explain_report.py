"""Regression tests for the explanation problems P1–P10 and P13 of the Oct 2026 audit (Section 2).

Every position is a real game position with the stored 0.3 s analysis as classify.py sees it:
the FEN, my move, the engine's best move and line, the opponent's line after my move, both evals
(White's point of view), my rating in that game and where the opponent's last move captured.
"""

from typing import NamedTuple

import chess
import pytest

from chessreview.classify import Eval, judge, keeps_long_mate, thresholds_for
from chessreview.config import load_config
from chessreview.explain import LineResult, MoveContext, explain, explain_best, play_line


class Row(NamedTuple):
    fen: str
    move: str
    best: str
    best_line: str
    reply_line: str
    before: tuple[int | None, int | None]  # White's point of view: (cp, mate)
    after: tuple[int | None, int | None]
    rating: int
    prev_capture: str | None


ROWS = {
    "240/30": Row(
        "2kr3r/pppn1ppp/4q3/3pNb2/3P4/2P2Q1P/P1P2PP1/R1B1R1K1 w - - 3 16",
        "e5d7",
        "c3c4",
        "c3c4 d7e5 e1e5 f5e4 f3a3 e6c6 f2f3 e4c2",
        "e6e1 g1h2 f5d7 f3g3 e1e4 c1f4 e4g6 g3f3",
        (52, None),
        (-757, None),
        870,
        None,
    ),
    "186/8": Row(
        "r1bqkbnr/p2ppppp/1p6/2pPn3/4P3/2P5/PP3PPP/RNBQKBNR w KQkq - 1 5",
        "g1f3",
        "f2f4",
        "f2f4 e5g6 g1f3 d8c7 b1a3 a7a6 f4f5 g6e5",
        "e5f3 d1f3 d7d6 c1f4 a7a6 g2g4 e7e5 d5e6",
        (274, None),
        (106, None),
        830,
        None,
    ),
    "221/50": Row(
        "4k3/p1R4p/4p1p1/1b6/4r3/4B3/PP1K1rPP/7R w - - 0 26",
        "d2e1",
        "e3f2",
        "e3f2 e4e2 d2c3 e2f2 h1e1 b5e2 c7h7 f2g2",
        "f2b2 e1d1 e4e3 a2a4 b5a4 d1c1 e3e2 h1f1",
        (639, None),
        (-767, None),
        888,
        "f2",
    ),
    "196/39": Row(
        "1r1qr2k/2p4p/Q5pP/4b3/4p3/P3P3/1PP2PP1/R3K1NR b KQ - 0 20",
        "b8b2",
        "e5b2",
        "e5b2 g1h3 b2c3 e1e2 d8d2 e2f1 c3a1 g2g3",
        "a6c4 d8g5",
        (-715, None),
        (78, None),
        840,
        "a6",
    ),
    "190/14": Row(
        "r1bq1rk1/pppp1ppp/2n2n2/8/4P3/2P1Q3/P1P1BPPP/R1B1K1NR w KQ - 5 8",
        "e4e5",
        "c3c4",
        "c3c4 c6b4 e2d3 f6e4 d3e4 f7f5 c1a3 f8e8",
        "f8e8 f2f4 d7d6 g1f3 d6e5 f3e5 c6e5 f4e5",
        (-54, None),
        (-233, None),
        822,
        None,
    ),
    "35/16": Row(
        "r2q1rk1/ppp2ppp/3p1n2/4p3/1bBnPPb1/2NP4/PPPBN1PP/R2Q1RK1 w - - 3 9",
        "d1e1",
        "h2h3",
        "h2h3 b4c3 h3g4 f6g4 g2g3 c3d2 d1d2 c7c6",
        "d4c2 e1c1 c2a1 e2g3 c7c6 a2a3 b4c5 g1h1",
        (-246, None),
        (-571, None),
        951,
        None,
    ),
    "125/49": Row(
        "2k1r3/p3R1p1/7p/2pn4/q7/2P2P1P/PP1Q2P1/1K5R b - - 0 25",
        "e8e7",
        "d5e7",
        "d5e7 d2e3 a4d7 h1e1 c8d8 e3c5 d7f5 c5f5",
        "d2d5 e7e2 d5c5 c8b7 c5d5 b7a6 d5d6 a6b7",
        (-86, None),
        (576, None),
        837,
        "e7",
    ),
    "199/107": Row(
        "8/8/6P1/5p1p/3k1P2/1b2pK2/8/8 b - - 2 54",
        "d4d5",
        "d4d3",
        "d4d3 f3g3 e3e2 g3h4 e2e1q h4h5 e1e7 h5h6",
        "f3e3 d5c5",
        (None, -8),
        (-962, None),
        864,
        None,
    ),
    "22/31": Row(
        "r4rk1/pp3ppp/2n1p3/2bpP3/5PPq/3Q2NP/PPP5/R3KB1R b KQ - 2 16",
        "c6d4",
        "c6b4",
        "c6b4 d3f3 b4c2 e1d1 c2e3 d1d2 f7f6 a1c1",
        "e1c1 b7b5 c1b1 b5b4 g3e2 d4e2 f1e2 a7a5",
        (-593, None),
        (-102, None),
        920,
        None,
    ),
    "149/35": Row(
        "r3r1k1/p1pqn1p1/1p3pN1/3p1P1R/P2Pp1n1/1QP1P3/1P2K3/RNB5 b - - 1 18",
        "e7g6",
        "e7f5",
        "e7f5 e2e1 g8f7 g6f4 e8h8 f4d5 h8h5 d5b6",
        "f5g6 g4e5 e2f1 c7c6 b3d1 e5f3 f1f2 d7g4",
        (-351, None),
        (60, None),
        878,
        None,
    ),
    "214/19": Row(
        "rnbq1rk1/ppp1bppp/5n2/3Np3/3pP3/P2B1Q1P/1PPPNPP1/R1B1K2R b KQ - 3 10",
        "c7c5",
        "f6d5",
        "f6d5 e4d5 f7f5 d3c4 e7d6 d2d3 d8e8 e2g3",
        "d5e7 d8e7 d3c4 c8e6 c4e6 f7e6 d2d3 a7a5",
        (-192, None),
        (0, None),
        864,
        None,
    ),
    "214/21": Row(
        "rnbq1rk1/pp2bppp/5n2/2pNp3/3pP3/P1PB1Q1P/1P1PNPP1/R1B1K2R b KQ - 0 11",
        "c8e6",
        "f6d5",
        "f6d5 e4d5 f7f5 d3c4 e7d6 d2d3 d8e8 c3d4",
        "d5e7 d8e7 d3c2 f6d7 e1g1 b8c6 d2d3 f7f5",
        (-224, None),
        (-29, None),
        864,
        None,
    ),
    "180/35": Row(
        "r3k3/p1p2p1p/2p5/3p4/4PPb1/1P2Bn2/P1P2Pr1/1R3K1R b q - 4 18",
        "g4h3",
        "g2h2",
        "g2h2 h1h2 f3h2 f1g2 h2f3 e4d5 c6d5 g2g3",
        "h1h3 g2g1 f1e2 d5e4 b1d1 a8d8 d1d8 e8d8",
        (-680, None),
        (-135, None),
        814,
        None,
    ),
    "132/78": Row(
        "8/5pk1/p1KN3p/6p1/5n2/8/P4Q2/8 w - - 4 40",
        "c6b6",
        "f2a7",
        "f2a7 g7f6 a7f7 f6e5 f7c4 e5f6 c4e4 a6a5",
        "g7f6 b6c6 f6g6 a2a4 g6h5 f2f3 h5g6 f3e4",
        (None, 10),
        (966, None),
        845,
        None,
    ),
}

CFG = load_config()


def context(key: str) -> MoveContext:
    """The MoveContext classify.py builds for a stored row (thresholds by my rating in that game)."""
    row = ROWS[key]
    board = chess.Board(row.fen)
    me = board.turn
    u = chess.Move.from_uci
    return MoveContext(
        board=board,
        move=u(row.move),
        best=u(row.best),
        best_line=[u(m) for m in row.best_line.split()],
        reply_line=[u(m) for m in row.reply_line.split()],
        before=_eval(row.before, me),
        after=_eval(row.after, me),
        prev_capture=chess.parse_square(row.prev_capture) if row.prev_capture else None,
        min_loss=thresholds_for(row.rating, CFG, True).min_loss,
    )


def _eval(white: tuple[int | None, int | None], me: chess.Color) -> Eval:
    return Eval.from_row({"score_cp": white[0], "score_mate": white[1]}, me)


def texts(key: str) -> tuple[str, str, str]:
    ctx = context(key)
    kind, text = explain(ctx)
    return kind, text, explain_best(ctx, kind)


def test_p1_a_plain_capture_is_not_a_discovered_attack():
    kind, text, _ = texts("240/30")
    assert kind == "hanging_piece" and "discovered" not in text
    assert "knight on e5 was shielding your rook on e1" in text and "Qxe1+ takes it" in text
    assert "You lose a rook" in text


@pytest.mark.parametrize("key", ["186/8", "214/19", "214/21"])
def test_p2_a_check_that_trades_doesnt_expose_the_king(key):
    kind, _, _ = texts(key)
    assert kind != "king_safety"


def test_p2_the_best_move_explains_the_tempo():
    _, _, best = texts("186/8")
    assert "f4 attacks the knight on e5" in best


def test_p3_the_piece_lost_is_named_and_the_bigger_miss_too():
    kind, text, _ = texts("221/50")
    assert "pawn on b2 undefended" not in text
    assert kind == "loses_material" and "a bishop and two pawns" in text and "Rxe3" in text
    assert "You also missed Bxf2, which wins a rook for a bishop" in text


def test_p4_the_capture_i_made_isnt_reported_as_missed():
    kind, text, _ = texts("196/39")
    assert kind == "missed_capture" and "undefended" not in text
    assert "You took on b2 with the rook" in text and "rook on a1" in text
    assert "wins a rook" in text and "Bxa1" in text  # the line runs through the gain


def test_p5_a_material_claim_the_eval_doesnt_back_is_dropped():
    kind, text, best = texts("190/14")
    assert kind != "missed_tactic" and "knight" not in text
    assert "wins" not in best and "Nxe4" not in best  # the shown line stops before its captures


def test_p6_the_best_move_isnt_credited_with_a_defence_it_doesnt_make():
    _, _, best = texts("35/16")
    assert "h3 defends" not in best
    assert "keeps your queen on d1 where it guards your pawn on c2" in best
    _, _, best = texts("180/35")
    assert "defends" not in best and "Rh2 attacks the rook on h1" in best


def test_p7_material_counts_from_after_my_recapture():
    kind, text, best = texts("125/49")
    assert kind == "hanging_piece"
    assert "Taking back with the rook leaves your knight on d5 undefended" in text
    assert "a knight and a pawn" in text
    assert "Nxe7 keeps your knight safe" in best


@pytest.mark.parametrize("key", ["199/107", "132/78"])
def test_p8_a_lost_long_mate_while_still_winning_is_an_inaccuracy(key):
    ctx = context(key)
    assert judge(ctx.before, ctx.after, CFG, thresholds_for(ROWS[key].rating, CFG, True))[0] == "inaccuracy"
    kind, text, _ = texts(key)
    assert kind == "missed_mate" and "Your move should still win." in text


def test_p8_judging_mates():
    assert judge(Eval(None, 8), Eval(962, None), CFG)[0] == "inaccuracy"  # long mate, still +9.6
    assert judge(Eval(None, 3), Eval(962, None), CFG)[0] == "mistake"  # a short mate is a tactic
    assert judge(Eval(None, 8), Eval(400, None), CFG)[0] == "blunder"  # long mate, but +4 left
    assert keeps_long_mate(Eval(None, 8), Eval(1500, None))  # a puzzle try that keeps +15
    assert not keeps_long_mate(Eval(None, 3), Eval(1500, None))
    assert not keeps_long_mate(Eval(None, 8), Eval(800, None))


def test_p9_an_understated_win_names_the_fork():
    kind, text, best = texts("22/31")
    assert kind == "missed_tactic"
    assert "fork" in text and "Nxc2+ attacks the king and the rook on a1" in text
    assert "wins a pawn" not in best and "Nb4 attacks the queen on d3" in best


def test_p10_the_line_runs_through_the_gain():
    kind, text, _ = texts("149/35")
    assert kind == "missed_capture" and "wins a rook" in text
    assert "Rxh5" in text


def test_p13_min_loss_scales_with_the_band():
    assert [thresholds_for(r, CFG, True).min_loss for r in (600, 1000, 1400, 1800, 2300)] == [2, 2, 2, 2, 1]
    assert thresholds_for(None, CFG, True).min_loss == 2  # standard thresholds


def test_p13_a_one_pawn_tactic_counts_at_2000_plus():
    board = chess.Board("r3k3/8/8/8/8/8/PP6/4K3 w - - 0 1")
    u = chess.Move.from_uci
    ctx = dict(
        board=board,
        move=u("e1d2"),
        best=u("e1d1"),
        best_line=[u("e1d1")],
        reply_line=[u("a8a3"), u("b2b3"), u("a3a2")],
        before=Eval(-300, None),
        after=Eval(-450, None),
    )
    kind, text = explain(MoveContext(**ctx, min_loss=thresholds_for(2300, CFG, True).min_loss))
    assert kind == "loses_material" and "a pawn" in text
    kind, _ = explain(MoveContext(**ctx, min_loss=thresholds_for(1000, CFG, True).min_loss))
    assert kind == "positional"


def test_a_line_cut_mid_exchange_is_finished():
    # The line stops after White's Qxd8+; the king takes back, so it is a trade, not a queen won.
    board = chess.Board("3qk3/8/8/8/8/8/8/3QK3 w - - 0 1")
    result = play_line(board, [chess.Move.from_uci("d1d8")], chess.WHITE)
    assert isinstance(result, LineResult) and result.net == 0
    assert result.won == [chess.QUEEN] and result.lost == [chess.QUEEN]
    # A capture nothing can answer stays a win.
    board = chess.Board("3q3k/8/8/8/8/8/8/3QK3 w - - 0 1")
    assert play_line(board, [chess.Move.from_uci("d1d8")], chess.WHITE).net == 9


def test_a_capture_that_loses_on_the_spot_ends_the_line():
    # A short line ending in Bxb7?? Rxb7 doesn't count the pawn or the bishop.
    board = chess.Board("1r4k1/1p6/8/8/8/8/6B1/6K1 w - - 0 1")
    result = play_line(board, [chess.Move.from_uci("g2b7")], chess.WHITE)
    assert result.net == 0 and result.won == [] and result.lost == []
