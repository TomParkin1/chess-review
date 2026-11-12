import io
import threading
import time
from pathlib import Path

import chess.pgn
import pytest

from chessreview import db
from chessreview.config import load_config
from chessreview.pgn_import import (
    ImportStats,
    import_game,
    import_paths,
    opening_from_headers,
    time_class,
)

SAMPLE = Path(__file__).parent / "data" / "sample.pgn"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def cfg():
    return load_config()


def test_import_detects_colour_result_and_skips(conn, cfg):
    stats = import_paths(conn, [SAMPLE], cfg)
    assert stats.games_seen == 6
    assert stats.imported == 3
    assert stats.duplicates == 1
    assert sum(stats.skipped.values()) == 2  # username missing, too short

    rows = {r["link"]: r for r in conn.execute("SELECT * FROM games")}
    g1 = rows["https://www.chess.com/game/live/1001"]
    assert (g1["my_color"], g1["my_result"]) == ("white", "loss")
    g2 = rows["https://www.chess.com/game/live/1002"]
    assert (g2["my_color"], g2["my_result"]) == ("black", "win")  # case-insensitive name
    assert g2["opening_family"] == "Queen's Gambit Declined"


def test_reimport_is_all_duplicates(conn, cfg):
    import_paths(conn, [SAMPLE], cfg)
    stats = import_paths(conn, [SAMPLE], cfg)
    assert stats.imported == 0
    assert stats.duplicates == 4  # 3 imported games + the in-file duplicate


def test_game_without_link_deduped_by_hash(conn, cfg):
    import_paths(conn, [SAMPLE], cfg)
    keys = [r[0] for r in conn.execute("SELECT dedupe_key FROM games")]
    assert any(k.startswith("sha1:") for k in keys)


@pytest.mark.parametrize("tc,expected", [
    ("60", "bullet"), ("120+1", "bullet"), ("180", "blitz"), ("180+2", "blitz"),
    ("600", "rapid"), ("1/86400", "daily"), ("", "unknown"),
])
def test_time_class(tc, expected):
    assert time_class(tc) == expected


def test_opening_family():
    headers = {"ECOUrl": "https://www.chess.com/openings/Sicilian-Defense-Najdorf-Variation-6.Be3"}
    assert opening_from_headers(headers) == ("Sicilian Defense Najdorf Variation", "Sicilian Defense")


def test_opening_from_moves_without_headers(conn, cfg):
    import_paths(conn, [SAMPLE], cfg)
    row = conn.execute("SELECT eco, opening FROM games WHERE link IS NULL").fetchone()
    assert row["eco"] == "B90"
    assert row["opening"].startswith("Sicilian Defense: Najdorf Variation")


def test_concurrent_import_of_the_same_game_is_a_duplicate(tmp_path, cfg):
    """B8: two imports of the same game at once (a second tab, a sync during an upload). The second
    checked before the first committed, then hit UNIQUE(dedupe_key): HTTP 500 and an orphan batch."""
    a, b = db.connect(tmp_path / "race.db"), db.connect(tmp_path / "race.db")
    game = chess.pgn.read_game(io.StringIO(SAMPLE.read_text()))
    first, second, errors = ImportStats(), ImportStats(), []
    import_game(a, game, cfg, "a", first)            # stored but not committed: A holds the write lock

    def other():
        try:
            import_game(b, game, cfg, "b", second)   # its check can't see A's game; its insert waits
            b.commit()
        except Exception as exc:                      # before the fix: sqlite3.IntegrityError
            errors.append(exc)
    thread = threading.Thread(target=other)
    thread.start()
    time.sleep(0.3)
    a.commit()
    thread.join(10)
    assert errors == [] and first.imported == 1
    assert second.imported == 0 and second.duplicates == 1
    assert a.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 1
    a.close()
    b.close()


def test_failed_import_leaves_no_empty_batch(conn, cfg, tmp_path):
    with pytest.raises(FileNotFoundError):
        import_paths(conn, [tmp_path / "missing.pgn"], cfg)
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 0
