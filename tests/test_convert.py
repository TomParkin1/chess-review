"""v3 milestone 3: converting won positions and essential endgames (convert.py)."""
from dataclasses import replace

import chess
import pytest

from chessreview import convert, db, srs
from chessreview.classify import Eval
from chessreview.convert import Task, state


def task(goal="win", finish="resign", kind="convert", color="white", max_plies=120):
    return Task(key="x/1", kind=kind, fen=chess.STARTING_FEN, color=color, goal=goal, finish=finish,
                title="t", max_plies=max_plies)


def test_endgames_are_valid_and_my_side_moves_first():
    games = convert.endgames()
    assert len({t.key for t in games}) == len(games) >= 12
    for t in games:
        board = chess.Board(t.fen)
        assert board.is_valid(), t.key
        assert t.goal in ("win", "draw") and t.finish in ("mate", "resign")
        assert (board.turn == chess.WHITE) == (t.color == "white")
        assert t.level in convert.LEVELS


def test_levels_unlock_in_order(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    overview = convert.endgame_overview(conn)
    assert {e["level"] for e in overview if e["unlocked"]} == {1}
    for e in overview:
        if e["level"] == 1:
            srs.record_key(conn, "endgame", e["key"].split("/")[1], "solved")
    assert {e["level"] for e in convert.endgame_overview(conn) if e["unlocked"]} == {1, 2}


def test_checkmate_and_draws():
    mated = chess.Board("R5k1/5ppp/8/8/8/8/5PPP/6K1 b - - 1 1")         # black is mated, I am white
    assert state(task(), mated, None, 0, 1000).status == "won"
    stalemate = chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
    assert state(task(), stalemate, None, 0, 1000).status == "failed"
    assert state(task(goal="draw"), stalemate, None, 0, 1000).status == "won"


def test_stockfish_resigns():
    b = chess.Board()
    assert state(task(), b, Eval(None, 4), 0, 1000).status == "won"                 # mate in 4
    s = state(task(), b, Eval(1200, None), 0, 1000)
    assert s.status == "playing" and s.streak == 1
    assert state(task(), b, Eval(1200, None), 2, 1000).status == "won"              # third move at +12
    assert state(task(finish="mate", kind="endgame"), b, Eval(None, 4), 0, 1000).status == "playing"


def test_slipping_advantage_scales_with_rating():
    """+1.8 counts as having let it slip at 900 (bar +5), not yet at 2000 (bar +3)."""
    b = chess.Board()
    assert state(task(), b, Eval(180, None), 0, 900).status == "failed"
    assert state(task(), b, Eval(180, None), 0, 2000).status == "playing"
    # Endgames are judged by the result only (the eval of a B+N mate can look modest).
    assert state(task(kind="endgame", finish="mate"), b, Eval(180, None), 0, 900).status == "playing"


def test_draw_goal_holds_or_fails():
    b = chess.Board()
    for _ in range(2 * convert.HOLD_MOVES):
        b.push(next(iter(b.legal_moves)))
    assert state(task(goal="draw"), b, Eval(0, None), 0, 1000).status == "won"
    assert state(task(goal="draw"), chess.Board(), Eval(-400, None), 0, 1000).status == "failed"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "c.db")
    for gid, result, wins in ((1, "loss", [0.1, 0.75, 0.8, 0.2]), (2, "win", [0.75, 0.8, 0.9, 0.9]),
                              (3, "draw", [0.1, 0.75, 0.3, 0.75])):
        c.execute("""INSERT INTO games (id, dedupe_key, white, black, white_elo, black_elo, my_color, my_result, date,
                         moves_uci, num_plies, pgn, analysed_at, end_time, time_class, rated)
                     VALUES (?, ?, 'me', 'opp', 1000, 1000, 'white', ?, '2026-10-01', 'e2e4', 8, '', '2026-10-01',
                             ?, 'rapid', 1)""", (gid, f"k{gid}", result, f"2026-10-0{gid} 12:00"))
        for i, w in enumerate(wins):
            c.execute("""INSERT INTO mistakes (game_id, ply, judgement, win_before, win_drop, is_mine)
                         VALUES (?, ?, 'good', ?, 0, 1)""", (gid, 2 * i, w))
            c.execute("INSERT INTO positions (game_id, ply, fen, score_cp) VALUES (?, ?, ?, 600)",
                      (gid, 2 * i, chess.STARTING_FEN))
    c.commit()
    yield c
    c.close()


def test_convert_positions(conn):
    keys = [p["key"] for p in convert.convert_positions(conn)]
    # winning bar at 1000: +5 = 0.726 win chance; game 3 never holds it for two moves
    assert keys == ["convert/1:2", "convert/2:0"]           # the lost game first
    assert convert.next_convert(conn) == "convert/1:2"
    srs.record_key(conn, "convert", "1:2", "failed")
    assert convert.next_convert(conn) == "convert/2:0"      # tried today: due again tomorrow
    t = convert.task_for(conn, "convert/1:2")
    assert t.goal == "win" and t.color == "white" and "lost" in t.subtitle
