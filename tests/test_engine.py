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
        cp, mate, best, pv, depth = analyse_position(engine, board, cfg)
    finally:
        engine.quit()
    assert mate == 1 and cp is None
    assert best == "a1a8"


def test_checkmated_position_needs_no_engine(cfg):
    board = chess.Board("R5k1/5ppp/8/8/8/8/5PPP/6K1 b - - 1 1")  # black is mated
    cp, mate, best, pv, depth = analyse_position(None, board, cfg)
    assert cp == MATED_CP and best is None


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
