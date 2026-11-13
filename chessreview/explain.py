"""Rule-based, plain-English explanations of why a move was bad.

Each rule is a function taking a MoveContext and returning (kind, text) or None.
RULES is tried in order and the first match wins; a sentence naming the engine's
best move is appended to every explanation. To add a rule, write a function and
put it in RULES. Bump VERSION when output changes so stored explanations are rebuilt.

Material is counted as pawn=1, knight=bishop=3, rook=5, queen=9. "Material change
along a line" plays the engine's line on a copy of the board and compares the
balance at the end, so recaptures inside the line are taken into account, and an
exchange the (short, stored) line leaves half-done is finished with a static exchange.

The lines come from a short search, so they are trusted only as far as the engine's
own eval agrees: a line that ends a knight up in a position the engine calls equal
doesn't "win a knight" (`_gain_ok`, `_loss_ok`). A bad move has two halves, what it
loses and what the best move would have won: the bigger one leads, and the other is
added when it is a tactic too. A line shown for a claim runs through the capture that
completes it. What counts as a tactic scales with the mover's level (`MoveContext.min_loss`,
from classify.Thresholds.min_loss).
"""

from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING

import chess

from .phases import PIECE_VALUES

if TYPE_CHECKING:
    from .classify import Eval

VERSION = 7
LINE_PLIES = 8  # how far along an engine line to count material
MIN_LOSS = 2  # material swing (in pawns) that counts as a tactic, not positional (default for min_loss)
SLACK = 150  # centipawns an eval may fall short of a line's material before the line isn't trusted
UNDERSTATED = 300  # ...and may exceed it before the line undersells what the move wins
SHOW_PLIES = 4  # plies of a line to show, unless the material it describes comes later
MAX_SHOW_PLIES = 12
STILL_WINNING_CP = 700  # after a missed mate, this much ahead (Lichess' mate rule) still wins


@dataclass
class LineResult:
    net: int  # material gained by `color` (negative = lost)
    lost: list[chess.PieceType]  # my pieces captured along the line
    won: list[chess.PieceType]  # opponent pieces I captured along the line
    end: int = 0  # material balance of `color` at the end of the line
    plies: int = 0  # plies of the line that reach `net` (through its last capture)


@dataclass
class MoveContext:
    board: chess.Board  # position before my move (me to move)
    move: chess.Move  # the move I played
    best: chess.Move | None  # engine's best move in that position
    best_line: list[chess.Move]  # engine line starting with `best`
    reply_line: list[chess.Move]  # engine line for the opponent after my move
    before: Eval  # eval before my move, my point of view
    after: Eval  # eval after my move, my point of view
    prev_capture: chess.Square | None = None  # where the opponent's last move captured (None: it didn't)
    min_loss: int = MIN_LOSS  # material (pawns) that counts as a tactic at the mover's level

    @property
    def me(self) -> chess.Color:
        return self.board.turn

    def board_after(self) -> chess.Board:
        b = self.board.copy(stack=False)
        b.push(self.move)
        return b

    @property
    def recapture(self) -> bool:
        """My move takes back where the opponent just captured, or takes what the best move takes
        too (with another piece): either way the piece it wins is no gain of its own."""
        if not self.board.is_capture(self.move):
            return False
        return self.move.to_square in (self.prev_capture, self.best.to_square if self.best else None)

    @cached_property
    def loss(self) -> LineResult:
        """What the opponent wins after my move (net < 0). Counted from after my move when it takes
        back (the piece it wins back only restores the balance), else from before it, so a pawn
        my move grabs counts."""
        if self.recapture:
            return play_line(self.board_after(), self.reply_line, self.me)
        return play_line(self.board, [self.move] + self.reply_line, self.me)

    @cached_property
    def gain(self) -> LineResult:
        """What the engine's best line wins for me."""
        return play_line(self.board, self.best_line, self.me)

    @property
    def lost(self) -> int:
        return -self.loss.net

    @property
    def missed(self) -> int:
        """What the best line wins, apart from taking back what the opponent just captured."""
        return self.gain.net - (_taken_value(self.board, self.move) if self.recapture else 0)

    def loss_line(self) -> str:
        """The opponent's line after my move, as far as the material I lose."""
        own = 0 if self.recapture else 1  # the loss line starts with my own move unless it took back
        return san_line(self.board_after(), self.reply_line, _shown(self.loss.plies - own))

    def gain_line(self) -> str:
        return san_line(self.board, self.best_line, _shown(self.gain.plies))

    def best_line_text(self, plies: int = SHOW_PLIES) -> str:
        """The best line for display. When the eval doesn't back the material it ends with, only
        up to its first capture: the short search's later moves aren't to be trusted."""
        if not _gain_ok(self):
            plies = min(plies, _until_capture(self.board, self.best_line))
        return san_line(self.board, self.best_line, plies)


