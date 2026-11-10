"""Milestone 1 of v2: migrations, cross-source dedupe, chess.com sync, clocks, marks, accuracy."""
import io
import json
import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path

import chess
import chess.pgn
import pytest

from chessreview import chesscom, db, report
from chessreview.config import load_config
from chessreview.pgn_import import ImportStats, import_game, import_pgn_text

DATA = Path(__file__).parent / "data"
MONTH = json.loads((DATA / "chesscom_2026_10.json").read_text())


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "v2.db")
    yield c
    c.close()


def fake_fetch(url: str) -> dict:
    if url.endswith("/archives"):
        base = "https://api.chess.com/pub/player/sampleplayer/games"
        return {"archives": [f"{base}/2026/09", f"{base}/2026/10"]}
    if url.endswith("/2026/10"):
        return MONTH
    return {"games": []}


# ------------------------------------------------------------------ migrations

def test_v1_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(db.SCHEMA)  # the original layout, user_version 0
    old.execute("""INSERT INTO games (dedupe_key, white, black, my_color, my_result, date,
                       moves_uci, num_plies, pgn, analysed_at)
                   VALUES ('sha1:x', 'a', 'b', 'white', 'win', '2026-01-02', 'e2e4 e7e5', 2, '', '2026-01-03')""")
    old.commit()
    old.close()

    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    g = conn.execute("SELECT * FROM games").fetchone()
    assert g["content_hash"] == db.content_hash("a", "b", "2026-01-02", "e2e4 e7e5")
    assert g["analysis_version"] == 1 and g["end_time"] == "2026-01-02 00:00:00"
    batch = conn.execute("SELECT * FROM imports").fetchone()
    assert g["batch_id"] == batch["id"] and batch["session_done_at"]  # old games: no "new" session
    conn.close()
    db.connect(path).close()  # migrating twice is a no-op


# ------------------------------------------------------------------ dedupe across sources

def _without_link_and_clocks(pgn: str) -> str:
    """What the same game looks like in a chess.com bulk download."""
    game = chess.pgn.read_game(io.StringIO(pgn))
    for key in ("Link", "ECO", "ECOUrl", "UTCDate", "UTCTime", "EndDate", "StartTime"):
        game.headers.pop(key, None)
    exporter = chess.pgn.StringExporter(headers=True, variations=False, comments=False)
    return game.accept(exporter)


def test_uploaded_game_is_not_duplicated_by_sync(conn, cfg):
    bulk = "\n\n".join(_without_link_and_clocks(g["pgn"]) for g in MONTH["games"])
    assert import_pgn_text(conn, bulk, cfg).imported == 3

    result = chesscom.sync(conn, cfg, fetch=fake_fetch)
    assert result.stats.imported == 0 and result.stats.duplicates == 3
    assert result.batch_id is None  # an empty batch is not kept
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 0


def test_synced_game_is_not_duplicated_by_upload(conn, cfg):
    assert chesscom.sync(conn, cfg, fetch=fake_fetch).stats.imported == 3
    bulk = "\n\n".join(_without_link_and_clocks(g["pgn"]) for g in MONTH["games"])
    stats = import_pgn_text(conn, bulk, cfg)
    assert stats.imported == 0 and stats.duplicates == 3


# ------------------------------------------------------------------ sync

def test_sync_imports_with_clocks_links_and_batch(conn, cfg):
    result = chesscom.sync(conn, cfg, fetch=fake_fetch)
    assert result.months == ["2026/09", "2026/10"] and result.games_added == 3
    rows = conn.execute("SELECT * FROM games ORDER BY end_time").fetchall()
    assert all(r["source"] == "chess.com" and r["link"] and r["uuid"] for r in rows)
    assert all(r["batch_id"] == result.batch_id for r in rows)
    assert rows[0]["end_time"] < rows[-1]["end_time"] and rows[0]["end_time"].endswith(":42")
    clocks = rows[0]["clocks_ms"].split()
    assert len(clocks) == rows[0]["num_plies"] and clocks[0] == "599900"  # 0:09:59.9
    assert db.get_meta(conn, chesscom.LAST_MONTH_KEY) == "2026/10"
    batch = conn.execute("SELECT * FROM imports WHERE id = ?", (result.batch_id,)).fetchone()
    assert batch["session_done_at"]   # first sync = history import: no new-games session


def test_next_sync_only_fetches_from_last_month(conn, cfg):
    chesscom.sync(conn, cfg, fetch=fake_fetch)
    fetched = []
    chesscom.sync(conn, cfg, fetch=lambda url: fetched.append(url) or fake_fetch(url))
    assert [u.rsplit("/", 2)[-2:] for u in fetched[1:]] == [["2026", "10"]]


def test_months_to_fetch_first_sync_choices():
    archives = [f"https://x/games/2026/{m:02d}" for m in range(2, 11)]
    today = date(2026, 10, 16)
    assert len(chesscom.months_to_fetch(archives, None, "all", today)) == 9
    assert chesscom.months_to_fetch(archives, None, "3m", today)[0].endswith("2026/08")
    assert chesscom.months_to_fetch(archives, None, "1m", today) == [archives[-1]]
    assert chesscom.months_to_fetch(archives, "2026/09", "all", today) == archives[-2:]


def test_variants_are_skipped(conn, cfg):
    month = {"games": [dict(MONTH["games"][0], rules="chess960")]}
    result = chesscom.sync(conn, cfg, fetch=lambda url: {"archives": ["https://x/games/2026/10"]}
                           if url.endswith("archives") else month)
    assert result.stats.imported == 0 and "variant (chess960)" in result.stats.skipped


