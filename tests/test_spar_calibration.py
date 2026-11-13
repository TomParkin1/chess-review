"""E5 calibration (opt-in, minutes): engine matches that back spar's levels below Stockfish's
UCI_Elo floor. Each level plays UCI_Elo 1320 from equal positions, both colours; weaker levels
must score less. Re-run when Stockfish changes:
  CHESSREVIEW_CALIBRATION=1 .venv/bin/pytest tests/test_spar_calibration.py
(The full calibration behind spar.TOP5_CALIBRATION / RANDOM_CALIBRATION was 100 games a level.)
"""

import os
import random
from concurrent.futures import ThreadPoolExecutor

import chess
import chess.engine
import pytest

from chessreview import spar
from chessreview.config import load_config
from chessreview.engine import find_stockfish

pytestmark = pytest.mark.skipif(
    not os.environ.get("CHESSREVIEW_CALIBRATION"), reason="slow: set CHESSREVIEW_CALIBRATION=1"
)
GAMES = 40
OPENINGS = [
    "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3",
    "rnbqkb1r/pp2pppp/3p1n2/8/3NP3/8/PPP2PPP/RNBQKB1R w KQkq - 1 5",
    "rnbqkbnr/pp3ppp/4p3/2ppP3/3P4/8/PPP2PPP/RNBQKBNR w KQkq - 0 4",
    "rnbqkb1r/ppp1pppp/5n2/3p4/3P4/5N2/PPP1PPPP/RNBQKB1R w KQkq - 2 3",
]


def play(engine: chess.engine.SimpleEngine, board: chess.Board, st: spar.Strength, rng: random.Random) -> chess.Move:
    engine.configure(st.options)
    limit = chess.engine.Limit(time=0.1)
    if st.swap and rng.random() < st.swap:
        if st.swap_kind == "random":
            return rng.choice(list(board.legal_moves))
        return rng.choice([i["pv"][0] for i in engine.analyse(board, limit, multipv=5) if i.get("pv")])
    return engine.play(board, limit).move


def game(elo: int, i: int) -> float:
    path = find_stockfish(load_config().engine.path)
    a, b = chess.engine.SimpleEngine.popen_uci(path), chess.engine.SimpleEngine.popen_uci(path)
    rng, me = random.Random(i), chess.WHITE if i % 2 == 0 else chess.BLACK
    board = chess.Board(OPENINGS[i // 2 % len(OPENINGS)])
    try:
        while not board.is_game_over(claim_draw=True) and len(board.move_stack) < 160:
            if board.turn == me:
                board.push(play(a, board, spar.strength(elo), rng))
            else:
                board.push(play(b, board, spar.strength(spar.ELO_FLOOR), rng))
    finally:
        a.quit()
        b.quit()
    outcome = board.outcome(claim_draw=True)
    return 0.5 if outcome is None or outcome.winner is None else float(outcome.winner == me)


def score(elo: int) -> float:
    with ThreadPoolExecutor(8) as pool:
        return sum(pool.map(lambda i: game(elo, i), range(GAMES))) / GAMES


def test_weaker_levels_score_less_against_1320():
    scores = [score(elo) for elo in (1320, 1000, 700)]
    assert scores[0] > scores[1] > scores[2], scores
    assert 0.3 <= scores[0] <= 0.7  # 1320 against itself: about even