# A rule's answer: (kind of mistake, explanation), or None when the rule doesn't apply.
RuleResult = tuple[str, str] | None

# ---------------------------------------------------------------- helpers


def value(piece_type: chess.PieceType | None) -> int:
    return PIECE_VALUES.get(piece_type, 0) if piece_type else 0


def _name_at(board: chess.Board, square: chess.Square) -> str:
    """Name of the piece on `square`, which must hold one ("knight")."""
    piece_type = board.piece_type_at(square)
    assert piece_type is not None, chess.square_name(square)
    return chess.piece_name(piece_type)


def balance(board: chess.Board, color: chess.Color) -> int:
    """Material of `color` minus material of the opponent."""
    total = 0
    for pt, v in PIECE_VALUES.items():
        total += v * (len(board.pieces(pt, color)) - len(board.pieces(pt, not color)))
    return total


def _taken(board: chess.Board, move: chess.Move) -> chess.PieceType | None:
    """The piece type `move` captures (None if it isn't a capture)."""
    if board.is_en_passant(move):
        return chess.PAWN
    return board.piece_type_at(move.to_square) if board.is_capture(move) else None


def _taken_value(board: chess.Board, move: chess.Move) -> int:
    return value(_taken(board, move))


def play_line(
    board: chess.Board, line: list[chess.Move], color: chess.Color, max_plies: int = LINE_PLIES
) -> LineResult:
    """Play an engine line on a copy of `board` and count material for `color`.

    Stops after max_plies, but keeps going while captures continue so that an exchange is
    not cut off half-way. A line that ends with a capture (the stored lines are short) is
    finished with a static exchange on that square, unless that capture loses material on
    the spot: then it is the tail of a short search, and isn't counted.
    """
    b = board.copy(stack=False)
    start = balance(b, color)
    lost: list[chess.PieceType] = []
    won: list[chess.PieceType] = []
    path: list[int] = []
    last_taken = None
    for i, move in enumerate(line):
        if i >= max_plies and not b.is_capture(move):
            break
        if move not in b.legal_moves:
            break
        last_taken = _taken(b, move)
        _push(b, move, color, won, lost)
        path.append(balance(b, color) - start)
    if last_taken:
        back, replies = exchange(b, b.peek().to_square)
        if back > value(last_taken):
            b.pop()
            (won if b.turn == color else lost).remove(last_taken)
            path.pop()
        else:
            for move in replies:
                _push(b, move, color, won, lost)
    net = balance(b, color) - start
    return LineResult(net, lost, won, balance(b, color), _plies_to(path, net))


def _push(
    board: chess.Board, move: chess.Move, color: chess.Color, won: list[chess.PieceType], lost: list[chess.PieceType]
) -> None:
    """Play `move`, noting the piece it captures."""
    captured = _taken(board, move)
    if captured:
        (won if board.turn == color else lost).append(captured)
    board.push(move)


def exchange(board: chess.Board, square: chess.Square) -> tuple[int, list[chess.Move]]:
    """Static exchange on `square`: (material the side to move wins there, the captures played).

    The least valuable attacker captures first, and either side stops when going on would lose.
    """
    captures = [
        m for m in board.legal_moves if m.to_square == square and board.is_capture(m) and not board.is_en_passant(m)
    ]
    if not captures:
        return 0, []
    move = min(captures, key=lambda m: value(board.piece_type_at(m.from_square)) or 100)  # the king last
    taken = value(board.piece_type_at(square))
    b = board.copy(stack=False)
    b.push(move)
    back, rest = exchange(b, square)
    if taken - back <= 0:
        return 0, []
    return taken - back, [move] + rest


def _plies_to(path: list[int], net: int) -> int:
    """How many plies of a line reach its material result for good: through the capture that
    completes it (all of the line when the result only comes from finishing an exchange after it)."""
    if net == 0:
        return 0
    for i, change in enumerate(path):
        if change == net and all((later - net) * net >= 0 for later in path[i:]):
            return i + 1
    return len(path)


def _until_capture(board: chess.Board, line: list[chess.Move]) -> int:
    """Plies of the line before its first capture (at least one)."""
    b = board.copy(stack=False)
    for i, move in enumerate(line):
        if move not in b.legal_moves or b.is_capture(move):
            return max(i, 1)
        b.push(move)
    return len(line)


def _shown(plies: int) -> int:
    return min(MAX_SHOW_PLIES, max(SHOW_PLIES, plies))


def material_change(board: chess.Board, line: list[chess.Move], color: chess.Color) -> int:
    return play_line(board, line, color).net


