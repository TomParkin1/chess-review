"""Finish M4 acceptance: every essential endgame played out through /api/play/move with real Stockfish.

  strong  Stockfish plays my side (the "Watch Stockfish" path: /api/play/hint, then /api/play/move):
          every endgame must still be won or held.
  safe    three merely safe moves (they keep the result but make the least progress): none may count
          as won yet (P11: no endgame passes without reaching its milestone).

Slow (minutes), so opt-in: CHESSREVIEW_PLAYOUTS=1 .venv/bin/pytest tests/test_endgame_playouts.py
"""
import os
import shutil
from dataclasses import replace

import chess
import chess.engine
import pytest

from chessreview import convert
from chessreview.config import load_config
from chessreview.web import create_app

pytestmark = pytest.mark.skipif(not os.environ.get("CHESSREVIEW_PLAYOUTS"),
                                reason="slow: set CHESSREVIEW_PLAYOUTS=1")
KEYS = [t.key for t in convert.endgames()]
SAFE_MOVES = 3
SAFE_DEPTH = 14


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    base = load_config()
    if not shutil.which(base.engine.path):
        pytest.skip("Stockfish not installed")
    app = create_app(replace(base, db_path=tmp_path_factory.mktemp("playouts") / "p.db"))  # real live_time
    app.testing = True
    yield app.test_client()
    app.extensions["live"].close()


@pytest.fixture(scope="module")
def picker():
    """A separate Stockfish that ranks every legal move to pick merely safe ones."""
    engine = chess.engine.SimpleEngine.popen_uci(load_config().engine.path)
    engine.configure({"Threads": 4})
    yield engine
    engine.quit()


def play(client, key: str, moves: list[str], uci: str, streak: int) -> dict:
    r = client.post("/api/play/move", json={"key": key, "moves": moves, "uci": uci, "streak": streak})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def strong_playout(client, key: str) -> dict:
    task = client.get(f"/api/convert/task?key={key}").get_json()
    moves, streak = [], 0
    for _ in range(task["max_plies"] // 2 + 2):
        hint = client.post("/api/play/hint", json={"key": key, "moves": moves}).get_json()
        r = play(client, key, moves, hint["uci"], streak)
        moves += [hint["uci"]] + ([r["reply_uci"]] if r.get("reply_uci") else [])
        streak = r["streak"]
        if r["status"] != "playing":
            return {**r, "my_moves": (len(moves) + 1) // 2}
    raise AssertionError(f"{key}: still playing after max_plies")


def safe_move(engine: chess.engine.SimpleEngine, board: chess.Board, goal: str) -> chess.Move:
    """Not the best move, but one that keeps the result (still decisive for a win, not losing for a
    draw), and of those the one that makes the least progress (lowest eval, longest mate).
    One search per move: MultiPV over every move ignored its time limit for 16 minutes in K+R+P v K."""
    me = board.turn
    ranked = []
    for move in board.legal_moves:
        board.push(move)
        ranked.append((move, engine.analyse(board, chess.engine.Limit(depth=SAFE_DEPTH, time=0.5))["score"].pov(me)))
        board.pop()
    ranked.sort(key=lambda ms: ms[1].score(mate_score=100_000), reverse=True)
    best = ranked[0][1].score(mate_score=100_000)

    def safe(score: chess.engine.Score) -> bool:
        if score.is_mate():
            return score.mate() > 0 if goal == "win" else False
        return score.score() >= max(150, min(best, 2000) // 2) if goal == "win" else score.score() >= -100

    options = [(m, s) for m, s in ranked[1:] if safe(s)] or ranked[:1]
    return min(options, key=lambda ms: ms[1].score(mate_score=100_000))[0]


def safe_playout(client, engine, key: str, n: int = SAFE_MOVES) -> list[dict]:
    task = client.get(f"/api/convert/task?key={key}").get_json()
    board = chess.Board(task["fen"])
    moves, streak, out = [], 0, []
    for _ in range(n):
        uci = safe_move(engine, board, task["goal"]).uci()
        r = play(client, key, moves, uci, streak)
        out.append(r)
        moves += [uci] + ([r["reply_uci"]] if r.get("reply_uci") else [])
        board = chess.Board(task["fen"])
        for m in moves:
            board.push_uci(m)
        streak = r["streak"]
        if r["status"] != "playing":
            break
    return out


@pytest.mark.parametrize("key", KEYS)
def test_strong_moves_win_or_hold(client, key):
    r = strong_playout(client, key)
    assert r["status"] == "won", f"{key}: {r['reason']} after {r['my_moves']} moves"


@pytest.mark.parametrize("key", KEYS)
def test_three_safe_moves_never_finish(client, picker, key):
    results = safe_playout(client, picker, key)
    assert [r["status"] for r in results] == ["playing"] * SAFE_MOVES, \
        f"{key}: {[(r['san'], r['status'], r['reason']) for r in results]}"
