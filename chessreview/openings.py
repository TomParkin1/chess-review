"""Name a game's opening from its moves, using the Lichess opening database.

data/openings.tsv comes from https://github.com/lichess-org/chess-openings (CC0).
Lines are indexed by position (EPD), so transpositions are recognised; a game gets
the name of the last position along its moves that appears in the database.
"""

import csv
import io
from functools import lru_cache
from pathlib import Path

import chess
import chess.pgn

OPENINGS_TSV = Path(__file__).parent / "data" / "openings.tsv"
MAX_PLIES = 40  # no named line in the database is longer than this


def _line(pgn: str) -> chess.pgn.Game:
    """One line of the openings file (always a valid PGN)."""
    game = chess.pgn.read_game(io.StringIO(pgn))
    assert game is not None, pgn
    return game


@lru_cache(maxsize=1)
def _positions() -> dict[str, tuple[str, str]]:
    """Map EPD -> (eco, name) for the final position of every named line."""
    index = {}
    with open(OPENINGS_TSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            board = _line(row["pgn"]).end().board()
            index[board.epd()] = (row["eco"], row["name"])
    return index


@lru_cache(maxsize=1)
def book_positions() -> frozenset[str]:
    """EPD of every position along every named line (for 'book move' marks)."""
    seen = set()
    with open(OPENINGS_TSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            board = chess.Board()
            for move in _line(row["pgn"]).mainline_moves():
                board.push(move)
                seen.add(board.epd())
    return frozenset(seen)


def is_book(board_after_move: chess.Board) -> bool:
    """Whether the position after a move is on a named opening line."""
    return board_after_move.epd() in book_positions()


def family_of(name: str) -> str:
    """'Sicilian Defense: Najdorf Variation' -> 'Sicilian Defense'."""
    return name.split(":", 1)[0].strip()


def identify(moves: list[chess.Move], start_fen: str | None = None) -> tuple[str, str, str] | None:
    """Return (eco, name, family) for the deepest known position, or None."""
    if start_fen:
        return None  # custom start positions have no book name
    index = _positions()
    board = chess.Board()
    found = None
    for move in moves[:MAX_PLIES]:
        board.push(move)
        hit = index.get(board.epd())
        if hit:
            found = hit
    if found is None:
        return None
    eco, name = found
    return eco, name, family_of(name)