def _piece_list(types: list[chess.PieceType]) -> str:
    """[KNIGHT, PAWN, PAWN] -> 'a knight and two pawns'."""
    words = {1: "a", 2: "two", 3: "three", 4: "four"}
    parts = []
    for pt in sorted(set(types), key=value, reverse=True):
        n = types.count(pt)
        name = chess.piece_name(pt)
        parts.append(f"{words.get(n, n)} {name}{'s' if n > 1 else ''}")
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _traded_out(give: list[chess.PieceType], get: list[chess.PieceType]) -> tuple[list, list]:
    """Both lists without equal trades (a knight for a knight is not worth mentioning)."""
    give, get = list(give), list(get)
    for pt in list(give):
        if pt in get:
            give.remove(pt)
            get.remove(pt)
    return give, get


def describe_trade(result: LineResult, losing: bool = True) -> str:
    """'a knight for a pawn' from the point of view of the side losing material."""
    give, get = _traded_out(*((result.lost, result.won) if losing else (result.won, result.lost)))
    if not give:
        return describe_material(abs(result.net))
    text = _piece_list(give)
    return f"{text} for {_piece_list(get)}" if get else text


def san_line(board: chess.Board, line: list[chess.Move], plies: int = 4) -> str:
    """'12. Nf3 Bxf3 13. gxf3' for the first few moves of a line."""
    b = board.copy(stack=False)
    legal = []
    for move in line[:plies]:
        if move not in b.legal_moves:
            break
        legal.append(move)
        b.push(move)
    return board.variation_san(legal) if legal else ""


def describe_material(points: int) -> str:
    if points <= 1:
        return "a pawn"
    if points == 2:
        return "two pawns"
    if points <= 4:
        return "a piece"
    if points <= 6:
        return "a rook's worth of material"
    if points <= 8:
        return "a rook and a piece's worth of material"
    return "a queen's worth of material"


def piece_on(board: chess.Board, square: chess.Square) -> str:
    piece = board.piece_at(square)
    name = chess.piece_name(piece.piece_type) if piece else "piece"
    return f"{name} on {chess.square_name(square)}"


def _san(board: chess.Board, move: chess.Move | None) -> str:
    return board.san(move) if move and move in board.legal_moves else "?"


# ---------------------------------------------------------------- is the line's material real?


def _gain_ok(ctx: MoveContext) -> bool:
    """The eval with the best move agrees with the material its line ends with. A short search's
    line can end a knight up in a position the same search calls equal: then no knight is won."""
    if ctx.before.mate is not None:
        return ctx.before.mate > 0
    return (ctx.before.cp or 0) >= 100 * ctx.gain.end - SLACK


def _understated(ctx: MoveContext) -> bool:
    """The eval with the best move is far above what its line wins: the line undersells it."""
    return ctx.before.mate is None and (ctx.before.cp or 0) > 100 * ctx.gain.end + UNDERSTATED


def _loss_ok(ctx: MoveContext) -> bool:
    """The eval after my move agrees that I end up with the material the opponent's line leaves me."""
    if ctx.after.mate is not None:
        return ctx.after.mate < 0
    return (ctx.after.cp or 0) <= 100 * ctx.loss.end + SLACK


def _more_than_material(ctx: MoveContext) -> bool:
    """The material (lost, and missed) explains less than half of the drop in the eval: naming
    only a pawn would make it look like the whole story."""
    drop = ctx.before.as_cp() - ctx.after.as_cp()
    explained = 100 * (ctx.lost + (ctx.missed if _missed_ok(ctx) else 0))
    return drop > 2 * explained and drop - explained > UNDERSTATED