# ------------------------------------------------------------------ time use

def test_time_spent_from_clocks():
    # 10+5: white 600 -> 598 (used 7s incl. +5), black 600 -> 590 (15s), white 598 -> 600 (3s)
    assert report.time_spent("598000 590000 600000", "600+5") == [7000, 15000, 3000]
    assert report.time_spent("598000 - 590000", "600") == [2000, None, 8000]  # black clock unknown
    assert report.time_spent(None, "600") == []


# ------------------------------------------------------------------ marks

def mark(**kw):
    base = dict(judgement="good", drop_pct=0.0, before_pct=55, after_pct=55, is_best=False,
                forced=False, book=False, second_gap_pct=None, prev_opponent_judgement=None,
                sacrifice=False)
    base.update(kw)
    return report.mark_move(report.MarkInput(**base))


@pytest.mark.parametrize("kw,expected", [
    (dict(book=True, is_best=True), "book"),
    (dict(forced=True), "forced"),
    (dict(is_best=True), "best"),
    (dict(drop_pct=0.4), "best"),
    (dict(drop_pct=1.5), "excellent"),
    (dict(drop_pct=4), "good"),
    (dict(is_best=True, second_gap_pct=20), "great"),
    (dict(is_best=True, second_gap_pct=20, before_pct=97), "best"),   # already decided
    (dict(is_best=True, sacrifice=True), "brilliant"),
    (dict(is_best=True, sacrifice=True, before_pct=95), "best"),     # already completely winning
    (dict(judgement="mistake", drop_pct=12), "mistake"),
    (dict(judgement="mistake", drop_pct=12, before_pct=75, prev_opponent_judgement="blunder"), "miss"),
    (dict(judgement="inaccuracy", drop_pct=6, before_pct=75, prev_opponent_judgement="blunder"), "inaccuracy"),
    (dict(judgement="mistake", drop_pct=12, before_pct=50, prev_opponent_judgement="mistake"), "mistake"),
    (dict(is_best=True, second_gap_pct=20, is_recapture=True), "best"),   # obvious recapture
    (dict(judgement="blunder", drop_pct=40, after_pct=20, prev_opponent_judgement="blunder"), "blunder"),
])
def test_mark_rules(kw, expected):
    assert mark(**kw) == expected


def test_sacrifice_detection():
    board = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 2 3")
    assert report.is_sacrifice(board, chess.Move.from_uci("c4f7"))      # Bxf7+ gives a bishop for a pawn
    assert not report.is_sacrifice(board, chess.Move.from_uci("b1c3"))  # quiet developing move
    assert report.is_sacrifice(board, chess.Move.from_uci("f3e5"))      # Nxe5: knight en prise to Nc6
    # Taking a free queen is not a sacrifice even if the knight can be recaptured.
    free_queen = chess.Board("4k3/8/8/3q4/8/4N3/8/4K3 w - - 0 1")
    assert not report.is_sacrifice(free_queen, chess.Move.from_uci("e3d5"))


# ------------------------------------------------------------------ accuracy

def test_move_accuracy_values():
    assert report.move_accuracy(60, 60) == 100
    assert report.move_accuracy(60, 70) == 100
    assert 80 < report.move_accuracy(60, 55) < 85     # small slip
    assert report.move_accuracy(80, 10) < 10          # blunder


def test_game_accuracy_perfect_and_blunder():
    pcts = [50.0] * 21
    perfect = report.game_accuracy(pcts, [100.0] * 20)
    assert perfect == (100.0, 100.0)
    accs = [100.0] * 20
    accs[10] = 5.0                                      # one white blunder
    white, black = report.game_accuracy(pcts, accs)
    assert white < black == 100.0


def test_later_sync_with_new_games_opens_a_session(conn, cfg):
    cfg = replace(cfg, coach=replace(cfg.coach, history_days=3650))   # the test games count as recent
    first_two = {"games": MONTH["games"][:2]}
    chesscom.sync(conn, cfg, fetch=lambda u: fake_fetch(u) if u.endswith("archives") else
                  (first_two if u.endswith("/2026/10") else {"games": []}))
    later = chesscom.sync(conn, cfg, fetch=fake_fetch)       # the third game is new
    assert later.games_added == 1
    batch = conn.execute("SELECT * FROM imports WHERE id = ?", (later.batch_id,)).fetchone()
    assert batch["session_done_at"] is None


def test_first_sync_that_fails_partway_is_still_history(conn, cfg):
    """E1: the "history" label was skipped when the first sync raised, but its months were saved,
    so its games became a new-games session."""
    def failing(url):
        if url.endswith("/2026/10"):
            raise OSError("network down")
        return fake_fetch(url) if not url.endswith("/2026/09") else MONTH
    with pytest.raises(OSError):
        chesscom.sync(conn, cfg, fetch=failing)
    row = conn.execute("SELECT label, session_done_at FROM imports").fetchone()
    assert row["label"] == "chess.com history import" and row["session_done_at"] is not None


def test_a_page_that_isnt_json_is_chess_com_unavailable(monkeypatch):
    """E1: a maintenance page raised JSONDecodeError, a ValueError, which the sync route reads as
    "no username yet" (a 400 with a JSON parse message)."""
    import urllib.request

    class Page(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Page(b"<html>maintenance</html>"))
    with pytest.raises(chesscom.Unavailable):
        chesscom.fetch_json("https://api.chess.com/pub/player/x/games/archives")
    assert issubclass(chesscom.Unavailable, OSError)       # the sync route answers 502
