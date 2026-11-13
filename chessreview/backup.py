"""Backups of the database, made with SQLite's online backup API.

The database runs in WAL mode: recent work can sit in chess.db-wal until a checkpoint, so a
plain file copy can miss it. Every copy is made with `Connection.backup()` (a consistent
snapshot, safe while the app runs) and then switched to a rollback journal, so it is one
self-contained file that can be copied, downloaded or restored.

Copies live in <database folder>/backups (data/backups for the real database), named
chess-<kind>-<YYYYMMDD-HHMMSS>.db:
  daily      made once a day by `serve` (at startup, then checked hourly); the first daily copy
             of an ISO week is also kept as a weekly copy
  manual     Settings -> Back up now (or the `backup` command)
  before-*   made automatically before something rewrites or deletes data: Delete all games,
             a full reclassification, a schema migration, a puzzle-library refresh, a restore
  upload     a file given to Restore
Rotation (config [backup]) keeps the newest `daily` daily copies, `weekly` weekly copies and a
shared pool of the newest `other` copies of every other kind.

This module only uses the standard library (db.py imports it for the pre-migration copy).
"""

import os
import re
import shutil
import sqlite3
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .config import BackupConfig

KINDS = {
    "daily": "Daily",
    "weekly": "Weekly",
    "manual": "Backed up by you",
    "before-delete": "Before Delete all games",
    "before-reclassify": "Before reclassifying your games",
    "before-migration": "Before a database upgrade",
    "before-library": "Before a puzzle library update",
    "before-restore": "Before restoring a backup",
    "upload": "Restored from a file",
}
NAME = re.compile(r"^chess-(?P<kind>[a-z-]+)-(?P<stamp>\d{8}-\d{6})\.db$")
STAMP = "%Y%m%d-%H%M%S"
SQLITE_HEADER = b"SQLite format 3\x00"

_lock = threading.RLock()  # one copy or restore at a time


@dataclass(frozen=True)
class Backup:
    path: Path
    kind: str
    at: datetime

    @property
    def label(self) -> str:
        return KINDS.get(self.kind, self.kind)

    def as_dict(self) -> dict:
        return {
            "name": self.path.name,
            "kind": self.kind,
            "label": self.label,
            "at": self.at.isoformat(timespec="seconds"),
            "size": self.path.stat().st_size,
            "games": _games_in(self.path),
        }


def folder_for(db_path: Path | str) -> Path:
    return Path(db_path).parent / "backups"


def file_of(conn: sqlite3.Connection) -> Path | None:
    """The database file `conn` is using (None in memory)."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return Path(row[2]) if row[2] else None
    return None


def folder_of(conn: sqlite3.Connection) -> Path | None:
    """The backups folder next to the database file `conn` is using (None in memory)."""
    path = file_of(conn)
    return folder_for(path) if path else None


def _copy(conn: sqlite3.Connection, path: Path) -> None:
    """Consistent copy of `conn`'s database into a new single-file database at `path`."""
    dst = sqlite3.connect(path)
    try:
        conn.backup(dst)
        dst.execute("PRAGMA journal_mode = DELETE")  # no -wal file beside the copy
    finally:
        dst.close()


def make(conn: sqlite3.Connection, kind: str, folder: Path | None = None, now: datetime | None = None) -> Path | None:
    """Back up the database `conn` is using. Returns the new file (None for an in-memory DB)."""
    folder = folder or folder_of(conn)
    if folder is None:
        return None
    with _lock:
        path = _new_path(folder, kind, now)
        tmp = path.with_name(path.name + ".tmp")
        _copy(conn, tmp)
        os.replace(tmp, path)  # a half-written copy never looks like a backup
    return path


def save_upload(folder: Path, data: bytes, now: datetime | None = None) -> Path:
    """Keep a file given to Restore beside the other copies (checked by `check` before use)."""
    with _lock:
        path = _new_path(folder, "upload", now)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    return path


