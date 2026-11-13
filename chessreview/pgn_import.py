"""Parse PGN files, work out my colour/result/opening, and store new games.

Openings are named from the moves (openings.py), falling back to chess.com's
ECO/ECOUrl headers. Clock times ([%clk] comments, present in chess.com API games) are
kept per ply. A game is a duplicate if its Link or its content hash (players, date,
moves) is already known, so uploads and chess.com syncs never double up. Duplicates were
originally detected by the chess.com Link header, or by a hash of the
players, date and moves when there is no Link.
"""

import io
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import chess
import chess.pgn

from . import db, openings, session
from .config import Config

# Words that end the "family" part of an opening name,
# e.g. "Sicilian Defense Najdorf Variation" -> "Sicilian Defense".
FAMILY_ENDINGS = {"Defense", "Defence", "Opening", "Game", "Gambit", "Attack", "System"}


@dataclass
class ImportStats:
    """Counts from an import: files, games seen, imported, duplicates, skips by reason, and files it couldn't read."""

    files: int = 0
    games_seen: int = 0
    imported: int = 0
    duplicates: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)  # "<path>: <why>" for paths that couldn't be read

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def merge(self, other: ImportStats) -> None:
        self.files += other.files
        self.games_seen += other.games_seen
        self.imported += other.imported
        self.duplicates += other.duplicates
        for reason, n in other.skipped.items():
            self.skipped[reason] = self.skipped.get(reason, 0) + n
        self.errors += other.errors


def time_class(time_control: str) -> str:
    """Map a chess.com TimeControl header to bullet/blitz/rapid/daily.

    Uses chess.com's rule: estimated duration = base + 40 * increment.
    """
    if not time_control or time_control == "-":
        return "unknown"
    if "/" in time_control:  # daily games look like "1/86400"
        return "daily"
    base, _, inc = time_control.partition("+")
    try:
        total = int(base) + 40 * int(inc or 0)
    except ValueError:
        return "unknown"
    if total < 180:
        return "bullet"
    if total < 600:
        return "blitz"
    return "rapid"


def opening_from_headers(headers: chess.pgn.Headers) -> tuple[str, str]:
    """Return (opening name, opening family) from chess.com's ECOUrl header."""
    url = headers.get("ECOUrl", "")
    if url:
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        words = [w for w in slug.split("-") if w and not re.search(r"\d", w)]
        name = " ".join(words)
    else:
        name = headers.get("Opening", "") or headers.get("ECO", "") or "Unknown"
        words = name.split()

    family_words = []
    for w in words:
        family_words.append(w)
        if w in FAMILY_ENDINGS:
            break
    else:
        family_words = words[:2]
    family = " ".join(family_words) or name
    return name or "Unknown", family or "Unknown"


def dedupe_key(game: chess.pgn.Game, moves_uci: str) -> str:
    """The game's identity across imports: its Link header, else a hash of players, date and moves."""
    link = game.headers.get("Link", "").strip()
    if link:
        return link
    h = game.headers
    return "sha1:" + db.content_hash(h.get("White", ""), h.get("Black", ""), h.get("Date", ""), moves_uci)


def end_time(headers: chess.pgn.Headers) -> str:
    """Sortable 'YYYY-MM-DD HH:MM:SS' for when the game finished (time may be unknown)."""
    date = (headers.get("EndDate") or headers.get("UTCDate") or headers.get("Date") or "").replace(".", "-")
    clock = (headers.get("EndTime") or headers.get("UTCTime") or "")[:8]
    if not re.fullmatch(r"\d\d:\d\d:\d\d", clock):
        clock = "00:00:00"
    return f"{date} {clock}"


def clocks_ms(game: chess.pgn.Game) -> str | None:
    """Remaining clock after each ply, space-separated milliseconds ('-' if missing)."""
    values = []
    for node in game.mainline():
        seconds = node.clock()
        values.append("-" if seconds is None else str(round(seconds * 1000)))
    return " ".join(values) if any(v != "-" for v in values) else None


def my_result(result: str, my_color: str) -> str | None:
    """'win', 'draw' or 'loss' from my side, or None for an unfinished game."""
    if result == "1/2-1/2":
        return "draw"
    if result == "1-0":
        return "win" if my_color == "white" else "loss"
    if result == "0-1":
        return "win" if my_color == "black" else "loss"
    return None


