"""Puzzle library: a stratified sample of the Lichess puzzle database (CC0), for every rating.

`fetch()` streams https://database.lichess.org/lichess_db_puzzle.csv.zst (about 300 MB)
through the stdlib zstd decompressor and keeps, for every 100-point rating bucket from 400
to 3100 and every theme in THEMES, a random sample of SAMPLE_PER_CELL well-tested puzzles
(popularity, plays and rating deviation filters). Nothing but the sample is stored (~25k
puzzles, a few MB). A refresh keeps the puzzles I have already tried (attempts and library cards
refer to them by id) and backs up the database first.

CSV columns: PuzzleId, FEN, Moves, Rating, RatingDeviation, Popularity, NbPlays, Themes,
GameUrl, OpeningTags (+ DailyDate). FEN is the position *before* the opponent's move; Moves
starts with that move, then alternates my solution moves and the opponent's replies.

Puzzle rating: starts at my current chess.com rating and moves with an Elo update against
each puzzle's rating; new puzzles are picked around it, so the difficulty follows me up. The
first puzzles count most (K 120 shrinking to 40 by the 5th, like a provisional rating), so a
seed that is too low or too high corrects itself quickly; then K goes from 40 down to 16 over
the first 100 puzzles. Only the first try of a puzzle counts: a clean solve scores 1, a solve
with hints or after a wrong move 1/2 (a hint costs less than a failure), anything else 0.
"""

import csv
import io
import math
import random
import sqlite3
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, BinaryIO

import chess

from . import backup, db, level, srs

URL = "https://database.lichess.org/lichess_db_puzzle.csv.zst"
BUCKET = 100
MIN_RATING, MAX_RATING = 400, 3100
SAMPLE_PER_CELL = 25  # puzzles kept per (rating bucket, theme)
MIN_POPULARITY, MIN_PLAYS, MAX_DEVIATION = 80, 300, 90
RATING_KEY, COUNT_KEY, FETCHED_KEY = "puzzle_rating", "puzzle_count", "puzzle_library_fetched"
THEMES_KEY = "puzzle_library_themes"  # the THEMES the stored sample was drawn for (E2)
DEFAULT_RATING = 1500

# Lichess theme tag -> label. Only these themes are sampled (phase/length tags are not).
THEMES = {
    "mateIn1": "Mate in 1",
    "mateIn2": "Mate in 2",
    "mateIn3": "Mate in 3",
    "backRankMate": "Back-rank mate",
    "smotheredMate": "Smothered mate",
    "fork": "Fork",
    "pin": "Pin",
    "skewer": "Skewer",
    "discoveredAttack": "Discovered attack",
    "doubleCheck": "Double check",
    "hangingPiece": "Hanging piece",
    "trappedPiece": "Trapped piece",
    "sacrifice": "Sacrifice",
    "deflection": "Deflection",
    "attraction": "Attraction",
    "clearance": "Clearance",
    "interference": "Interference",
    "intermezzo": "In-between move",
    "xRayAttack": "X-ray",
    "capturingDefender": "Capture the defender",
    "zugzwang": "Zugzwang",
    "defensiveMove": "Defensive move",
    "quietMove": "Quiet move",
    "exposedKing": "Exposed king",
    "kingsideAttack": "Kingside attack",
    "attackingF2F7": "Attacking f2/f7",
    "advancedPawn": "Advanced pawn",
    "promotion": "Promotion",
    "endgame": "Endgame",
    "rookEndgame": "Rook endgame",
    "pawnEndgame": "Pawn endgame",
    "queenEndgame": "Queen endgame",
    "bishopEndgame": "Bishop endgame",
    "knightEndgame": "Knight endgame",
    "queenRookEndgame": "Queen and rook endgame",
    "opening": "Opening",
    "crushing": "Crushing",
    "advantage": "Winning an advantage",
}

# Too broad to be a useful hint.
GENERIC_THEMES = {"Crushing", "Winning an advantage", "Endgame", "Opening", "Advanced pawn"}

