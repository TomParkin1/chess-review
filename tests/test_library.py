"""v3 milestone 4: the puzzle library (library.py) and its place in Today."""

import io
from compression import zstd
from datetime import date, timedelta

import chess
import pytest

from chessreview import backup, coach, db, library
from chessreview.config import load_config

HEADER = "PuzzleId,FEN,Moves,Rating,RatingDeviation,Popularity,NbPlays,Themes,GameUrl,OpeningTags,DailyDate\n"
# A fork for Black (Nxc2+ after White's Bd2), rated 900 and 2100, plus rows the filters drop.
FEN = "r1bqkb1r/pppp1ppp/8/4p3/1n2P3/2N5/PPPP1PPP/R1BQKBNR w KQkq - 0 5"
ROWS = [
    f"aaaaa,{FEN},c1d2 b4c2 e1e2 c2a1,900,75,95,5000,fork short,https://lichess.org/x,,",
    f"bbbbb,{FEN},c1d2 b4c2 e1e2 c2a1,2100,75,95,5000,fork short,https://lichess.org/y,,",
    f"ccccc,{FEN},c1d2 b4c2 e1e2 c2a1,1500,150,95,5000,fork,https://lichess.org/z,,",  # deviation too high
    f"ddddd,{FEN},c1d2 b4c2 e1e2 c2a1,1500,75,95,10,fork,https://lichess.org/w,,",  # too few plays
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


def test_a_sample_from_before_new_themes_is_outdated(conn):
    """E2: Settings suggests an update when the sample was drawn before the endgame types existed."""
    assert not library.outdated(conn)
    db.set_meta(conn, library.THEMES_KEY, "fork pin")
    assert library.outdated(conn)


def test_endgame_puzzles_rotate_through_the_types(conn):
    from datetime import date

    rows = (
        [(f"r{i}", " endgame rookEndgame ") for i in range(3)]
        + [(f"p{i}", " endgame pawnEndgame ") for i in range(3)]
        + [("q0", " endgame queenEndgame ")]
    )
    conn.executemany(
        """INSERT INTO puzzles (id, fen, moves, rating, deviation, popularity, plays, themes, game_url)
                        VALUES (?, ?, 'c1d2 b4c2', 1500, 80, 90, 1000, ?, '')""",
        [(i, FEN, t) for i, t in rows],
    )
    conn.commit()
    day = next(
        d for d in (date(2026, 11, n) for n in range(1, 30)) if d.toordinal() % len(library.ENDGAME_ROTATION) == 0
    )  # the rotation starts at rook
    ids = library.pick_endgames(conn, 4, day)
    assert [i[0] for i in ids] == ["r", "p", "r", "q"] and len(set(ids)) == 4
    # A type with nothing left falls back to any endgame puzzle; nothing excluded comes back.
    more = library.pick_endgames(conn, 5, day, exclude=set(ids))
    assert len(more) == 3 and not set(more) & set(ids)
    assert library.endgame_types(conn, ids) == ["Rook endgame", "Pawn endgame", "Queen endgame"]


def test_puzzle_json_starts_after_the_opponents_move(conn):
    p = library.puzzle_json(conn.execute("SELECT * FROM puzzles WHERE id = 'aaaaa'").fetchone())
    board = chess.Board(p["fen"])
    assert p["my_color"] == "black" and board.turn == chess.BLACK
    assert p["prev_move_uci"] == "c1d2" and p["solution"] == ["b4c2", "e1e2", "c2a1"]
    assert p["best_san"] == "Nxc2+" and p["playout_moves"] == 2 and p["hint"] == "Theme: fork."


def test_rating_moves_with_results_and_only_first_tries_count(conn):
    db.set_meta(conn, library.RATING_KEY, "1500")
    before, after = library.update_rating(conn, 1500, 1.0)
    assert before == 1500 and after == 1560  # first puzzle: K = 120, expected 0.5
    r = library.record(conn, "eeeee", "failed")
    assert r["rated"] and r["rating_after"] < r["rating_before"]
    assert conn.execute("SELECT COUNT(*) FROM cards WHERE kind = 'library' AND key = 'eeeee'").fetchone()[0] == 1
    again = library.record(conn, "eeeee", "solved")
    assert not again["rated"] and again["rating_after"] == r["rating_after"]
    library.record(conn, "bbbbb", "solved")  # solved first time: no card
    assert conn.execute("SELECT COUNT(*) FROM cards WHERE key = 'bbbbb'").fetchone()[0] == 0


def test_hints_cost_half_and_the_first_puzzles_count_most(conn):
    """Section 3: a hint used to cost as much as a failure, and a seed far below my puzzle
    strength took about 15 clean solves to correct."""
    assert library.k_factor(0) == 120 and 39 < library.k_factor(5) < 41 and library.k_factor(10) == 37.6
    assert library.k_factor(100) == library.k_factor(500) == 16
    db.set_meta(conn, library.RATING_KEY, "1500")
    db.set_meta(conn, library.COUNT_KEY, "50")  # past the provisional puzzles: K = 28
    with conn:
        conn.execute("UPDATE puzzles SET rating = 1500")
    helped = library.record(conn, "eeeee", "solved_with_help")
    assert helped["rating_after"] == helped["rating_before"] == 1500  # half a win at equal ratings
    failed = library.record(conn, "bbbbb", "failed")
    assert failed["rating_after"] == 1486  # a failure costs K/2
    skipped = library.record(conn, "aaaaa", "skipped")
    assert skipped["rating_after"] < failed["rating_after"]  # a skip counts as a loss too


def test_pick_follows_the_rating(conn):
    """The same library serves a 900 and a 2100 player different puzzles."""
    assert library.pick(conn, 1, ["fork"], around=900) == ["aaaaa"]
    assert library.pick(conn, 1, ["fork"], around=2100) == ["bbbbb"]
    assert library.pick(conn, 5, ["mateIn1"], around=1500) == ["eeeee"]
    library.record(conn, "eeeee", "solved")
    assert library.pick(conn, 5, ["mateIn1"], around=1500) == []  # never the same puzzle twice


def test_themes_for_weaknesses():
    assert library.themes_for({"kind": "hanging_piece"}) == ["hangingPiece"]
    assert library.themes_for({"phase": "endgame"}) == library.ENDGAME_THEMES
    assert library.themes_for({"low_clock": "1"}) == []


def test_focus_block_mixes_library_puzzles(conn, monkeypatch):
    weakness = {"title": "Missed tactics", "detail": "", "train": {"kind": "fork"}}
    monkeypatch.setattr(coach.insights, "compute", lambda *a, **k: {"weaknesses": [weakness]})
    db.set_meta(conn, library.RATING_KEY, "950")
    plan = coach.today_plan(conn, load_config(), date.today())
    focus = next(b for b in plan["blocks"] if b["id"] == "focus")
    assert focus["items"] and all(item[0] == "lib" for item in focus["items"])  # no games here, library only


# ------------------------------------------------------------ M1: a refresh can't orphan cards (B2)

NEW_SAMPLE = [  # next month's file: a different sample, without the puzzles above
    f"fffff,{FEN},c1d2 b4c2 e1e2 c2a1,950,75,95,5000,fork short,https://lichess.org/u,,",
    f"ggggg,{FEN},c1d2 b4c2 e1e2 c2a1,1550,75,95,5000,mateIn1 oneMove,https://lichess.org/t,,",
]


def refetch(conn, rows):
    data = zstd.compress((HEADER + "\n".join(rows) + "\n").encode())
    return library.fetch(conn, opener=lambda req, timeout: FakeResponse(data))


def test_refresh_keeps_puzzles_with_cards_and_today_can_finish(conn, monkeypatch):
    """B2: a refresh deleted every puzzle, so a failed puzzle's card pointed at nothing; Today kept
    scheduling it (0 / 1, Start did nothing) and the plan could never be completed."""
    monkeypatch.setattr(coach.insights, "compute", lambda *a, **k: {"weaknesses": []})
    library.record(conn, "aaaaa", "failed")  # a card, due tomorrow
    library.record(conn, "eeeee", "solved")  # tried: kept for its attempt history
    assert refetch(conn, NEW_SAMPLE) == 2
    ids = {r["id"] for r in conn.execute("SELECT id FROM puzzles")}
    assert ids == {"aaaaa", "eeeee", "fffff", "ggggg"}  # bbbbb (never tried) is replaced
    assert list(backup.folder_of(conn).glob("chess-before-library-*.db"))  # backed up first

    tomorrow = date.today() + timedelta(days=1)
    plan = coach.today_plan(conn, load_config(), tomorrow)
    reviews = next(b for b in plan["blocks"] if b["id"] == "reviews")
    assert reviews["items"] == [["lib", "aaaaa", "due"]] and reviews["count"] == 1
    conn.execute(
        "INSERT INTO puzzle_attempts (puzzle_id, result, at) VALUES ('aaaaa', 'solved', ?)",
        (f"{tomorrow.isoformat()} 12:00:00",),
    )
    conn.commit()
    assert coach.today_plan(conn, load_config(), tomorrow)["complete"]


def test_a_card_whose_puzzle_is_gone_is_not_scheduled(conn, monkeypatch):
    """Libraries refreshed before the fix may already hold such cards: they are skipped."""
    monkeypatch.setattr(coach.insights, "compute", lambda *a, **k: {"weaknesses": []})
    library.record(conn, "aaaaa", "failed")
    tomorrow = date.today() + timedelta(days=1)
    assert coach.today_plan(conn, load_config(), tomorrow)["total"] == 1
    conn.execute("DELETE FROM puzzles WHERE id = 'aaaaa'")
    conn.commit()
    plan = coach.today_plan(conn, load_config(), tomorrow)  # the stored plan lost an item: rebuilt
    assert plan["total"] == 0 and plan["complete"]
    assert coach.today_plan(conn, load_config(), tomorrow + timedelta(days=1))["total"] == 0