def _new_path(folder: Path, kind: str, now: datetime | None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now()
    path = folder / f"chess-{kind}-{now:{STAMP}}.db"
    while path.exists():  # two copies of a kind in the same second
        now += timedelta(seconds=1)
        path = folder / f"chess-{kind}-{now:{STAMP}}.db"
    return path


def files(folder: Path) -> list[Backup]:
    """Backups in `folder`, newest first."""
    out = []
    if folder.is_dir():
        for path in folder.iterdir():
            m = NAME.match(path.name)
            if m:
                out.append(Backup(path, m["kind"], datetime.strptime(m["stamp"], STAMP)))
    return sorted(out, key=lambda b: (b.at, b.kind != "weekly"), reverse=True)  # a daily before its weekly twin


def daily(db_path: Path | str, keep: BackupConfig = BackupConfig(), now: datetime | None = None) -> Path | None:
    """Today's automatic copy (and this week's weekly copy), then rotation. Returns the new
    daily copy, or None if today's already exists or there is no database yet."""
    if not Path(db_path).exists():
        return None
    folder = folder_for(db_path)
    now = now or datetime.now()
    existing = files(folder)
    if any(b.kind == "daily" and b.at.date() == now.date() for b in existing):
        return None
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        path = make(conn, "daily", folder, now)
    finally:
        conn.close()
    week = now.isocalendar()[:2]
    if path is not None and not any(b.kind == "weekly" and b.at.isocalendar()[:2] == week for b in existing):
        shutil.copy2(path, folder / f"chess-weekly-{now:{STAMP}}.db")  # a finished single file
    rotate(folder, keep)
    return path


def rotate(folder: Path, keep: BackupConfig = BackupConfig()) -> list[Path]:
    """Delete copies beyond the newest keep.daily daily, keep.weekly weekly and keep.other others."""
    backups = files(folder)
    old = (
        [b for b in backups if b.kind == "daily"][keep.daily :]
        + [b for b in backups if b.kind == "weekly"][keep.weekly :]
        + [b for b in backups if b.kind not in ("daily", "weekly")][keep.other :]
    )
    with _lock:
        for b in old:
            b.path.unlink(missing_ok=True)
    return [b.path for b in old]


def check(path: Path, max_version: int) -> None:
    """Raise ValueError unless `path` is an intact Chess Review database we can restore."""
    with open(path, "rb") as f:
        if f.read(16) != SQLITE_HEADER:
            raise ValueError("This isn't a database file.")
    src = sqlite3.connect(path)
    try:
        if src.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("This database file is damaged.")
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"games", "meta"} <= tables:
            raise ValueError("This isn't a Chess Review database.")
        if src.execute("PRAGMA user_version").fetchone()[0] > max_version:
            raise ValueError("This backup comes from a newer version of Chess Review.")
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"This database file can't be read ({exc}).") from exc
    finally:
        src.close()


def restore(conn: sqlite3.Connection, source: Path, max_version: int) -> Path | None:
    """Replace the database `conn` is using with the copy at `source`, after backing up the
    current data. Returns that before-restore copy. The caller runs migrations afterwards."""
    check(source, max_version)
    with _lock:
        try:
            before = make(conn, "before-restore")
        except sqlite3.DatabaseError:  # the current database is unreadable: keep the raw file
            current = file_of(conn)
            before = None
            if current is not None:
                before = _new_path(folder_for(current), "before-restore", None)
                shutil.copy2(current, before)
        src = sqlite3.connect(source)
        try:
            src.backup(conn)
        finally:
            src.close()
        conn.execute("PRAGMA journal_mode = WAL")
    return before


def snapshot_bytes(conn: sqlite3.Connection) -> bytes:
    """A consistent copy of the whole database, as bytes (for Export)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "export.db"
        _copy(conn, path)
        return path.read_bytes()


def _games_in(path: Path) -> int | None:
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return c.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        finally:
            c.close()
    except sqlite3.Error:
        return None


class DailyBackups:
    """Background thread for `serve`: today's copy at startup, then a check every hour."""

    def __init__(self, db_path: Path | str, keep: BackupConfig, interval: float = 3600):
        self.db_path, self.keep, self.interval = db_path, keep, interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="daily-backup")

    def start(self) -> DailyBackups:
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                daily(self.db_path, self.keep)
            except (OSError, sqlite3.Error) as exc:  # e.g. disk full: try again next hour
                print(f"Daily backup failed: {exc}")
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