# My mistake kinds (explain.py) -> the puzzle themes that train them.
KIND_THEMES = {
    "hanging_piece": ["hangingPiece"],
    "missed_capture": ["hangingPiece", "capturingDefender"],
    "fork": ["fork"],
    "discovered_attack": ["discoveredAttack"],
    "missed_mate": ["mateIn1", "mateIn2", "mateIn3"],
    "allowed_mate": ["defensiveMove", "backRankMate"],
    "king_safety": ["exposedKing", "defensiveMove"],
    "missed_tactic": ["pin", "skewer", "fork", "discoveredAttack"],
    "missed_opportunity": ["advantage", "crushing"],
    "loses_material": ["defensiveMove", "trappedPiece"],
    "positional": ["quietMove", "defensiveMove"],
}
# Endgame types (E2), and the order Today's endgame block cycles through them: roughly how often
# each comes up (Lichess frequency), rook endgames most.
ENDGAME_THEMES = ["rookEndgame", "pawnEndgame", "queenEndgame", "bishopEndgame", "knightEndgame", "queenRookEndgame"]
ENDGAME_ROTATION = [
    "rookEndgame",
    "pawnEndgame",
    "rookEndgame",
    "queenEndgame",
    "pawnEndgame",
    "bishopEndgame",
    "rookEndgame",
    "knightEndgame",
    "pawnEndgame",
    "queenRookEndgame",
]
PHASE_THEMES = {"endgame": ENDGAME_THEMES, "opening": ["opening"], "middlegame": ["kingsideAttack", "sacrifice"]}


def themes_for(train: dict | None) -> list[str]:
    """Puzzle themes for a weakness' Practice filter (insights.weaknesses), [] if none fit."""
    train = train or {}
    if train.get("kind"):
        return KIND_THEMES.get(train["kind"], [])
    if train.get("phase"):
        return PHASE_THEMES.get(train["phase"], [])
    return []


# ------------------------------------------------------------ download + sampling


@dataclass
class FetchProgress:
    state: str = "idle"  # idle | running | done | error
    bytes_read: int = 0
    bytes_total: int = 0
    rows: int = 0
    kept: int = 0
    error: str = ""
    started_at: float | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class _CountingReader(io.RawIOBase):
    """Wraps the HTTP response to count compressed bytes for the progress bar."""

    def __init__(self, raw: BinaryIO, progress: FetchProgress) -> None:
        self.raw, self.progress = raw, progress

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        data = self.raw.read(len(buffer))
        buffer[: len(data)] = data
        self.progress.bytes_read += len(data)
        return len(data)


@dataclass
class Sampler:
    """Reservoir sample per (rating bucket, theme)."""

    per_cell: int = SAMPLE_PER_CELL
    seed: int = 1
    cells: dict = field(default_factory=dict)  # (bucket, theme) -> [rows]
    seen: dict = field(default_factory=dict)  # (bucket, theme) -> count

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)

    def offer(self, row: list[str]) -> None:
        try:
            rating, deviation, popularity, plays = int(row[3]), int(row[4]), int(row[5]), int(row[6])
        except ValueError, IndexError:
            return
        if (
            not (MIN_RATING <= rating < MAX_RATING)
            or deviation > MAX_DEVIATION
            or popularity < MIN_POPULARITY
            or plays < MIN_PLAYS
        ):
            return
        bucket = rating // BUCKET * BUCKET
        for theme in row[7].split():
            if theme not in THEMES:
                continue
            cell = (bucket, theme)
            n = self.seen.get(cell, 0) + 1
            self.seen[cell] = n
            kept = self.cells.setdefault(cell, [])
            if len(kept) < self.per_cell:
                kept.append(row)
            else:
                j = self.rng.randrange(n)
                if j < self.per_cell:
                    kept[j] = row

    def puzzles(self) -> dict[str, list[str]]:
        return {row[0]: row for rows in self.cells.values() for row in rows}