def import_game(
    conn: sqlite3.Connection,
    game: chess.pgn.Game,
    cfg: Config,
    source: str,
    stats: ImportStats,
    batch_id: int | None = None,
    origin: str = "upload",
    extra: dict | None = None,
) -> None:
    """Store one game if it is mine, standard chess, finished and new.

    `origin` is 'upload' or 'chess.com'; `extra` may carry API-only fields (uuid, rated).
    """
    extra = extra or {}
    h = game.headers
    stats.games_seen += 1

    if game.errors:
        stats.skip("unparseable moves")
        return
    variant = h.get("Variant", "Standard")
    if cfg.imports.skip_variants and variant.lower() not in ("standard", "chess"):
        stats.skip(f"variant ({variant})")
        return

    user = db.username(conn, cfg).lower()
    if user and h.get("White", "").lower() == user:
        color = "white"
    elif user and h.get("Black", "").lower() == user:
        color = "black"
    else:
        stats.skip("username not found in game")
        return

    mainline = list(game.mainline_moves())
    moves = [m.uci() for m in mainline]
    if len(moves) < cfg.imports.min_plies:
        stats.skip(f"fewer than {cfg.imports.min_plies} plies")
        return
    result = h.get("Result", "*")
    res = my_result(result, color)
    if res is None:
        stats.skip("unfinished game")
        return

    moves_uci = " ".join(moves)
    key = dedupe_key(game, moves_uci)
    chash = db.content_hash(h.get("White", ""), h.get("Black", ""), h.get("Date", "").replace(".", "-"), moves_uci)
    link = h.get("Link") or None
    known_game = conn.execute(
        "SELECT 1 FROM games WHERE dedupe_key = ? OR content_hash = ? OR (link IS NOT NULL AND link = ?)",
        (key, chash, link),
    ).fetchone()
    if known_game:
        stats.duplicates += 1
        return

    start_fen = h.get("FEN") if h.get("SetUp") == "1" else None
    known = openings.identify(mainline, start_fen)
    eco: str | None
    if known:
        eco, opening, family = known
    else:
        eco = h.get("ECO")
        opening, family = opening_from_headers(h)
    # Another import (a second tab, a sync) may have stored the same game since the check above.
    cur = conn.execute(
        """INSERT INTO games (dedupe_key, link, white, black, white_elo, black_elo,
               my_color, my_result, result, termination, date, time_control, time_class,
               eco, opening, opening_family, start_fen, moves_uci, num_plies, pgn, source_file,
               content_hash, end_time, uuid, rated, source, batch_id, clocks_ms)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (dedupe_key) DO NOTHING""",
        (
            key,
            link,
            h.get("White"),
            h.get("Black"),
            _int(h.get("WhiteElo")),
            _int(h.get("BlackElo")),
            color,
            res,
            result,
            h.get("Termination"),
            h.get("Date", "").replace(".", "-"),
            h.get("TimeControl"),
            time_class(h.get("TimeControl", "")),
            eco,
            opening,
            family,
            start_fen,
            moves_uci,
            len(moves),
            str(game),
            source,
            chash,
            end_time(h),
            extra.get("uuid"),
            None if extra.get("rated") is None else int(bool(extra["rated"])),
            origin,
            batch_id,
            clocks_ms(game),
        ),
    )
    if cur.rowcount == 0:
        stats.duplicates += 1
        return
    stats.imported += 1


def import_pgn_text(
    conn: sqlite3.Connection, text: str, cfg: Config, source: str = "", batch_id: int | None = None
) -> ImportStats:
    """Import every game in a PGN string. Commits once at the end."""
    stats = ImportStats(files=1)
    stream = io.StringIO(text)
    while True:
        start = stream.tell()
        try:
            game = chess.pgn.read_game(stream)
        except Exception:  # python-chess gave up on this game: skip it and read on
            stats.skip("unreadable PGN")
            if stream.tell() == start:  # nothing consumed: reading on would loop for ever
                break
            continue
        if game is None:
            break
        import_game(conn, game, cfg, source, stats, batch_id=batch_id)
    conn.commit()
    return stats


def import_paths(conn: sqlite3.Connection, paths: list[Path], cfg: Config) -> ImportStats:
    """Import PGN files, or every *.pgn file inside given directories, as one batch.

    A path that doesn't exist, a folder without PGN files or a file that can't be read is noted in
    `errors` and the rest are still imported.
    """
    total = ImportStats()
    batch_id = db.create_batch(conn, "upload", f"Upload of {len(paths)} path(s)")
    try:
        for path in paths:
            if not path.exists():
                total.errors.append(f"{path}: no such file or folder")
                continue
            files = sorted(path.glob("*.pgn")) if path.is_dir() else [path]
            if not files:
                total.errors.append(f"{path}: no .pgn files in this folder")
            for f in files:
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    total.errors.append(f"{f}: {exc.strerror or exc}")
                    continue
                total.merge(import_pgn_text(conn, text, cfg, source=f.name, batch_id=batch_id))
    finally:
        session.finish_import(conn, cfg, batch_id)  # an empty batch is dropped, even after an error
    return total


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None
