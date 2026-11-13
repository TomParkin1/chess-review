"""Core rules without an engine: what an import keeps or skips, and where the mistake thresholds fall."""

import io

import chess.pgn
import pytest

from chessreview import db
from chessreview.classify import ADAPTIVE_BANDS, Eval, Thresholds, judge, thresholds_for
from chessreview.config import load_config
from chessreview.pgn_import import ImportStats, clocks_ms, dedupe_key, import_game, my_result

MOVES = "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 6. Re1 b5 7. Bb3 d6"  # 14 plies


def pgn(moves: str = MOVES, result: str = "1-0", **headers: str) -> chess.pgn.Game:
    tags = {"White": "sampleplayer", "Black": "someone", "Result": result, "Date": "2025.01.01"} | headers
    text = "".join(f'[{k} "{v}"]\n' for k, v in tags.items()) + f"\n{moves} {result}\n"
    game = chess.pgn.read_game(io.StringIO(text))
    assert game is not None
    return game


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "core.db")
    yield c
    c.close()


def imported(conn, game: chess.pgn.Game) -> ImportStats:
    stats = ImportStats()
    import_game(conn, game, load_config(), "test", stats)
    return stats


# ------------------------------------------------------------ PGN import


@pytest.mark.parametrize(
    "result, colour, expected",
    [
        ("1-0", "white", "win"),
        ("1-0", "black", "loss"),
        ("0-1", "black", "win"),
        ("0-1", "white", "loss"),
        ("1/2-1/2", "white", "draw"),
        ("*", "white", None),
    ],
)
def test_my_result_from_the_game_result_and_my_colour(result, colour, expected):
    assert my_result(result, colour) == expected


@pytest.mark.parametrize(
    "game, reason",
    [
        (pgn(White="a", Black="b"), "username not found in game"),
        (pgn(moves="1. e4 e5 2. Nf3"), "fewer than 10 plies"),
        (pgn(result="*"), "unfinished game"),
        (pgn(Variant="Chess960"), "variant (Chess960)"),
        (pgn(moves="1. e4 e5 2. Ke3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7"), "unparseable moves"),  # 2. Ke3 is illegal
    ],
)
def test_games_that_are_skipped_say_why(conn, game, reason):
    stats = imported(conn, game)
    assert stats.imported == 0 and stats.skipped == {reason: 1}


def test_my_colour_comes_from_the_headers_in_any_case(conn):
    stats = imported(conn, pgn(White="someone", Black="SAMPLEPLAYER", result="0-1"))
    row = conn.execute("SELECT my_color, my_result, num_plies FROM games").fetchone()
    assert stats.imported == 1 and tuple(row) == ("black", "win", 14)


def test_a_game_is_identified_by_its_link_else_by_players_date_and_moves():
    linked = pgn(Link="https://www.chess.com/game/live/1")
    assert dedupe_key(linked, "e2e4") == "https://www.chess.com/game/live/1"
    a, b = pgn(), pgn()
    assert dedupe_key(a, "e2e4 e7e5") == dedupe_key(b, "e2e4 e7e5") != dedupe_key(a, "e2e4 c7c5")
    assert dedupe_key(a, "e2e4 e7e5").startswith("sha1:")


def test_clocks_are_kept_in_milliseconds_with_gaps_marked():
    game = pgn(moves="1. e4 {[%clk 0:09:59.5]} e5 2. Nf3 {[%clk 0:09:58]} Nc6", result="*")
    assert clocks_ms(game) == "599500 - 598000 -"
    assert clocks_ms(pgn()) is None  # no clock comments at all


# ------------------------------------------------------------ mistake thresholds


@pytest.mark.parametrize("below, inaccuracy, mistake, blunder", [b for b in ADAPTIVE_BANDS if b[0] is not None])
def test_each_rating_band_starts_exactly_at_its_lower_bound(below, inaccuracy, mistake, blunder):
    cfg = load_config()
    inside, next_band = thresholds_for(below - 1, cfg, adaptive=True), thresholds_for(below, cfg, adaptive=True)
    assert (inside.inaccuracy, inside.mistake, inside.blunder) == (inaccuracy, mistake, blunder)
    assert next_band.inaccuracy < inside.inaccuracy  # stricter from the next band up
    assert inside.band.endswith(f"–{below - 1}")


def test_the_top_band_has_no_upper_bound():
    top = thresholds_for(10_000, load_config(), adaptive=True)
    assert (top.inaccuracy, top.mistake, top.blunder) == ADAPTIVE_BANDS[-1][1:]


@pytest.mark.parametrize(
    "drop, expected",
    [(0.099, "good"), (0.10, "inaccuracy"), (0.199, "inaccuracy"), (0.20, "mistake"), (0.30, "blunder")],
)
def test_a_drop_equal_to_a_threshold_reaches_that_judgement(drop, expected, monkeypatch):
    """Win-chance drops are compared with >=, so a drop right on the line counts."""
    monkeypatch.setattr(Eval, "win_chance", lambda self: self.cp / 1000)  # a known drop, no formula in the way
    before, after = Eval(0, None), Eval(-round(drop * 1000), None)
    assert judge(before, after, load_config(), Thresholds(0.10, 0.20, 0.30))[0] == expected
