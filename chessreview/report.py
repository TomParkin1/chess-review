"""Game-report numbers: move marks (chess.com style), accuracy, key moments, time use.

Move marks are display labels inspired by chess.com's Game Review. chess.com's exact
rules are not public, so these are documented approximations:

  book        the position after the move is in the opening database (first 30 plies)
  forced      the only legal move
  brilliant   best/excellent move that leaves a piece (>= knight) en prise, i.e. a sound
              sacrifice, when the mover was not already completely winning
  great       the engine's best move when the second-best move is >= 15 win% points worse
              (the only good move), in a position that was not already decided; obvious
              recaptures don't count
  best        the engine's move, or within 0.5 win% points of it
  excellent   within 2 win% points
  good        anything else below the inaccuracy threshold
  miss        a mistake/blunder right after the opponent's mistake or blunder, when their
              error had left the mover clearly better (>= 60%): the chance was not taken
              (a blunder that loses the game stays a blunder)
  inaccuracy / mistake / blunder   from classify.judge (Lichess thresholds)

Accuracy follows Lichess (lila AccuracyPercent): per move
    103.1668 * exp(-0.04354 * win%_lost) - 3.1669 (+1, clamped 0..100)
and per game the average of a volatility-weighted mean and the harmonic mean.
"""

import math
from dataclasses import dataclass

import chess

from .phases import PIECE_VALUES

MARKS = ["brilliant", "great", "best", "excellent", "good", "book", "forced",
         "inaccuracy", "mistake", "miss", "blunder"]
BOOK_MAX_PLY = 30


def win_pct(win_chance: float) -> float:
    """Lichess win chance (-1..1) to a percentage (0..100)."""
    return 50 + 50 * win_chance


# ------------------------------------------------------------------ marks

def is_sacrifice(board: chess.Board, move: chess.Move) -> bool:
    """Does `move` leave the moved piece (knight or more) where it can be won?

    Static check: after the move the piece is attacked, and either undefended or
    attacked by something cheaper; a capture that already wins as much is not a sacrifice.
    """
    piece = board.piece_at(move.from_square)
    if piece is None or piece.piece_type in (chess.PAWN, chess.KING):
        return False
    value = PIECE_VALUES[piece.piece_type if not move.promotion else move.promotion]
    if value < 3:
        return False
    captured = board.piece_at(move.to_square)
    captured_value = PIECE_VALUES.get(captured.piece_type, 0) if captured else 0
    after = board.copy(stack=False)
    after.push(move)
    attackers = after.attackers(not piece.color, move.to_square)
    if not attackers:
        return False
    cheapest = min(PIECE_VALUES.get(after.piece_at(sq).piece_type, 100) for sq in attackers)
    defended = after.is_attacked_by(piece.color, move.to_square)
    gives_up = value if not defended else value - cheapest
    return gives_up - captured_value >= 2


@dataclass
class MarkInput:
    judgement: str               # good | inaccuracy | mistake | blunder (classify.judge)
    drop_pct: float              # win% points lost by the move, mover's point of view
    before_pct: float            # mover's win% before the move
    after_pct: float
    is_best: bool
    forced: bool
    book: bool
    second_gap_pct: float | None  # best line minus second-best line, mover's win% (None: unknown)
    prev_opponent_judgement: str | None
    sacrifice: bool
    is_recapture: bool = False   # captures on the square the opponent just captured on


def mark_move(m: MarkInput) -> str:
    if m.book:
        return "book"
    if m.forced:
        return "forced"
    if m.judgement != "good":
        chance = m.prev_opponent_judgement in ("mistake", "blunder") and m.before_pct >= 60
        if chance and m.judgement != "inaccuracy" and not (m.judgement == "blunder" and m.after_pct < 40):
            return "miss"
        return m.judgement
    near_best = m.is_best or m.drop_pct <= 2
    if near_best and m.sacrifice and m.before_pct < 90 and m.after_pct >= 45:
        return "brilliant"
    if (m.is_best and not m.is_recapture and m.second_gap_pct is not None
            and m.second_gap_pct >= 15 and 5 < m.before_pct < 95):
        return "great"
    if m.is_best or m.drop_pct <= 0.5:
        return "best"
    if m.drop_pct <= 2:
        return "excellent"
    return "good"


# ------------------------------------------------------------------ accuracy

def move_accuracy(before_pct: float, after_pct: float) -> float:
    """Lichess per-move accuracy from the mover's win% before and after."""
    if after_pct >= before_pct:
        return 100.0
    raw = 103.1668100711649 * math.exp(-0.04354415386753951 * (before_pct - after_pct)) - 3.166924740191411
    return max(0.0, min(100.0, raw + 1))


def game_accuracy(white_pcts: list[float], accuracies: list[float], white_to_move_first: bool = True
                  ) -> tuple[float | None, float | None]:
    """(white accuracy, black accuracy) the Lichess way.

    white_pcts: White's win% for every position (len = moves + 1).
    accuracies: accuracy of each move from its mover's point of view (len = moves).
    """
    n = len(accuracies)
    if n < 2:
        return None, None
    window = max(2, min(8, n // 10))
    padded = [white_pcts[:window]] * (window - 2) + [white_pcts[i:i + window]
                                                     for i in range(len(white_pcts) - window + 1)]
    weights = [max(0.5, min(12.0, _stdev(w))) for w in padded][:n]

    out = []
    for side in (0, 1):   # 0: the side that moved first
        idx = [i for i in range(n) if i % 2 == side]
        if not idx:
            out.append(None)
            continue
        accs = [accuracies[i] for i in idx]
        ws = [weights[i] for i in idx]
        weighted = sum(a * w for a, w in zip(accs, ws)) / sum(ws)
        harmonic = len(accs) / sum(1 / max(a, 1.0) for a in accs)
        out.append(round((weighted + harmonic) / 2, 1))
    first, second = out
    return (first, second) if white_to_move_first else (second, first)


def _stdev(values: list[float]) -> float:
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


# ------------------------------------------------------------------ time use

def parse_time_control(tc: str | None) -> tuple[int | None, int]:
    """'600+5' -> (600000 ms base, 5000 ms increment); daily/unknown -> (None, 0)."""
    if not tc or "/" in tc or tc == "-":
        return None, 0
    base, _, inc = tc.partition("+")
    try:
        return int(base) * 1000, int(inc or 0) * 1000
    except ValueError:
        return None, 0


def time_spent(clocks_ms: str | None, time_control: str | None) -> list[int | None]:
    """Milliseconds each ply took, from remaining clocks ('-' = unknown)."""
    if not clocks_ms:
        return []
    clocks = [None if c == "-" else int(c) for c in clocks_ms.split()]
    base, inc = parse_time_control(time_control)
    spent = []
    for i, clock in enumerate(clocks):
        previous = clocks[i - 2] if i >= 2 else base   # the same side's clock one move earlier
        if clock is None or previous is None:
            spent.append(None)
        else:
            spent.append(max(0, previous - clock + inc))
    return spent