def fetch(
    conn: sqlite3.Connection,
    progress: FetchProgress | None = None,
    url: str = URL,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> int:
    """Download and sample the puzzle database, replacing the stored library (puzzles I have
    tried are kept, see below). Returns the number of puzzles in the new sample."""
    from compression import zstd  # stdlib since Python 3.14

    progress = progress or FetchProgress()
    progress.state, progress.started_at = "running", time.time()
    sampler = Sampler()
    request = urllib.request.Request(url, headers={"User-Agent": "chess-review (local training app)"})
    with opener(request, timeout=60) as response:
        progress.bytes_total = int(response.headers.get("Content-Length") or 0)
        raw = io.BufferedReader(_CountingReader(response, progress), buffer_size=1 << 20)
        text = io.TextIOWrapper(zstd.ZstdFile(raw), encoding="utf-8", newline="")
        reader = csv.reader(text)
        next(reader, None)  # header
        for row in reader:
            sampler.offer(row)
            progress.rows += 1
    puzzles = sampler.puzzles()
    if count(conn):
        backup.make(conn, "before-library")
    with conn:
        # Puzzles I have tried stay: their attempts and spaced-repetition cards point at them.
        conn.execute("""DELETE FROM puzzles WHERE id NOT IN (SELECT puzzle_id FROM puzzle_attempts)
                        AND id NOT IN (SELECT key FROM cards WHERE kind = 'library')
                        AND id NOT IN (SELECT puzzle_id FROM calc_attempts WHERE puzzle_id IS NOT NULL)""")
        conn.executemany(
            """INSERT OR IGNORE INTO puzzles (id, fen, moves, rating, deviation, popularity, plays, themes, game_url)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [
                (r[0], r[1], r[2], int(r[3]), int(r[4]), int(r[5]), int(r[6]), f" {r[7]} ", r[8])
                for r in puzzles.values()
            ],
        )
        db.set_meta(conn, FETCHED_KEY, time.strftime("%Y-%m-%d"))
        db.set_meta(conn, THEMES_KEY, themes_stamp())
    progress.kept, progress.state = len(puzzles), "done"
    return len(puzzles)


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM puzzles").fetchone()[0]


def themes_stamp() -> str:
    return " ".join(sorted(THEMES))


def outdated(conn: sqlite3.Connection) -> bool:
    """The stored sample was drawn before themes were added to THEMES (an update samples them too;
    puzzles that carry a new theme among their tags are found by it already)."""
    return count(conn) > 0 and db.get_meta(conn, THEMES_KEY) != themes_stamp()


# ------------------------------------------------------------ puzzle rating


def rating(conn: sqlite3.Connection) -> int:
    stored = db.get_meta(conn, RATING_KEY)
    if stored is not None:
        return round(float(stored))
    return level.current_rating(conn) or DEFAULT_RATING


SCORES = {"solved": 1.0, "solved_with_help": 0.5}  # anything else scores 0


def k_factor(played: int) -> float:
    return max(16.0, 40 - 24 * min(played, 100) / 100, 120 * 0.8**played)


def update_rating(conn: sqlite3.Connection, puzzle_rating: int, score: float) -> tuple[int, int]:
    """Elo update of my puzzle rating after one puzzle (score 1, 1/2 or 0). Returns (before, after)."""
    before = rating(conn)
    played = int(db.get_meta(conn, COUNT_KEY) or 0)
    expected = 1 / (1 + math.pow(10, (puzzle_rating - before) / 400))
    after = before + k_factor(played) * (score - expected)
    with conn:
        db.set_meta(conn, RATING_KEY, str(after))
        db.set_meta(conn, COUNT_KEY, str(played + 1))
    return before, round(after)


_record_lock = threading.Lock()


def record(conn: sqlite3.Connection, puzzle_id: str, result: str) -> dict:
    """Store an attempt, update my puzzle rating (only the first attempt of a puzzle counts)
    and its spaced-repetition card (failed puzzles become cards; due ones get reviewed)."""
    p = conn.execute("SELECT rating FROM puzzles WHERE id = ?", (puzzle_id,)).fetchone()
    if p is None:
        raise LookupError(puzzle_id)
    with _record_lock:  # "first attempt?" and the rating update are one step (a double submit)
        first = conn.execute("SELECT 1 FROM puzzle_attempts WHERE puzzle_id = ?", (puzzle_id,)).fetchone() is None
        if first:
            before, after = update_rating(conn, p["rating"], SCORES.get(result, 0.0))
        else:
            before = after = rating(conn)
        with conn:
            conn.execute(
                """INSERT INTO puzzle_attempts (puzzle_id, result, rating_before, rating_after)
                            VALUES (?, ?, ?, ?)""",
                (puzzle_id, result, before, after),
            )
    card = conn.execute("SELECT 1 FROM cards WHERE kind = 'library' AND key = ?", (puzzle_id,)).fetchone()
    if card or result not in srs.SOLVED:
        srs.record_key(conn, "library", puzzle_id, result)
    return {"rating_before": before, "rating_after": after, "rated": first}


# ------------------------------------------------------------ choosing puzzles


def pick(
    conn: sqlite3.Connection,
    n: int,
    themes: list[str] | None = None,
    around: int | None = None,
    exclude: set[str] | None = None,
    seed: str | None = None,
) -> list[str]:
    """Up to `n` puzzle ids I have never tried, near my puzzle rating (the window widens
    until enough are found), optionally with any of `themes`. `seed` makes it deterministic."""
    around = around or rating(conn)
    exclude = exclude or set()
    theme_sql, params = "", []
    if themes:
        theme_sql = " AND (" + " OR ".join("themes LIKE ?" for _ in themes) + ")"
        params = [f"% {t} %" for t in themes]
    for width in (100, 200, 400, 800):
        rows = conn.execute(
            f"""
            SELECT id FROM puzzles WHERE rating BETWEEN ? AND ?{theme_sql}
              AND id NOT IN (SELECT puzzle_id FROM puzzle_attempts)
              AND id NOT IN (SELECT puzzle_id FROM calc_attempts WHERE puzzle_id IS NOT NULL) ORDER BY id""",
            [around - width, around + width, *params],
        ).fetchall()
        ids = [r["id"] for r in rows if r["id"] not in exclude]
        if len(ids) >= n or width == 800:
            random.Random(seed).shuffle(ids)
            return ids[:n]
    return []


def pick_endgames(conn: sqlite3.Connection, n: int, on: date, exclude: set[str] | None = None) -> list[str]:
    """Up to `n` endgame puzzles near my puzzle rating, one type after
    another along ENDGAME_ROTATION (where the day starts in it moves daily); a type with nothing
    unseen falls back to any endgame type."""
    exclude = set(exclude or ())
    out: list[str] = []
    start = on.toordinal() % len(ENDGAME_ROTATION)
    for i in range(n):
        theme = ENDGAME_ROTATION[(start + i) % len(ENDGAME_ROTATION)]
        seed = f"{on.isoformat()}:{i}"
        found = pick(conn, 1, [theme], exclude=exclude, seed=seed) or pick(
            conn, 1, ENDGAME_THEMES + ["endgame"], exclude=exclude, seed=seed
        )
        if found:
            exclude.add(found[0])
            out.append(found[0])
    return out


def endgame_types(conn: sqlite3.Connection, ids: list[str]) -> list[str]:
    """The endgame types (labels, in ENDGAME_THEMES order) among these puzzles."""
    if not ids:
        return []
    tags = " ".join(
        r["themes"] for r in conn.execute(f"SELECT themes FROM puzzles WHERE id IN ({','.join('?' * len(ids))})", ids)
    ).split()
    return [THEMES[t] for t in ENDGAME_THEMES if t in tags]


def puzzle_json(row: sqlite3.Row) -> dict:
    """A library puzzle as a puzzle.js item (mode 'library'): the position after the
    opponent's first move, my colour, and the solution line from there."""
    board = chess.Board(row["fen"])
    moves = row["moves"].split()
    first = chess.Move.from_uci(moves[0])
    board.push(first)
    themes = [THEMES[t] for t in row["themes"].split() if t in THEMES]
    best = chess.Move.from_uci(moves[1])
    specific = [t for t in themes if t not in GENERIC_THEMES]
    return {
        "mode": "library",
        "puzzle_id": row["id"],
        "fen": board.fen(),
        "start_fen": row["fen"],
        "prev_move_uci": moves[0],
        "solution": moves[1:],
        "best_pv_uci": moves[1:],
        "best_uci": moves[1],
        "best_san": board.san(best),
        "my_color": "white" if board.turn else "black",
        "rating": row["rating"],
        "themes": themes,
        "game_url": row["game_url"],
        "playout_moves": len(moves) // 2,
        "move_label": f"Puzzle {row['id']}",
        "ply": board.ply(),
        "hint": f"Theme: {specific[0].lower()}." if specific else "Look at every check and capture first.",
    }
