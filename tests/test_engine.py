import shutil
from dataclasses import replace
from pathlib import Path

import chess
import pytest

from chessreview import db
from chessreview.config import load_config
from chessreview.engine import MATED_CP, AnalysisJob, analyse_position, open_engine
from chessreview.pgn_import import import_paths

SAMPLE = Path(__file__).parent / "data" / "sample.pgn"


@pytest.fixture
def cfg(tmp_path):
    base = load_config()
    if not shutil.which(base.engine.path):
        pytest.skip("Stockfish not installed")
    fast = replace(base.engine, time_per_position=0.05, workers=2, threads=1)
    return replace(base, engine=fast, db_path=tmp_path / "test.db")


def test_finds_mate_in_one(cfg):
    board = chess.Board("6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1")  # Ra8#
    engine = open_engine(cfg)
    try:
        e = analyse_position(engine, board, cfg)
    finally:
        engine.quit()
    assert e.score_mate == 1 and e.score_cp is None
    assert e.best_uci == "a1a8"
    assert e.second_uci and e.second_uci != "a1a8"  # MultiPV 2: the runner-up move


def test_checkmated_position_needs_no_engine(cfg):
    board = chess.Board("R5k1/5ppp/8/8/8/8/5PPP/6K1 b - - 1 1")  # black is mated
    e = analyse_position(None, board, cfg)
    assert e.score_cp == MATED_CP and e.best_uci is None


def test_job_analyses_each_game_once(cfg):
    conn = db.connect(cfg.db_path)
    import_paths(conn, [SAMPLE], cfg)

    job = AnalysisJob(cfg)
    job.start()
    job.wait()
    assert job.progress.state == "done"
    assert job.progress.games_done == 3
    n_positions = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    expected = conn.execute("SELECT SUM(num_plies + 1) FROM games").fetchone()[0]
    assert n_positions == expected

    again = AnalysisJob(cfg)
    again.start()
    again.wait()
    assert again.progress.games_total == 0  # everything cached
    conn.close()


def test_both_sides_classified_with_marks_and_accuracy(cfg):
    from chessreview.classify import ensure_classified
    from chessreview.report import MARKS

    conn = db.connect(cfg.db_path)
    import_paths(conn, [SAMPLE], cfg)
    job = AnalysisJob(cfg)
    job.start()
    job.wait()
    ensure_classified(conn, cfg)

    rows = conn.execute("SELECT * FROM mistakes").fetchall()
    total_moves = conn.execute("SELECT SUM(num_plies) FROM games").fetchone()[0]
    assert len(rows) == total_moves                      # every move, both sides
    assert {r["is_mine"] for r in rows} == {0, 1}
    assert all(r["mark"] in MARKS and 0 <= r["accuracy"] <= 100 for r in rows)
    assert any(r["mark"] == "book" for r in rows)        # all sample games start in theory
    assert all(r["explanation"] is None for r in rows if not r["is_mine"])
    for g in conn.execute("SELECT * FROM games"):
        assert g["analysis_version"] == 2
        assert 0 <= g["accuracy_white"] <= 100 and 0 <= g["accuracy_black"] <= 100
    positions = conn.execute("SELECT * FROM positions WHERE best_uci IS NOT NULL").fetchall()
    assert any(p["second_uci"] for p in positions)       # MultiPV 2 stored
    conn.close()
