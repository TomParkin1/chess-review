"""Rule-based, plain-English explanations of why a move was bad.

Each rule is a function taking a MoveContext and returning (kind, text) or None.
RULES is tried in order and the first match wins; a sentence naming the engine's
best move is appended to every explanation. To add a rule, write a function and
put it in RULES. Bump VERSION when output changes so stored explanations are rebuilt.

Material is counted as pawn=1, knight=bishop=3, rook=5, queen=9. "Material change
along a line" plays the engine's line on a copy of the board and compares the
balance at the end, so recaptures inside the line are taken into account.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

import chess

from .phases import PIECE_VALUES

if TYPE_CHECKING:
    from .classify import Eval

VERSION = 6
LINE_PLIES = 8        # how far along an engine line to count material
MIN_LOSS = 2          # material swing (in pawns) that counts as a tactic, not positional


@dataclass
class MoveContext:
    board: chess.Board                 # position before my move (me to move)
    move: chess.Move                   # the move I played
    best: chess.Move | None            # engine's best move in that position
    best_line: list[chess.Move]        # engine line starting with `best`
    reply_line: list[chess.Move]       # engine line for the opponent after my move
    before: "Eval"                     # eval before my move, my point of view
    after: "Eval"                      # eval after my move, my point of view

    @property
    def me(self) -> chess.Color:
        return self.board.turn

    def board_after(self) -> chess.Board:
        b = self.board.copy(stack=False)
        b.push(self.move)
        return b


# ---------------------------------------------------------------- helpers

def value(piece_type: chess.PieceType) -> int:
    return PIECE_VALUES.get(piece_type, 0)


def balance(board: chess.Board, color: chess.Color) -> int:
    """Material of `color` minus material of the opponent."""
    total = 0
    for pt, v in PIECE_VALUES.items():
        total += v * (len(board.pieces(pt, color)) - len(board.pieces(pt, not color)))
    return total


@dataclass
class LineResult:
    net: int                          # material gained by `color` (negative = lost)
    lost: list[chess.PieceType]       # my pieces captured along the line
    won: list[chess.PieceType]        # opponent pieces I captured along the line


def play_line(board: chess.Board, line: list[chess.Move], color: chess.Color,
              max_plies: int = LINE_PLIES) -> LineResult:
    """Play an engine line on a copy of `board` and count material for `color`.

    Stops after max_plies, but keeps going while captures continue so that an
    exchange is not cut off half-way.
    """
    b = board.copy(stack=False)
    start = balance(b, color)
    lost, won = [], []
    for i, move in enumerate(line):
        if i >= max_plies and not b.is_capture(move):
            break
        if move not in b.legal_moves:
            break
        if b.is_capture(move):
            captured = chess.PAWN if b.is_en_passant(move) else b.piece_at(move.to_square).piece_type
            (won if b.turn == color else lost).append(captured)
        b.push(move)
    return LineResult(balance(b, color) - start, lost, won)


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


def describe_trade(result: LineResult, losing: bool = True) -> str:
    """'a knight for a pawn' from the point of view of the side losing material."""
    give, get = (result.lost, result.won) if losing else (result.won, result.lost)
    # Cancel out equal trades (a knight for a knight is not worth mentioning).
    give, get = list(give), list(get)
    for pt in list(give):
        if pt in get:
            give.remove(pt)
            get.remove(pt)
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


# ---------------------------------------------------------------- rules

def missed_mate(ctx: MoveContext):
    if ctx.before.mate and ctx.before.mate > 0 and not (ctx.after.mate and ctx.after.mate > 0):
        best = _san(ctx.board, ctx.best)
        if ctx.before.mate == 1:
            return "missed_mate", f"You missed checkmate in one: {best}."
        if ctx.before.mate > 3:
            return "missed_mate", (f"You had a forced checkmate in {ctx.before.mate}, starting "
                                   f"{san_line(ctx.board, ctx.best_line, 3)}.")
        line = san_line(ctx.board, ctx.best_line, 2 * ctx.before.mate - 1)
        return "missed_mate", (f"You had a forced checkmate in {ctx.before.mate}, "
                               f"starting with {best}: {line}.")
    return None


def allowed_mate(ctx: MoveContext):
    if ctx.after.mate and ctx.after.mate < 0 and not (ctx.before.mate and ctx.before.mate < 0):
        after = ctx.board_after()
        n = -ctx.after.mate
        line = san_line(after, ctx.reply_line, 2 * n)
        if n == 1:
            return "allowed_mate", f"This allows checkmate in one: {line}."
        if n > 3:
            return "allowed_mate", (f"This walks into a forced checkmate in {n}, starting "
                                    f"{san_line(after, ctx.reply_line, 3)}.")
        return "allowed_mate", f"This walks into a forced checkmate in {n}: {line}."
    return None


def walked_into_fork(ctx: MoveContext):
    if not ctx.reply_line:
        return None
    after = ctx.board_after()
    reply = ctx.reply_line[0]
    if reply not in after.legal_moves:
        return None
    captured = after.piece_at(reply.to_square)
    if captured and value(captured.piece_type) > 1:
        return None  # that is a capture, handled by the hanging-piece rule
    result = play_line(ctx.board, [ctx.move] + ctx.reply_line, ctx.me)
    if -result.net < MIN_LOSS:
        return None

    reply_san = after.san(reply)
    after.push(reply)
    attacker = after.piece_at(reply.to_square)
    targets = []
    for sq in after.attacks(reply.to_square):
        target = after.piece_at(sq)
        if not target or target.color != ctx.me:
            continue
        if target.piece_type == chess.KING:
            targets.append(sq)
        elif value(target.piece_type) >= 3 and (
                value(target.piece_type) > value(attacker.piece_type)
                or not after.is_attacked_by(ctx.me, sq)):
            targets.append(sq)
    if len(targets) < 2:
        return None
    names = [chess.piece_name(after.piece_at(sq).piece_type) for sq in targets]
    attacked = " and ".join(f"your {n}" for n in names)
    return "fork", (f"This walks into a fork: {reply_san} attacks {attacked}, "
                    f"and you lose {describe_trade(result)}.")


def discovered_attack(ctx: MoveContext):
    """The opponent's reply moves a piece out of the way of one of their long-range
    pieces, which then attacks something valuable of mine."""
    if not ctx.reply_line:
        return None
    after = ctx.board_after()
    reply = ctx.reply_line[0]
    if reply not in after.legal_moves:
        return None
    result = play_line(ctx.board, [ctx.move] + ctx.reply_line, ctx.me)
    if -result.net < MIN_LOSS:
        return None

    them = not ctx.me
    sliders = [sq for pt in (chess.BISHOP, chess.ROOK, chess.QUEEN)
               for sq in after.pieces(pt, them) if sq != reply.from_square]
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
            if (target and target.color == ctx.me and target.piece_type != chess.KING
                    and value(target.piece_type) >= 3
                    and (value(target.piece_type) > value(slider.piece_type)
                         or not after.is_attacked_by(ctx.me, target_sq))):
                candidates.append((value(target.piece_type), sq, target_sq))
    if not candidates:
        return None
    _, sq, target_sq = max(candidates)
    return "discovered_attack", (
        f"This allows a discovered attack: {reply_san} uncovers the "
        f"{piece_on(after, sq)}, which now attacks your "
        f"{piece_on(after, target_sq)}. You lose {describe_trade(result)}.")


def hanging_piece(ctx: MoveContext):
    if not ctx.reply_line:
        return None
    after = ctx.board_after()
    reply = ctx.reply_line[0]
    if reply not in after.legal_moves or not after.is_capture(reply):
        return None
    result = play_line(ctx.board, [ctx.move] + ctx.reply_line, ctx.me)
    if result.net > -1:
        return None

    target_sq = reply.to_square
    target = piece_on(after, target_sq)
    defended = after.is_attacked_by(ctx.me, target_sq)
    reply_san = after.san(reply)
    lost = describe_trade(result)
    if target_sq == ctx.move.to_square:
        if defended:
            return "hanging_piece", (f"Your {target} isn't safe there: {reply_san} wins material "
                                     f"even though it is defended. You lose {lost}.")
        name = chess.piece_name(after.piece_at(target_sq).piece_type)
        return "hanging_piece", (f"You moved your {name} to {chess.square_name(target_sq)}, "
                                 f"where it can simply be taken ({reply_san}). You lose {lost}.")
    if defended:
        return "loses_material", (f"This lets your opponent win material with {reply_san} "
                                  f"({san_line(after, ctx.reply_line)}). You lose {lost}.")
    return "hanging_piece", (f"This leaves your {target} undefended, and {reply_san} "
                             f"takes it. You lose {lost}.")


def loses_material(ctx: MoveContext):
    result = play_line(ctx.board, [ctx.move] + ctx.reply_line, ctx.me)
    if -result.net < MIN_LOSS:
        return None
    line = san_line(ctx.board_after(), ctx.reply_line)
    return "loses_material", f"This loses {describe_trade(result)} by force: {line}."


def missed_win(ctx: MoveContext):
    if not ctx.best_line:
        return None
    best_result = play_line(ctx.board, ctx.best_line, ctx.me)
    mine = material_change(ctx.board, [ctx.move] + ctx.reply_line, ctx.me)
    if best_result.net < MIN_LOSS or best_result.net - mine < MIN_LOSS:
        return None
    best = ctx.best
    best_san = _san(ctx.board, best)
    won = describe_trade(best_result, losing=False)
    if ctx.board.is_capture(best):
        target = ctx.board.piece_at(best.to_square)
        if target and not ctx.board.is_attacked_by(not ctx.me, best.to_square):
            return "missed_capture", (f"You missed {best_san}: the {piece_on(ctx.board, best.to_square)} "
                                      f"was undefended, so you could win {won}.")
        return "missed_capture", (f"You missed {best_san}, which wins {won}: "
                                  f"{san_line(ctx.board, ctx.best_line)}.")
    return "missed_tactic", (f"You missed a tactic: {best_san} wins {won} "
                             f"({san_line(ctx.board, ctx.best_line)}).")


def exposed_king(ctx: MoveContext):
    """The opponent's best reply is a check that hurts (but no forced mate or material loss)."""
    if not ctx.reply_line:
        return None
    after = ctx.board_after()
    reply = ctx.reply_line[0]
    if reply not in after.legal_moves or not after.gives_check(reply):
        return None
    return "king_safety", (f"This exposes your king: {san_line(after, ctx.reply_line)} "
                           f"and the evaluation drops from {ctx.before} to {ctx.after}.")


