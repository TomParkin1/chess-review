"""v3 milestone 4: the puzzle library (library.py) and its place in Today."""
import io
from datetime import date

import chess
import pytest
from compression import zstd

from chessreview import coach, db, library
from chessreview.config import load_config

HEADER = "PuzzleId,FEN,Moves,Rating,RatingDeviation,Popularity,NbPlays,Themes,GameUrl,OpeningTags,DailyDate\n"
# A fork for Black (Nxc2+ after White's Bd2), rated 900 and 2100, plus rows the filters drop.
FEN = "r1bqkb1r/pppp1ppp/8/4p3/1n2P3/2N5/PPPP1PPP/R1BQKBNR w KQkq - 0 5"
ROWS = [
    f"aaaaa,{FEN},c1d2 b4c2 e1e2 c2a1,900,75,95,5000,fork short,https://lichess.org/x,,",
    f"bbbbb,{FEN},c1d2 b4c2 e1e2 c2a1,2100,75,95,5000,fork short,https://lichess.org/y,,",
    f"ccccc,{FEN},c1d2 b4c2 e1e2 c2a1,1500,150,95,5000,fork,https://lichess.org/z,,",     # deviation too high
    f"ddddd,{FEN},c1d2 b4c2 e1e2 c2a1,1500,75,95,10,fork,https://lichess.org/w,,",        # too few plays
    f"eeeee,{FEN},c1d2 b4c2 e1e2 c2a1,1520,75,95,5000,mateIn1 oneMove,https://lichess.org/v,,",
]


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "l.db")
    data = zstd.compress((HEADER + "\n".join(ROWS) + "\n").encode())
    progress = library.FetchProgress()
    assert library.fetch(c, progress, opener=lambda req, timeout: FakeResponse(data)) == 3
    assert progress.state == "done" and progress.rows == 5 and progress.bytes_read == len(data)
    yield c
    c.close()


def test_sampler_keeps_at_most_per_cell():
    s = library.Sampler(per_cell=2)
    for i in range(10):
        s.offer([f"p{i}", FEN, "", "1210", "70", "90", "1000", "fork pin", "", ""])
    assert len(s.cells[(1200, "fork")]) == 2 and len(s.cells[(1200, "pin")]) == 2
    assert len(s.puzzles()) <= 4


def test_fetch_stores_the_sample(conn):
    ids = {r["id"] for r in conn.execute("SELECT id FROM puzzles")}
    assert ids == {"aaaaa", "bbbbb", "eeeee"}
    assert conn.execute("SELECT themes FROM puzzles WHERE id = 'aaaaa'").fetchone()[0] == " fork short "
    assert db.get_meta(conn, library.FETCHED_KEY)


def test_puzzle_json_starts_after_the_opponents_move(conn):
    p = library.puzzle_json(conn.execute("SELECT * FROM puzzles WHERE id = 'aaaaa'").fetchone())
    board = chess.Board(p["fen"])
    assert p["my_color"] == "black" and board.turn == chess.BLACK
    assert p["prev_move_uci"] == "c1d2" and p["solution"] == ["b4c2", "e1e2", "c2a1"]
    assert p["best_san"] == "Nxc2+" and p["playout_moves"] == 2 and p["hint"] == "Theme: fork."


def test_rating_moves_with_results_and_only_first_tries_count(conn):
    db.set_meta(conn, library.RATING_KEY, "1500")
    before, after = library.update_rating(conn, 1500, True)
    assert before == 1500 and after == 1520                      # K = 40, expected 0.5
    r = library.record(conn, "eeeee", "failed")
    assert r["rated"] and r["rating_after"] < r["rating_before"]
    assert conn.execute("SELECT COUNT(*) FROM cards WHERE kind = 'library' AND key = 'eeeee'").fetchone()[0] == 1
    again = library.record(conn, "eeeee", "solved")
    assert not again["rated"] and again["rating_after"] == r["rating_after"]
    library.record(conn, "bbbbb", "solved")                      # solved first time: no card
    assert conn.execute("SELECT COUNT(*) FROM cards WHERE key = 'bbbbb'").fetchone()[0] == 0


def test_pick_follows_the_rating(conn):
    """The same library serves a 900 and a 2100 player different puzzles."""
    assert library.pick(conn, 1, ["fork"], around=900) == ["aaaaa"]
    assert library.pick(conn, 1, ["fork"], around=2100) == ["bbbbb"]
    assert library.pick(conn, 5, ["mateIn1"], around=1500) == ["eeeee"]
    library.record(conn, "eeeee", "solved")
    assert library.pick(conn, 5, ["mateIn1"], around=1500) == []      # never the same puzzle twice


def test_themes_for_weaknesses():
    assert library.themes_for({"kind": "hanging_piece"}) == ["hangingPiece"]
    assert library.themes_for({"phase": "endgame"}) == ["endgame"]
    assert library.themes_for({"low_clock": "1"}) == []


def test_focus_block_mixes_library_puzzles(conn, monkeypatch):
    weakness = {"title": "Missed tactics", "detail": "", "train": {"kind": "fork"}}
    monkeypatch.setattr(coach.insights, "compute", lambda *a, **k: {"weaknesses": [weakness]})
    db.set_meta(conn, library.RATING_KEY, "950")
    plan = coach.today_plan(conn, load_config(), date.today())
    focus = next(b for b in plan["blocks"] if b["id"] == "focus")
    assert focus["items"] and all(item[0] == "lib" for item in focus["items"])   # no games here, library only
