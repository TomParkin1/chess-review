"""Parse PGN files, work out my colour/result/opening, and store new games.

Duplicates are detected by the chess.com Link header, or by a hash of the
players, date and moves when there is no Link.
"""

import hashlib
import io
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import chess
import chess.pgn

from .config import Config

# Words that end the "family" part of an opening name,
# e.g. "Sicilian Defense Najdorf Variation" -> "Sicilian Defense".
FAMILY_ENDINGS = {"Defense", "Defence", "Opening", "Game", "Gambit", "Attack", "System"}


@dataclass
class ImportStats:
    files: int = 0
    games_seen: int = 0
    imported: int = 0
    duplicates: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def merge(self, other: "ImportStats") -> None:
        self.files += other.files
        self.games_seen += other.games_seen
        self.imported += other.imported
        self.duplicates += other.duplicates
        for reason, n in other.skipped.items():
            self.skipped[reason] = self.skipped.get(reason, 0) + n


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
    link = game.headers.get("Link", "").strip()
    if link:
        return link
    h = game.headers
    basis = "|".join([h.get("White", ""), h.get("Black", ""), h.get("Date", ""), moves_uci])
    return "sha1:" + hashlib.sha1(basis.encode()).hexdigest()


def my_result(result: str, my_color: str) -> str | None:
    if result == "1/2-1/2":
        return "draw"
    if result == "1-0":
        return "win" if my_color == "white" else "loss"
    if result == "0-1":
        return "win" if my_color == "black" else "loss"
    return None


def import_game(conn: sqlite3.Connection, game: chess.pgn.Game, cfg: Config,
                source: str, stats: ImportStats) -> None:
    h = game.headers
    stats.games_seen += 1

    if game.errors:
        stats.skip("unparseable moves")
        return
    variant = h.get("Variant", "Standard")
    if cfg.imports.skip_variants and variant.lower() not in ("standard", "chess"):
        stats.skip(f"variant ({variant})")
        return

    user = cfg.player.username.lower()
    if h.get("White", "").lower() == user:
        color = "white"
    elif h.get("Black", "").lower() == user:
        color = "black"
    else:
        stats.skip("username not found in game")
        return

    moves = [m.uci() for m in game.mainline_moves()]
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
    if conn.execute("SELECT 1 FROM games WHERE dedupe_key = ?", (key,)).fetchone():
        stats.duplicates += 1
        return

    opening, family = opening_from_headers(h)
    start_fen = h.get("FEN") if h.get("SetUp") == "1" else None
    conn.execute(
        """INSERT INTO games (dedupe_key, link, white, black, white_elo, black_elo,
               my_color, my_result, result, termination, date, time_control, time_class,
               eco, opening, opening_family, start_fen, moves_uci, num_plies, pgn, source_file)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            key, h.get("Link"), h.get("White"), h.get("Black"),
            _int(h.get("WhiteElo")), _int(h.get("BlackElo")),
            color, res, result, h.get("Termination"),
            h.get("Date", "").replace(".", "-"),
            h.get("TimeControl"), time_class(h.get("TimeControl", "")),
            h.get("ECO"), opening, family, start_fen,
            moves_uci, len(moves), str(game), source,
        ),
    )
    stats.imported += 1


def import_pgn_text(conn: sqlite3.Connection, text: str, cfg: Config,
                    source: str = "") -> ImportStats:
    """Import every game in a PGN string. Commits once at the end."""
    stats = ImportStats(files=1)
    stream = io.StringIO(text)
    while True:
        try:
            game = chess.pgn.read_game(stream)
        except Exception:  # extremely malformed input; stop reading this file
            stats.skip("unreadable PGN")
            break
        if game is None:
            break
        import_game(conn, game, cfg, source, stats)
    conn.commit()
    return stats


def import_paths(conn: sqlite3.Connection, paths: list[Path], cfg: Config) -> ImportStats:
    """Import PGN files, or every *.pgn file inside given directories."""
    total = ImportStats()
    for path in paths:
        files = sorted(path.glob("*.pgn")) if path.is_dir() else [path]
        for f in files:
            text = f.read_text(encoding="utf-8", errors="replace")
            total.merge(import_pgn_text(conn, text, cfg, source=f.name))
    return total


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None
