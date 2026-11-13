"""Decide whether a position is in the opening, middlegame or endgame.

Rules (thresholds in config.toml [phases]):
  - endgame:  non-pawn material of both sides together <= endgame_material
              (knight/bishop 3, rook 5, queen 9)
  - opening:  the first opening_plies half-moves (unless already an endgame)
  - otherwise middlegame
"""

import chess

from .config import Config

PIECE_VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}


def non_pawn_material(board: chess.Board) -> int:
    """Total value of both sides' knights, bishops, rooks and queens."""
    return sum(
        PIECE_VALUES[pt] * len(board.pieces(pt, color))
        for color in chess.COLORS
        for pt in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN)
    )


def phase(board: chess.Board, ply: int, cfg: Config) -> str:
    """'opening', 'middlegame' or 'endgame' for a position at `ply`."""
    if non_pawn_material(board) <= cfg.phases.endgame_material:
        return "endgame"
    if ply < cfg.phases.opening_plies:
        return "opening"
    return "middlegame"
