"""The opponent's threat in one of my positions, from the stored null-move search.

`gain` is how much my win chance (Lichess -1..1 scale) would drop if I passed. A threat is
"real" when that drop reaches the *mistake* threshold at my current level (so what counts as
a threat rescales with my rating, classify.thresholds_for bands, never a fixed number) AND the
opponent's move is a threat a player would name: a mate, a check, a capture that wins material,
or a new attack on a piece of mine that is really in danger. The second condition matters: when
I have a tactic, passing lets the opponent defuse it (e.g. by castling), which also costs me a
lot but is my lost chance, not their threat.
"""

import sqlite3
from dataclasses import dataclass

import chess

from .classify import Eval, Thresholds
from .explain import material_change
from .phases import PIECE_VALUES


@dataclass(frozen=True)
class Threat:
    uci: str | None      # the opponent's best move if I passed
    gain: float          # drop in my win chance if I passed (>= 0 in practice)
    real: bool

    def as_dict(self, fen: str) -> dict:
        san = None
        if self.uci:
            passed = chess.Board(fen)
            passed.push(chess.Move.null())
            san = passed.san(chess.Move.from_uci(self.uci))
        return {"uci": self.uci, "san": san, "gain": round(self.gain, 3), "real": self.real}


def threat_of(pos: sqlite3.Row | dict, me: chess.Color, th: Thresholds) -> Threat | None:
    """Threat in a stored position (needs fen, score_* and threat_* columns); None if not searched."""
    if pos["threat_cp"] is None and pos["threat_mate"] is None:
        return None
    before = Eval.from_row(pos, me)
    passed = Eval.from_row({"score_cp": pos["threat_cp"], "score_mate": pos["threat_mate"]}, me)
    gain = before.win_chance() - passed.win_chance()
    return Threat(pos["threat_uci"], gain, gain >= th.mistake and _line_wins(pos, me, passed))


def _line_wins(pos: sqlite3.Row | dict, me: chess.Color, passed: Eval) -> bool:
    """The opponent's move after my pass is something a player would call a threat: it starts a
    forced mate, it is a check or a promotion, it is a capture whose line wins material, or it
    newly attacks one of my pieces that is undefended or worth more than the attacker.
    A quiet move (castling, a developing move) whose line only wins material later is not: the
    engine found it because passing gave away my own chance, not because they threaten it."""
    if passed.mate is not None and passed.mate < 0:
        return True
    if not pos["threat_pv"]:
        return False
    board = chess.Board(pos["fen"])
    board.push(chess.Move.null())
    line = [chess.Move.from_uci(u) for u in pos["threat_pv"].split()]
    move = line[0]
    if board.is_castling(move):
        return board.gives_check(move)
    if board.gives_check(move) or move.promotion:
        return True
    if board.is_capture(move):
        return material_change(board, line, not me) >= 1
    attacked_before = _attacked_pieces(board, me)
    board.push(move)
    return any(_threatened(board, sq, me) for sq in _attacked_pieces(board, me) - attacked_before)


def _threatened(board: chess.Board, sq: chess.Square, me: chess.Color) -> bool:
    """My newly attacked piece on `sq` is really in danger: undefended, or worth more than its
    cheapest attacker (this also covers discovered attacks). A king can't take a defended piece."""
    piece = board.piece_at(sq)
    if not board.is_attacked_by(me, sq):
        return True
    attackers = [board.piece_at(a) for a in board.attackers(not me, sq)]
    values = [PIECE_VALUES[a.piece_type] for a in attackers if a and a.piece_type != chess.KING]
    return bool(values) and PIECE_VALUES[piece.piece_type] > min(values)


def _attacked_pieces(board: chess.Board, me: chess.Color) -> set[chess.Square]:
    """Squares of my pieces (not pawns or the king) that the opponent attacks."""
    return {sq for sq, piece in board.piece_map().items()
            if piece.color == me and piece.piece_type not in (chess.PAWN, chess.KING)
            and board.is_attacked_by(not me, sq)}


def recapture_pending(prev_fen: str | None, prev_uci: str | None, fen: str) -> bool:
    """The opponent just captured and I can take back on that square. Mid-exchange the null
    move only says "they keep the material", so these positions get no threat question."""
    if not prev_fen or not prev_uci:
        return False
    prev_move = chess.Move.from_uci(prev_uci)
    if not chess.Board(prev_fen).is_capture(prev_move):
        return False
    board = chess.Board(fen)
    return any(m.to_square == prev_move.to_square and board.is_capture(m) for m in board.legal_moves)


def training_threat(pos: sqlite3.Row | dict, me: chess.Color, th: Thresholds) -> Threat | None:
    """The threat to ask about in training, or None (not searched, or mid-exchange). `pos` also
    needs prev_fen and prev_move_uci (the position before the opponent's last move)."""
    if recapture_pending(pos["prev_fen"], pos["prev_move_uci"], pos["fen"]):
        return None
    return threat_of(pos, me, th)


def gain_of_move(before: Eval, after: Eval) -> float:
    """How much an opponent move (played as if I had passed) costs me: my win chance drop."""
    return before.win_chance() - after.win_chance()
