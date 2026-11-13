"""Finish M1 / F2: backups with SQLite's backup API, rotation, restore, and the copies made
before anything that rewrites or deletes data."""

import io
import shutil
import sqlite3
from compression import zstd
from datetime import datetime, timedelta

import pytest

from chessreview import backup, classify, db, library
from chessreview.config import BackupConfig, load_config

DAY = datetime(2026, 10, 19, 9, 0, 0)  # a Monday


@pytest.fixture
def path(tmp_path):
    return tmp_path / "chess.db"


@pytest.fixture
def conn(path):
    c = db.connect(path)
    add_game(c, 1)
    yield c
    c.close()


def add_game(c, game_id):
    c.execute(
        """INSERT INTO games (id, dedupe_key, my_color, my_result, moves_uci, num_plies, pgn)
                 VALUES (?, ?, 'white', 'win', 'e2e4', 1, '')""",
        (game_id, f"k{game_id}"),
    )
    c.commit()


def games_in(path) -> int:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return c.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    finally:
        c.close()


def test_backup_includes_work_still_in_the_wal(conn, path):
    """A plain file copy of a WAL database can miss recent work; the backup API can't."""
    add_game(conn, 2)
    assert path.with_name("chess.db-wal").stat().st_size > 0  # not checkpointed into chess.db yet
    plain = path.with_name("plain-copy.db")
    shutil.copy(path, plain)
    with pytest.raises(sqlite3.Error):  # the file alone doesn't even have the tables
        games_in(plain)
    copy = backup.make(conn, "manual")
    assert copy.parent == path.parent / "backups" and backup.NAME.match(copy.name)
    assert games_in(copy) == 2
    assert not copy.with_name(copy.name + "-wal").exists()  # one self-contained file
    assert not list(copy.parent.glob("*.tmp"))


def test_daily_once_a_day_with_a_weekly_copy(conn, path):
    folder = backup.folder_for(path)
    assert backup.daily(path, now=DAY)
    assert backup.daily(path, now=DAY + timedelta(hours=5)) is None  # once a day
    assert backup.daily(path, now=DAY + timedelta(days=1))  # Tuesday: same week
    kinds = [b.kind for b in backup.files(folder)]
    assert kinds.count("daily") == 2 and kinds.count("weekly") == 1
    backup.daily(path, now=DAY + timedelta(days=7))  # next week
    assert [b.kind for b in backup.files(folder)].count("weekly") == 2


def test_rotation_keeps_7_daily_4_weekly_and_10_others(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    for i in range(12):
        stamp = (DAY - timedelta(days=i)).strftime(backup.STAMP)
        for kind in ("daily", "weekly", "manual", "before-delete"):
            (folder / f"chess-{kind}-{stamp}.db").write_bytes(b"")
    (folder / "notes.txt").write_text("not a backup")
    backup.rotate(folder, BackupConfig())
    kinds = [b.kind for b in backup.files(folder)]
    assert kinds.count("daily") == 7 and kinds.count("weekly") == 4
    assert kinds.count("manual") + kinds.count("before-delete") == 10
    assert backup.files(folder)[0].at == DAY  # the newest are kept
    assert (folder / "notes.txt").exists()


def test_restore_round_trip(conn, path):
    copy = backup.make(conn, "manual")
    db.delete_all_games(conn)
    assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0
    before = db.restore(conn, copy)
    assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert before.name.startswith("chess-before-restore-") and games_in(before) == 0
    other = db.connect(path)  # other connections see the restored data
    assert other.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 1
    other.close()


def test_restore_upgrades_an_older_copy(conn, path, monkeypatch):
    copy = backup.make(conn, "manual")
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + [lambda c: c.execute("CREATE TABLE extra (x)")])
    db.migrate(conn)
    db.restore(conn, copy)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'extra'").fetchone()


def test_restore_refuses_files_that_are_not_intact_chess_review_databases(conn, tmp_path):
    text = tmp_path / "notes.db"
    text.write_text("hello")
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(backup.SQLITE_HEADER + bytes(range(256)) * 40)
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE t (x)").connection.commit()
    newer = backup.make(conn, "manual")
    c = sqlite3.connect(newer)
    c.execute(f"PRAGMA user_version = {len(db.MIGRATIONS) + 1}")
    c.close()
    for bad, reason in (
        (text, "isn't a database"),
        (garbage, "can't be read"),
        (other, "isn't a Chess Review database"),
        (newer, "newer version"),
    ):
        with pytest.raises(ValueError, match=reason):
            db.restore(conn, bad)
    assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 1  # untouched


def test_copies_before_destructive_actions(conn, path, monkeypatch):
    folder = backup.folder_for(path)

    def kinds() -> set[str]:
        return {b.kind for b in backup.files(folder)}

    conn.execute("""INSERT INTO mistakes (game_id, ply, judgement, is_mine) VALUES (1, 0, 'good', 1)""")
    conn.commit()
    classify.ensure_classified(conn, load_config(), force=True)  # CLI reclassify, thresholds switch
    assert "before-reclassify" in kinds()

    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + [lambda c: c.execute("CREATE TABLE extra (x)")])
    db.connect(path).close()  # a pending migration
    migration = next(b for b in backup.files(folder) if b.kind == "before-migration")
    assert sqlite3.connect(migration.path).execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS) - 1

    data = zstd.compress(
        b"PuzzleId,FEN,Moves,Rating,RatingDeviation,Popularity,NbPlays,Themes,GameUrl\n"
        b"aaaaa,8/8/8/8/8/8/8/K6k w - - 0 1,a1a2 h1h2,1000,70,95,5000,fork,u\n"
    )

    def opener(req, timeout):
        return FakeResponse(data)

    library.fetch(conn, opener=opener)  # the first download: nothing to lose
    assert "before-library" not in kinds()
    library.fetch(conn, opener=opener)  # a refresh
    assert "before-library" in kinds()

    db.delete_all_games(conn)  # Settings and CLI reset
    deleted = next(b for b in backup.files(folder) if b.kind == "before-delete")
    assert games_in(deleted.path) == 1


def test_no_copies_of_an_empty_database(tmp_path):
    c = db.connect(tmp_path / "new.db")  # fresh: migrations run, nothing copied
    db.delete_all_games(c)
    assert backup.files(backup.folder_of(c)) == []
    c.close()


def test_export_snapshot_is_a_restorable_database(conn, tmp_path):
    data = backup.snapshot_bytes(conn)
    exported = tmp_path / "export.db"
    exported.write_bytes(data)
    backup.check(exported, len(db.MIGRATIONS))
    assert games_in(exported) == 1


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}
