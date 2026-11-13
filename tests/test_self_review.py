"""v3 milestone 5: self-review of new games before the engine (session.py)."""

from dataclasses import replace

import chess
import pytest

from chessreview import db, progress, session
from chessreview.config import load_config


@pytest.fixture
def cfg():
    base = load_config()
    return replace(base, coach=replace(base.coach, self_review_games=2))


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "s.db")
    batch = c.execute("INSERT INTO imports (source, games_added) VALUES ('chess.com', 3)").lastrowid
    moves = ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6", "f3g5", "d7d5"]
    board = chess.Board()
    fens = []
    for uci in moves:
        fens.append(board.fen())
        board.push_uci(uci)
    fens.append(board.fen())
    # game 1: lost, my worst move ply 4; game 2: won cleanly (skipped); game 3: drawn, worst ply 6
    for gid, result, worst in ((1, "loss", 4), (2, "win", None), (3, "draw", 6)):
        c.execute(
            """INSERT INTO games (id, dedupe_key, white, black, my_color, my_result, date, moves_uci, num_plies,
                         pgn, batch_id, analysed_at, end_time)
                     VALUES (?, ?, 'me', 'opp', 'white', ?, '2026-10-10', ?, 8, '', ?, '2026-10-10', ?)""",
            (gid, f"k{gid}", result, " ".join(moves), batch, f"2026-10-1{gid} 12:00"),
        )
        for ply, fen in enumerate(fens):
            c.execute(
                "INSERT INTO positions (game_id, ply, fen, move_uci, move_san, best_uci) VALUES (?, ?, ?, ?, ?, ?)",
                (gid, ply, fen, moves[ply] if ply < 8 else None, None, "d2d4"),
            )
        for ply in (0, 2, 4, 6):
            judgement = "blunder" if ply == worst else ("mistake" if worst and ply == 2 else "good")
            drop = 0.5 if ply == worst else (0.2 if worst and ply == 2 else 0.0)
            c.execute(
                """INSERT INTO mistakes (game_id, ply, judgement, win_drop, is_mine, explanation)
                         VALUES (?, ?, ?, ?, 1, 'why')""",
                (gid, ply, judgement, drop),
            )
    c.commit()
    yield c
    c.close()


def test_games_to_review_skip_clean_wins_and_losses_come_first(conn, cfg):
    games = session.self_review_games(conn, cfg, [1])
    assert [g["game_id"] for g in games] == [1, 3]
    assert len(games[0]["moves"]) == 8 and "judgement" not in games[0]["moves"][0]  # no spoilers


def test_pick_is_scored_against_the_biggest_drop(conn, cfg):
    assert session.record_self_review(conn, 1, 4)["exact"]
    near = session.record_self_review(conn, 3, 4)  # actual 6: one move off still counts
    assert near["hit"] and not near["exact"] and near["ply"] == 6
    assert session.self_review_games(conn, cfg, [1]) == []  # each game is reviewed once
    assert session.self_review_rate(conn) == {"tries": 2, "hits": 2, "rate": 100.0}


def test_misses_and_unsure(conn):
    assert not session.record_self_review(conn, 1, 0)["hit"]
    assert not session.record_self_review(conn, 3, None)["hit"]
    rate = session.self_review_rate(conn, [1, 3])
    assert rate["tries"] == 2 and rate["hits"] == 0
    assert session.summary(conn, load_config(), [1])["self_review"]["tries"] == 2
    with pytest.raises(LookupError):
        session.record_self_review(conn, 2, 0)  # a clean win has no turning point


def test_progress_reports_self_review(conn, cfg):
    session.record_self_review(conn, 1, 4)
    assert progress.report(conn, cfg)["self_review"]["hits"] == 1