def missed_opportunity(ctx: MoveContext):
    """I was clearly better and let most of the advantage slip without losing material."""
    if ctx.before.win_chance() < 0.3 or ctx.after.win_chance() < -0.2 or not ctx.best:
        return None
    line = san_line(ctx.board, ctx.best_line)
    return "missed_opportunity", (f"You were clearly better ({ctx.before}) but this lets the advantage "
                                  f"slip ({ctx.after}). Stronger was {line}.")


def positional(ctx: MoveContext):
    after = ctx.board_after()
    reply = san_line(after, ctx.reply_line)
    if ctx.before.win_chance() >= 0.3 and ctx.after.win_chance() <= -0.3:
        start = f"; the trouble starts with {reply}" if reply else ""
        return "positional", (f"This turns a winning position ({ctx.before}) into a losing one "
                              f"({ctx.after}){start}.")
    threat = f" after {reply}" if reply else ""
    return "positional", (f"No simple tactic found, but your position gets worse{threat} "
                          f"({ctx.before} to {ctx.after}).")


RULES = [missed_mate, allowed_mate, walked_into_fork, discovered_attack, hanging_piece, loses_material,
         missed_win, exposed_king, missed_opportunity, positional]


# Wording for "missed_*" kinds that does not give the answer away (review puzzles).
SPOILER_FREE = {
    "missed_mate": "There was a forced checkmate, and this move misses it.",
    "missed_capture": "This misses a chance to win material.",
    "missed_tactic": "This misses a tactic that wins material.",
}


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
    if ctx.best and kind not in ("missed_mate", "missed_capture", "missed_tactic",
                                 "missed_opportunity"):
        text += f" Best was {_san(ctx.board, ctx.best)}."
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
    "missed_opportunity": "You're clearly better here. Find the move that keeps the advantage, often the most forcing one.",
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
    line = san_line(board, ctx.best_line, 6)
    after_best = board.copy(stack=False)
    after_best.push(ctx.best)

    if ctx.before.mate and ctx.before.mate > 0:
        if kind == "missed_mate":
            return ""   # the explanation of my move already shows the mate
        return f"{best_san} starts a forced checkmate in {ctx.before.mate}: {line}."

    sentences = []
    gain = play_line(board, ctx.best_line, ctx.me)
    if kind in DEFENSIVE_KINDS and ctx.reply_line:
        sentences.append(_what_best_prevents(ctx, best_san, after_best))
    elif gain.net >= 1:
        target = board.piece_at(ctx.best.to_square) if board.is_capture(ctx.best) else None
        free = target and not board.is_attacked_by(not ctx.me, ctx.best.to_square)
        if free and value(target.piece_type) >= gain.net:
            sentences.append(f"{best_san} simply takes the undefended {chess.piece_name(target.piece_type)}.")
        else:
            sentences.append(f"{best_san} wins {describe_trade(gain, losing=False)}: {line}.")
    threats = _new_threats(after_best, ctx.best, ctx.me)
    if threats:
        sentences.append(f"{best_san} attacks {threats}." if not sentences else f"It attacks {threats}.")
    if not sentences:
        sentences.append(f"{best_san} keeps the position at {ctx.before} for you, instead of {ctx.after} "
                         f"after your move. The main line goes {line}.")
    return " ".join(s for s in sentences if s)


def _what_best_prevents(ctx: MoveContext, best_san: str, after_best: chess.Board) -> str:
    """Describe how the best move deals with the reply my move allowed."""
    after_mine = ctx.board_after()
    threat = ctx.reply_line[0]
    if threat not in after_mine.legal_moves:
        return ""
    threat_san = after_mine.san(threat)
    if ctx.after.mate and ctx.after.mate < 0:
        return f"{best_san} keeps your king safe: after your move, {threat_san} started a forced mate."
    target_sq = threat.to_square
    target = after_mine.piece_at(target_sq)
    if target and target.color == ctx.me and target.piece_type != chess.KING:
        name = chess.piece_name(target.piece_type)
        moved_from = ctx.best.from_square
        if moved_from == target_sq or (ctx.move.to_square == target_sq and moved_from == ctx.move.from_square):
            return f"{best_san} keeps your {name} safe instead of leaving it to {threat_san}."
        if after_best.is_attacked_by(ctx.me, target_sq) and not after_mine.is_attacked_by(ctx.me, target_sq):
            return f"{best_san} defends your {name}, so {threat_san} no longer wins it."
    return (f"{best_san} avoids the problem with your move, which allowed "
            f"{san_line(after_mine, ctx.reply_line, 3)}.")


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