def _you_lose(ctx: MoveContext) -> str:
    """'You lose a knight.', plus the drop in the eval and the line when that is more than the material."""
    lost = describe_trade(ctx.loss)
    if not _more_than_material(ctx):
        return f"You lose {lost}."
    line = san_line(ctx.board_after(), ctx.reply_line, MAX_SHOW_PLIES // 2)
    return f"You lose {lost}, and the evaluation drops from {ctx.before} to {ctx.after}: {line}."


def _and_drop(ctx: MoveContext) -> str:
    """For texts that already show the line: the drop in the eval, when that is more than the material."""
    return f" The evaluation drops from {ctx.before} to {ctx.after}." if _more_than_material(ctx) else ""


def _lost(ctx: MoveContext, at_least: int) -> bool:
    """My move loses at least `at_least` pawns of material, and the eval agrees."""
    return ctx.lost >= at_least and _loss_ok(ctx)


def _missed_ok(ctx: MoveContext) -> bool:
    """The best move wins at least min_loss of material that my move doesn't, and the eval agrees."""
    return (
        ctx.best is not None
        and bool(ctx.best_line)
        and ctx.missed >= ctx.min_loss
        and ctx.missed + ctx.lost >= ctx.min_loss
        and _gain_ok(ctx)
    )


def _beyond(ctx: MoveContext) -> LineResult:
    """The best line's result without the piece my move takes too (the same capture with another
    piece, or a recapture that only restores the balance)."""
    gain = ctx.gain
    same = ctx.best is not None and ctx.move.to_square == ctx.best.to_square
    taken = _taken(ctx.board, ctx.move)
    if taken is None or not (ctx.recapture or same):
        return gain
    won = list(gain.won)
    if taken in won:
        won.remove(taken)
    return LineResult(gain.net - value(taken), gain.lost, won, gain.end, gain.plies)


def _in_danger(board: chess.Board, square: chess.Square, victim: chess.Color) -> list[chess.Square]:
    """Pieces of `victim` that the piece on `square` attacks and that are really in danger: the
    king, or a piece (not a pawn) worth more than the attacker or undefended."""
    attacker_value = value(board.piece_type_at(square))
    targets = []
    for sq in board.attacks(square):
        target = board.piece_at(sq)
        if not target or target.color != victim:
            continue
        if target.piece_type == chess.KING or (
            value(target.piece_type) >= 3
            and (value(target.piece_type) > attacker_value or not board.is_attacked_by(victim, sq))
        ):
            targets.append(sq)
    return targets


def _takes_piece(board: chess.Board, move: chess.Move) -> bool:
    """The move captures a piece, not just a pawn."""
    return _taken_value(board, move) > 1


# ---------------------------------------------------------------- rules


def missed_mate(ctx: MoveContext) -> RuleResult:
    if ctx.before.mate and ctx.before.mate > 0 and not (ctx.after.mate and ctx.after.mate > 0):
        best = _san(ctx.board, ctx.best)
        if ctx.before.mate == 1:
            text = f"You missed checkmate in one: {best}."
        elif ctx.before.mate > 3:
            text = f"You had a forced checkmate in {ctx.before.mate}, starting {san_line(ctx.board, ctx.best_line, 3)}."
        else:
            line = san_line(ctx.board, ctx.best_line, 2 * ctx.before.mate - 1)
            text = f"You had a forced checkmate in {ctx.before.mate}, starting with {best}: {line}."
        if ctx.after.mate is None and (ctx.after.cp or 0) >= STILL_WINNING_CP:
            text += " Your move should still win."
        return "missed_mate", text
    return None


def allowed_mate(ctx: MoveContext) -> RuleResult:
    if ctx.after.mate and ctx.after.mate < 0 and not (ctx.before.mate and ctx.before.mate < 0):
        after = ctx.board_after()
        n = -ctx.after.mate
        line = san_line(after, ctx.reply_line, 2 * n)
        if n == 1:
            return "allowed_mate", f"This allows checkmate in one: {line}."
        if n > 3:
            return "allowed_mate", (
                f"This walks into a forced checkmate in {n}, starting {san_line(after, ctx.reply_line, 3)}."
            )
        return "allowed_mate", f"This walks into a forced checkmate in {n}: {line}."
    return None


def bigger_miss(ctx: MoveContext) -> RuleResult:
    """The best move wins more than my move loses: the miss is the lesson, and a loss of my
    own that is a tactic too is added."""
    if not _missed_ok(ctx) or ctx.lost > ctx.missed:
        return None
    kind, text = _missed(ctx)
    if _lost(ctx, ctx.min_loss):
        text += f" Your move also loses {describe_trade(ctx.loss)}: {ctx.loss_line()}."
    return kind, text


def walked_into_fork(ctx: MoveContext) -> RuleResult:
    if not ctx.reply_line:
        return None
    after = ctx.board_after()
    reply = ctx.reply_line[0]
    if reply not in after.legal_moves or _takes_piece(after, reply):
        return None  # that is a capture, handled by the hanging-piece rule
    if not _lost(ctx, ctx.min_loss):
        return None

    reply_san = after.san(reply)
    after.push(reply)
    targets = _in_danger(after, reply.to_square, ctx.me)
    if len(targets) < 2:
        return None
    names = [_name_at(after, sq) for sq in targets]
    attacked = " and ".join(f"your {n}" for n in names)
    return "fork", f"This walks into a fork: {reply_san} attacks {attacked}. {_you_lose(ctx)}"


def discovered_attack(ctx: MoveContext) -> RuleResult:
    """The opponent's reply moves a piece out of the way of one of their long-range
    pieces, which then attacks something valuable of mine. A reply that simply takes a
    piece is a capture, not a discovered attack (hanging_piece explains it)."""
    if not ctx.reply_line:
        return None
    after = ctx.board_after()
    reply = ctx.reply_line[0]
    if reply not in after.legal_moves or _takes_piece(after, reply):
        return None
    if not _lost(ctx, ctx.min_loss):
        return None

    them = not ctx.me
    sliders = [
        sq for pt in (chess.BISHOP, chess.ROOK, chess.QUEEN) for sq in after.pieces(pt, them) if sq != reply.from_square
    ]
    attacked_before = {sq: after.attacks(sq) for sq in sliders}
    reply_san = after.san(reply)
    after.push(reply)
    candidates = []  # (target value, slider square, target square)
    for sq in sliders:
        slider = after.piece_at(sq)
        if slider is None or slider.color != them:  # it was captured by the reply
            continue
        for target_sq in after.attacks(sq) & ~attacked_before[sq]:
            target = after.piece_at(target_sq)
            if (
                target
                and target.color == ctx.me
                and target.piece_type != chess.KING
                and value(target.piece_type) >= 3
                and (value(target.piece_type) > value(slider.piece_type) or not after.is_attacked_by(ctx.me, target_sq))
            ):
                candidates.append((value(target.piece_type), sq, target_sq))
    if not candidates:
        return None
    _, sq, target_sq = max(candidates)
    return "discovered_attack", (
        f"This allows a discovered attack: {reply_san} uncovers the "
        f"{piece_on(after, sq)}, which now attacks your "
        f"{piece_on(after, target_sq)}. {_you_lose(ctx)}"
    )


def hanging_piece(ctx: MoveContext) -> RuleResult:
    if not ctx.reply_line:
        return None
    after = ctx.board_after()
    reply = ctx.reply_line[0]
    if reply not in after.legal_moves or not after.is_capture(reply) or not _lost(ctx, 1):
        return None

    lost = describe_trade(ctx.loss)
    target_sq = reply.to_square
    target = after.piece_at(target_sq)
    worst = max(map(value, _traded_out(ctx.loss.lost, ctx.loss.won)[0]), default=0)
    if target is None or value(target.piece_type) < worst:
        # The first capture isn't what hurts (a pawn taken on the way to a piece): show the line.
        return "loses_material", f"This lets your opponent win {lost}: {ctx.loss_line()}.{_and_drop(ctx)}"
    defended = after.is_attacked_by(ctx.me, target_sq)
    reply_san = after.san(reply)
    name = chess.piece_name(target.piece_type)
    you_lose = _you_lose(ctx)
    if target_sq == ctx.move.to_square:
        if defended:
            return "hanging_piece", (
                f"Your {piece_on(after, target_sq)} isn't safe there: {reply_san} wins "
                f"material even though it is defended. {you_lose}"
            )
        return "hanging_piece", (
            f"You moved your {name} to {chess.square_name(target_sq)}, "
            f"where it can simply be taken ({reply_san}). {you_lose}"
        )
    if defended:
        return "loses_material", (
            f"This lets your opponent win material with {reply_san} ({ctx.loss_line()}). {you_lose}"
        )
    why = _why_loose(ctx, target_sq)
    if why:
        return "hanging_piece", f"{why}: after {ctx.board.san(ctx.move)}, {reply_san} takes it. {you_lose}"
    if ctx.recapture:
        moved = _name_at(ctx.board, ctx.move.from_square)
        taking = (
            "Taking back"
            if ctx.prev_capture == ctx.move.to_square
            else f"Taking on {chess.square_name(ctx.move.to_square)}"
        )
        return "hanging_piece", (
            f"{taking} with the {moved} leaves your {piece_on(after, target_sq)} "
            f"undefended, and {reply_san} takes it. {you_lose}"
        )
    return "hanging_piece", (
        f"This leaves your {piece_on(after, target_sq)} undefended, and {reply_san} takes it. {you_lose}"
    )


def _why_loose(ctx: MoveContext, target_sq: chess.Square) -> str | None:
    """Why my piece on target_sq can be taken after my move: the piece I moved was shielding it
    from the capturing piece's line, or was the piece guarding it."""
    reply = ctx.reply_line[0]
    frm = ctx.move.from_square
    moved = f"{_name_at(ctx.board, frm)} on {chess.square_name(frm)}"
    target = piece_on(ctx.board, target_sq)
    capturer = ctx.board.piece_at(reply.from_square)
    if (
        capturer
        and capturer.piece_type in (chess.BISHOP, chess.ROOK, chess.QUEEN)
        and frm in chess.SquareSet(chess.between(reply.from_square, target_sq))
    ):
        return f"Your {moved} was shielding your {target} from the {chess.piece_name(capturer.piece_type)}"
    if target_sq in ctx.board.attacks(frm):
        return f"Your {moved} was guarding your {target}"
    return None


def loses_material(ctx: MoveContext) -> RuleResult:
    if not _lost(ctx, ctx.min_loss):
        return None
    return "loses_material", f"This loses {describe_trade(ctx.loss)} by force: {ctx.loss_line()}.{_and_drop(ctx)}"


def missed_win(ctx: MoveContext) -> RuleResult:
    if not _missed_ok(ctx):
        return None
    return _missed(ctx)


def _missed(ctx: MoveContext) -> tuple[str, str]:
    """'You missed …' for the material the best move wins and how, plus the eval it keeps when that
    is worth far more than the material (a passed pawn or an attack the count doesn't show)."""
    kind, text = _missed_material(ctx)
    if _understated(ctx):
        text += f" It keeps you at {ctx.before}: more than the material shows."
    return kind, text


def _missed_material(ctx: MoveContext) -> tuple[str, str]:
    board, best = ctx.board, ctx.best
    assert best is not None  # only asked when the best move wins material
    best_san = _san(board, best)
    won = describe_trade(_beyond(ctx), losing=False)
    line = ctx.gain_line()
    if not board.is_capture(best):
        return "missed_tactic", f"You missed a tactic: {best_san} wins {won} ({line})."
    after_best = board.copy(stack=False)
    after_best.push(best)
    if board.is_capture(ctx.move) and ctx.move.to_square == best.to_square:
        # Same capture, other piece: what matters is what the best one does next.
        mine = _name_at(board, ctx.move.from_square)
        threats = _new_threats(after_best, best, ctx.me)
        how = f"it also attacks {threats} and" if threats else "it"
        return "missed_capture", (
            f"You took on {chess.square_name(best.to_square)} with the {mine}, but "
            f"{best_san} was stronger: {how} wins {won}: {line}."
        )
    free = not board.is_attacked_by(not ctx.me, best.to_square)
    if free and _taken_value(board, best) >= ctx.missed:
        return "missed_capture", (
            f"You missed {best_san}: the {piece_on(board, best.to_square)} was undefended, so you could win {won}."
        )
    return "missed_capture", f"You missed {best_san}, which wins {won}: {line}."


def _also_missed(ctx: MoveContext) -> str:
    """The other half of a move that lost material: the best move would also have won some."""
    if not _missed_ok(ctx):
        return ""
    return (
        f" You also missed {_san(ctx.board, ctx.best)}, which wins "
        f"{describe_trade(_beyond(ctx), losing=False)}: {ctx.gain_line()}."
    )


def missed_fork(ctx: MoveContext) -> RuleResult:
    """One of my first moves in the best line forks the king and a piece, or two pieces, and the
    eval says material is at stake even when the short line doesn't show it being won."""
    if not ctx.best_line or ctx.before.mate is not None:
        return None
    them = not ctx.me
    if (ctx.before.cp or 0) < 100 * (balance(ctx.board, ctx.me) + ctx.min_loss) - SLACK:
        return None
    b = ctx.board.copy(stack=False)
    for i, move in enumerate(ctx.best_line[:3]):  # my first two moves: later forks are speculation
        if move not in b.legal_moves:
            return None
        b.push(move)
        if i % 2:
            continue
        targets = _in_danger(b, move.to_square, them)
        if len(targets) >= 2 and _fork_holds(b, move.to_square):
            return "missed_tactic", (
                f"You missed a fork: {san_line(ctx.board, ctx.best_line, i + 1)} attacks {_their(b, targets)}."
            )
    return None


def _fork_holds(board: chess.Board, square: chess.Square) -> bool:
    """The forking piece can't simply be taken: taking it (the side to move) loses material,
    an even trade included (that answers the fork)."""
    captures = [m for m in board.legal_moves if m.to_square == square]
    if not captures:
        return True
    move = min(captures, key=lambda m: value(board.piece_type_at(m.from_square)) or 100)
    b = board.copy(stack=False)
    b.push(move)
    return value(board.piece_type_at(square)) - exchange(b, square)[0] < 0


def _their(board: chess.Board, squares: list[chess.Square]) -> str:
    """'the king and the rook on a1'."""
    ordered = sorted(squares, key=lambda sq: value(board.piece_type_at(sq)) or 100, reverse=True)
    names = ["the king" if board.piece_type_at(sq) == chess.KING else f"the {piece_on(board, sq)}" for sq in ordered]
    return " and ".join(names)


def exposed_king(ctx: MoveContext) -> RuleResult:
    """The opponent's best reply is a check that hurts (but no forced mate or material loss):
    my king has to move, or the checks go on. A check I simply block or take back (a trade
    with check, a queen trade offered with check) does nothing to my king."""
    if not ctx.reply_line:
        return None
    after = ctx.board_after()
    reply = ctx.reply_line[0]
    if reply not in after.legal_moves or not after.gives_check(reply) or not _king_hunted(after, ctx.reply_line):
        return None
    return "king_safety", (
        f"This exposes your king: {san_line(after, ctx.reply_line)} "
        f"and the evaluation drops from {ctx.before} to {ctx.after}."
    )


def _king_hunted(board: chess.Board, line: list[chess.Move], plies: int = SHOW_PLIES) -> bool:
    """In the first plies of the opponent's line (starting with a check), my king has to move or
    is checked again."""
    b = board.copy(stack=False)
    me = not b.turn
    checks = 0
    for move in line[:plies]:
        if move not in b.legal_moves:
            break
        if b.turn == me and b.piece_type_at(move.from_square) == chess.KING:
            return True
        checks += b.turn != me and b.gives_check(move)
        b.push(move)
    return checks >= 2


def missed_opportunity(ctx: MoveContext) -> RuleResult:
    """I was clearly better and let most of the advantage slip without losing material."""
    if ctx.before.win_chance() < 0.3 or ctx.after.win_chance() < -0.2 or not ctx.best:
        return None
    line = ctx.best_line_text()
    return "missed_opportunity", (
        f"You were clearly better ({ctx.before}) but this lets the advantage slip ({ctx.after}). Stronger was {line}."
    )


def positional(ctx: MoveContext) -> RuleResult:
    after = ctx.board_after()
    reply = san_line(after, ctx.reply_line)
    if ctx.before.win_chance() >= 0.3 and ctx.after.win_chance() <= -0.3:
        start = f"; the trouble starts with {reply}" if reply else ""
        return "positional", (f"This turns a winning position ({ctx.before}) into a losing one ({ctx.after}){start}.")
    threat = f" after {reply}" if reply else ""
    return "positional", (
        f"No simple tactic found, but your position gets worse{threat} ({ctx.before} to {ctx.after})."
    )


RULES = [
    missed_mate,
    allowed_mate,
    bigger_miss,
    walked_into_fork,
    discovered_attack,
    hanging_piece,
    loses_material,
    missed_win,
    missed_fork,
    exposed_king,
    missed_opportunity,
    positional,
]


# Wording for "missed_*" kinds that does not give the answer away (review puzzles).
SPOILER_FREE = {
    "missed_mate": "There was a forced checkmate, and this move misses it.",
    "missed_capture": "This misses a chance to win material.",
    "missed_tactic": "This misses a tactic that wins material.",
}
# Explanations of material my move lost; the best move's own win is added to them.
MATERIAL_KINDS = {"fork", "discovered_attack", "hanging_piece", "loses_material"}


def explain(ctx: MoveContext, reveal_best: bool = True) -> tuple[str, str]:
    """Return (kind, explanation) for a move judged to be an inaccuracy or worse.

    With reveal_best=False (judging my attempts at a puzzle) the text never names the
    engine's best move.
    """
    for rule in RULES:
        result = rule(ctx)
        if result:
            kind, text = result
            break
    if not reveal_best:
        if kind in SPOILER_FREE:
            text = SPOILER_FREE[kind]
        elif kind == "missed_opportunity":
            text = f"You were clearly better ({ctx.before}) but this lets the advantage slip ({ctx.after})."
        return kind, text
    if kind in MATERIAL_KINDS:
        text += _also_missed(ctx)
    best_san = _san(ctx.board, ctx.best)
    if (
        ctx.best
        and kind not in ("missed_mate", "missed_capture", "missed_tactic", "missed_opportunity")
        and f"missed {best_san}" not in text
    ):
        text += f" Best was {best_san}."
    return kind, text


# ---------------------------------------------------------------- hints for review mode

HINTS = {
    "missed_mate": "There's a forced checkmate. Look at every check.",
    "missed_capture": "You can win material right away. Look at what is undefended.",
    "missed_tactic": "There's a tactic that wins material: look for checks, captures and threats.",
    "allowed_mate": "Your king is in danger. What is your opponent threatening?",
    "fork": "Careful: your opponent has a fork available. Which squares do their knights and queen reach?",
    "discovered_attack": "Careful: a discovered attack is possible. Look at the lines behind their pieces.",
    "hanging_piece": "Make sure none of your pieces is left undefended.",
    "loses_material": "Your move lost material. Check every capture your opponent has.",
    "king_safety": "Keep your king safe: which checks does your opponent have?",
    "missed_opportunity": (
        "You're clearly better here. Find the move that keeps the advantage, often the most forcing one."
    ),
    "positional": "Before moving, ask what your opponent wants to do next.",
}

# Mistakes where the fix is attacking: these get a longer play-out in review mode.
ATTACKING_KINDS = {"missed_mate", "missed_capture", "missed_tactic"}


def hint(kind: str | None) -> str:
    return HINTS.get(kind or "", "Think about your opponent's best reply before you move.")


# ---------------------------------------------------------------- why the best move works

DEFENSIVE_KINDS = {"allowed_mate", "hanging_piece", "fork", "discovered_attack", "loses_material", "king_safety"}


def explain_best(ctx: MoveContext, kind: str | None) -> str:
    """Plain-English reason the engine's move is better than mine (shown after a puzzle).

    `kind` is the explanation category of my move, which tells us whether the fix was
    about defending (my move allowed something) or attacking (I missed something).
    """
    if ctx.best is None or ctx.best not in ctx.board.legal_moves:
        return ""
    board = ctx.board
    best_san = board.san(ctx.best)
    line = ctx.best_line_text(6)
    after_best = board.copy(stack=False)
    after_best.push(ctx.best)

    if ctx.before.mate and ctx.before.mate > 0:
        if kind == "missed_mate":
            return ""  # the explanation of my move already shows the mate
        return f"{best_san} starts a forced checkmate in {ctx.before.mate}: {line}."

    # The explanation of my move already names the best move and what it wins (missed_*),
    # or its line (missed_opportunity): only add what it doesn't say.
    said_gain = kind in ("missed_capture", "missed_tactic")
    sentences = []
    gain = ctx.gain
    if kind in DEFENSIVE_KINDS and ctx.reply_line:
        sentences.append(_what_best_prevents(ctx, best_san, after_best))
    elif gain.net >= 1 and not said_gain and _gain_ok(ctx) and not _understated(ctx):
        # (an amount the eval doesn't back, or one far below it, would teach the wrong size of the win)
        target = board.piece_at(ctx.best.to_square) if board.is_capture(ctx.best) else None
        free = target is not None and not board.is_attacked_by(not ctx.me, ctx.best.to_square)
        if target is not None and free and value(target.piece_type) >= gain.net:
            sentences.append(f"{best_san} simply takes the undefended {chess.piece_name(target.piece_type)}.")
        else:
            shown = "." if kind == "missed_opportunity" else f": {ctx.gain_line()}."  # that text shows the line
            sentences.append(f"{best_san} wins {describe_trade(gain, losing=False)}{shown}")
    sentences = [s for s in sentences if s]
    threats = _new_threats(after_best, ctx.best, ctx.me)
    if threats and not (said_gain and board.is_capture(ctx.best)):
        sentences.append(f"{best_san} attacks {threats}." if not sentences else f"It also attacks {threats}.")
    if not sentences and not said_gain and kind != "missed_opportunity":
        sentences.append(f"{best_san} keeps the position at {ctx.before} for you. The main line goes {line}.")
    return " ".join(sentences)


def _what_best_prevents(ctx: MoveContext, best_san: str, after_best: chess.Board) -> str:
    """Describe how the best move deals with the reply my move allowed."""
    after_mine = ctx.board_after()
    threat, best = ctx.reply_line[0], ctx.best
    if best is None or threat not in after_mine.legal_moves:
        return ""
    threat_san = after_mine.san(threat)
    if ctx.after.mate and ctx.after.mate < 0:
        return f"{best_san} keeps your king safe: after your move, {threat_san} started a forced mate."
    target_sq = threat.to_square
    target = after_mine.piece_at(target_sq)
    if target and target.color == ctx.me and target.piece_type != chess.KING:
        name = chess.piece_name(target.piece_type)
        moved_from = best.from_square
        if moved_from == target_sq or (ctx.move.to_square == target_sq and moved_from == ctx.move.from_square):
            return f"{best_san} keeps your {name} safe instead of leaving it to {threat_san}."
        if after_best.piece_at(target_sq) != target or after_mine.is_attacked_by(ctx.me, target_sq):
            return ""  # the piece isn't there after the best move, or my move didn't leave it loose
        guards = after_best.attackers(ctx.me, target_sq)
        if len(ctx.best_line) > 1 and ctx.best_line[1].to_square in guards:
            return ""  # the guard is taken straight away: guarding isn't what the best move is about
        if best.to_square in guards:
            return f"{best_san} defends your {name}, so {threat_san} no longer wins it."
        if ctx.move.from_square in guards:  # the piece my move took away still guards it
            return (
                f"{best_san} keeps your {piece_on(after_best, ctx.move.from_square)} where it guards "
                f"your {piece_on(after_best, target_sq)}, so {threat_san} doesn't work."
            )
    return ""  # nothing to add to the explanation of my move, which shows the line it allowed


def _new_threats(after_best: chess.Board, best: chess.Move, me: chess.Color) -> str:
    """Opponent pieces newly attacked by the moved piece that are undefended or worth more."""
    piece = after_best.piece_at(best.to_square)
    if piece is None:
        return ""
    found = []
    for sq in after_best.attacks(best.to_square):
        target = after_best.piece_at(sq)
        if not target or target.color == me or target.piece_type in (chess.PAWN, chess.KING):
            continue
        if value(target.piece_type) > value(piece.piece_type) or not after_best.is_attacked_by(not me, sq):
            found.append(f"the {piece_on(after_best, sq)}")
    return " and ".join(found[:2])
