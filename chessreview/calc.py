"""Calculation trainer (E6, F10): see a position, read a line of moves without them being played on
the board, and answer a question about the position at the end of it.

Questions (`KINDS`):
  best     "What's the best move now?" Only from Lichess puzzles (their lines are checked), only
           where the puzzle's solver is to move at the end; typed in SAN or UCI and judged on the
           final position (any mate counts when the solution mates).
  capture  "Can <side> win material by capturing on <square> now?" (yes/no): a piece attacked by
           the side to move at the end, judged by a static exchange (explain.exchange). Not when
           the side to move is in check. The subject is chosen so yes and no come about equally.
  where    "Where is the <piece> that started on <square>?": a knight, bishop or rook, only when
           another of the same kind and colour also moved in the line (SAN names destinations but
           not which piece went: you have to follow them). Pawns stay on their file and there is one
           king, so those would just be reading.
Sources: Lichess puzzles near my puzzle rating (start = the stored position, or the one after the
opponent's first move, so every depth from 3 to 6 is reachable) and the moves actually played in
my games from one of my positions. Never an engine line from my positions: that would give away
the answer to the position's own card in Today/Practice. Puzzles used here are left out of
library.pick (and kept by library.fetch), and attempts never reach puzzle_attempts or
review_results (puzzle rating, coach.pace).

Depth (`LEVELS`, plies of the line) adapts: two new questions right in a row at the current depth
go one deeper, a miss one shallower (meta `calc_level`, `calc_run`). Missed questions become
cards (kind 'calc', key = source~plies~kind~subject, which regenerates the same question) and come
back first when due. Attempts are kept in `calc_attempts` (migration 12): the streak counts them.
"""

import json
import random
import re
import sqlite3
from dataclasses import asdict, dataclass

import chess

from . import db, library, srs
from .explain import exchange

LEVELS = {1: 3, 2: 4, 3: 5, 4: 6}           # level -> plies in the line
LEVEL_KEY, RUN_KEY = "calc_level", "calc_run"
KINDS = ("best", "capture", "where")
RATING_WINDOW = 400
TRIES = 120                                 # candidates looked at for one new question (where-questions are rare)
PIECE_NAMES = {chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop", chess.ROOK: "rook",
               chess.QUEEN: "queen", chess.KING: "king"}


@dataclass
class Question:
    key: str                # source~plies~kind~subject
    source: str             # "lib:<puzzle id>" | "game:<game id>:<ply>"
    kind: str
    plies: int
    subject: str            # a square name ('' for best)
    fen: str                # the position shown
    line: list[str]         # UCI
    line_san: str           # "1. Nxe5 dxe5 2. Qd5+"
    to_move: str            # at the end of the line
    orientation: str
    prompt: str
    answer: str             # what is expected: SAN, a square, 'yes' | 'no'
    final_fen: str
    title: str

    def public(self) -> dict:
        """What the page gets before answering (no answer, no final position)."""
        d = asdict(self)
        for k in ("answer", "final_fen"):
            d.pop(k)
        return d


# ------------------------------------------------------------ level

def level(conn: sqlite3.Connection) -> int:
    stored = db.get_meta(conn, LEVEL_KEY)
    return int(stored) if stored and stored.isdigit() and int(stored) in LEVELS else 1


def _adapt(conn: sqlite3.Connection, correct: bool) -> tuple[int, bool]:
    """Two rights in a row: one level deeper; a miss: one shallower. Returns (level, changed)."""
    lvl, run = level(conn), int(db.get_meta(conn, RUN_KEY) or 0)
    new, run = (lvl, run + 1) if correct else (max(1, lvl - 1), 0)
    if run >= 2:
        new, run = min(max(LEVELS), lvl + 1), 0
    with conn:
        db.set_meta(conn, LEVEL_KEY, str(new))
        db.set_meta(conn, RUN_KEY, str(run))
    return new, new != lvl


# ------------------------------------------------------------ building questions

def _line_san(board: chess.Board, line: list[str]) -> str:
    return board.variation_san([chess.Move.from_uci(u) for u in line])


def _source_line(conn: sqlite3.Connection, source: str, plies: int, need_next: bool
                 ) -> tuple[chess.Board, list[str], str | None, str] | None:
    """(start position, the line, the move after it or None, my side for the board) for a source,
    or None when it is too short / gone / not a legal line."""
    kind, _, rest = source.partition(":")
    if kind == "lib":
        row = conn.execute("SELECT fen, moves FROM puzzles WHERE id = ?", (rest,)).fetchone()
        if row is None:
            return None
        moves = row["moves"].split()
        board = chess.Board(row["fen"])
        solver = "white" if board.turn == chess.BLACK else "black"   # the stored position is before their move
        # Odd depths start from the stored position (the opponent's move first), even ones after it,
        # so the solver is to move at the end either way.
        offset = 0 if plies % 2 else 1
        if len(moves) < offset + plies + (1 if need_next else 0):
            return None
        for u in moves[:offset]:
            board.push_uci(u)
        nxt = moves[offset + plies] if need_next else None
        return board, moves[offset:offset + plies], nxt, solver
    if kind == "game":
        game_id, _, ply = rest.partition(":")
        if not (game_id.isdigit() and ply.isdigit()):
            return None
        g = conn.execute("SELECT moves_uci, my_color FROM games WHERE id = ?", (int(game_id),)).fetchone()
        if g is None or not g["moves_uci"]:
            return None
        moves = g["moves_uci"].split()
        start = int(ply)
        if len(moves) < start + plies:
            return None
        board = chess.Board()
        try:
            for u in moves[:start]:
                board.push_uci(u)
        except ValueError:
            return None
        return board, moves[start:start + plies], None, g["my_color"]
    return None


def build(conn: sqlite3.Connection, source: str, plies: int, kind: str, subject: str | None = None,
          rng: random.Random | None = None) -> Question | None:
    """The question for a source, depth and kind; `subject` (a square) fixes the one asked about
    (a card), else it is chosen. None when this source can't give such a question."""
    if kind == "best" and not source.startswith("lib:"):
        return None
    got = _source_line(conn, source, plies, need_next=kind == "best")
    if got is None:
        return None
    start, line, nxt, orientation = got
    final = start.copy()
    try:
        for u in line:
            final.push_uci(u)
    except ValueError:
        return None
    if final.is_game_over():
        return None
    rng = rng or random.Random(f"{source}~{plies}~{kind}")
    side = "White" if final.turn == chess.WHITE else "Black"
    made = _BUILDERS[kind](start, line, final, nxt, subject, rng)
    if made is None:
        return None
    subject, prompt, answer = made
    title = f"{plies} half-moves deep"
    return Question(key=f"{source}~{plies}~{kind}~{subject}", source=source, kind=kind, plies=plies,
                    subject=subject, fen=start.fen(), line=line, line_san=_line_san(start, line),
                    to_move=side.lower(), orientation=orientation, prompt=prompt.replace("<side>", side),
                    answer=answer, final_fen=final.fen(), title=title)


def _best(start, line, final, nxt, subject, rng):
    move = chess.Move.from_uci(nxt)
    if move not in final.legal_moves:
        return None
    return "", "After these moves, <side> is to move. What's the best move?", final.san(move)


def _capture(start, line, final, nxt, subject, rng):
    if final.is_check():
        return None
    me = final.turn
    candidates = {}
    for sq in chess.SquareSet(final.occupied_co[not me] & ~final.kings):
        if final.is_attacked_by(me, sq):
            candidates[sq] = exchange(final, sq)[0] > 0
    if subject:
        sq = chess.parse_square(subject)
        if sq not in candidates:
            return None
    else:
        if not candidates:
            return None
        want = rng.random() < 0.5             # yes and no about equally often
        pool = [s for s, wins in candidates.items() if wins == want] or list(candidates)
        sq = rng.choice(sorted(pool))
    piece = final.piece_at(sq)
    name = f"{'white' if piece.color == chess.WHITE else 'black'} {PIECE_NAMES[piece.piece_type]}"
    return (chess.square_name(sq),
            f"After these moves, can <side> win material by capturing the {name} on {chess.square_name(sq)}?",
            "yes" if candidates[sq] else "no")


def _where(start, line, final, nxt, subject, rng):
    # Follow each piece from its square in the start position.
    at = {sq: sq for sq in chess.SquareSet(start.occupied)}      # start square -> where it is now (None: taken)
    moved: dict[chess.Square, int] = {}
    board = start.copy()
    for u in line:
        move = chess.Move.from_uci(u)
        origin = next((o for o, now in at.items() if now == move.from_square), None)
        for o, now in at.items():
            if now == move.to_square:
                at[o] = None                                    # captured
        if board.is_en_passant(move):
            victim = move.to_square + (-8 if board.turn == chess.WHITE else 8)
            for o, now in at.items():
                if now == victim:
                    at[o] = None
        if board.is_castling(move):
            rook_from, rook_to = _castle_rook(board, move)
            for o, now in at.items():
                if now == rook_from:
                    at[o] = rook_to
        if origin is not None:
            at[origin] = move.to_square
            moved[origin] = moved.get(origin, 0) + 1
        board.push(move)

    def tracked(o):
        """It takes following the line: another piece like it (N, B, R) moved too."""
        p = start.piece_at(o)
        if p.piece_type not in (chess.KNIGHT, chess.BISHOP, chess.ROOK) or at[o] is None:
            return False
        return any(x != o and start.piece_at(x) == p for x in moved)

    options = sorted(o for o in moved if tracked(o))
    if subject:
        o = chess.parse_square(subject)
        if o not in options:
            return None
    elif not options:
        return None
    else:
        o = rng.choice(options)
    p = start.piece_at(o)
    name = f"{'white' if p.color == chess.WHITE else 'black'} {PIECE_NAMES[p.piece_type]}"
    return (chess.square_name(o), f"After these moves, where is the {name} that starts on {chess.square_name(o)}?",
            chess.square_name(at[o]))


def _castle_rook(board: chess.Board, move: chess.Move) -> tuple[chess.Square, chess.Square]:
    rank = chess.square_rank(move.from_square)
    if chess.square_file(move.to_square) > chess.square_file(move.from_square):
        return chess.square(7, rank), chess.square(5, rank)
    return chess.square(0, rank), chess.square(3, rank)


_BUILDERS = {"best": _best, "capture": _capture, "where": _where}


# ------------------------------------------------------------ choosing

def _in_todays_plan(conn: sqlite3.Connection) -> tuple[set[str], set[tuple[int, int]]]:
    """Puzzle ids and (game, ply) positions in today's Today plan: never shown here (their line or
    a move from it would give the item away, and a puzzle's first try is rated)."""
    stored = db.get_meta(conn, "today_plan")
    plan = json.loads(stored) if stored else {}
    if plan.get("day") != srs.today().isoformat():
        return set(), set()
    items = [i for b in plan.get("blocks", []) for i in b.get("items", [])]
    return {i[1] for i in items if i[0] == "lib"}, {(i[0], i[1]) for i in items if i[0] != "lib"}


def _library_sources(conn: sqlite3.Connection, plies: int, need_next: bool, rng: random.Random) -> list[str]:
    rating = library.rating(conn)
    min_moves = (0 if plies % 2 else 1) + plies + (1 if need_next else 0)
    rows = conn.execute("""
        SELECT id, moves FROM puzzles WHERE rating BETWEEN ? AND ?
          AND id NOT IN (SELECT puzzle_id FROM puzzle_attempts)
          AND id NOT IN (SELECT puzzle_id FROM calc_attempts WHERE puzzle_id IS NOT NULL)""",
        (rating - RATING_WINDOW, rating + RATING_WINDOW)).fetchall()
    planned = _in_todays_plan(conn)[0]
    ids = [r["id"] for r in rows if len(r["moves"].split()) >= min_moves and r["id"] not in planned]
    rng.shuffle(ids)
    return [f"lib:{i}" for i in ids[:TRIES]]


def _game_sources(conn: sqlite3.Connection, rng: random.Random) -> list[str]:
    planned = _in_todays_plan(conn)[1]
    rows = conn.execute("""SELECT game_id, ply FROM mistakes WHERE is_mine = 1 AND phase IN ('middlegame', 'endgame')
                           ORDER BY RANDOM() LIMIT ?""", (TRIES,)).fetchall()
    return [f"game:{r['game_id']}:{r['ply']}" for r in rows if (r["game_id"], r["ply"]) not in planned]


def due_question(conn: sqlite3.Connection, exclude: set[str] = frozenset(), on=None) -> Question | None:
    """The most overdue calc card whose question can still be built (its puzzle may be gone)."""
    on = on or srs.today()
    for r in conn.execute("""SELECT key FROM cards WHERE kind = 'calc' AND mastered_on IS NULL AND due_on IS NOT NULL
                             AND due_on <= ? ORDER BY due_on, box""", (on.isoformat(),)):
        if r["key"] in exclude:
            continue
        q = from_key(conn, r["key"])
        if q is not None:
            return q
    return None


def from_key(conn: sqlite3.Connection, key: str) -> Question | None:
    parts = key.split("~")
    if len(parts) != 4 or not parts[1].isdigit() or parts[2] not in KINDS:
        return None
    source, plies, kind, subject = parts[0], int(parts[1]), parts[2], parts[3]
    try:
        return build(conn, source, plies, kind, subject or None)
    except ValueError:              # a malformed square
        return None


def new_question(conn: sqlite3.Connection, exclude: set[str] = frozenset(), rng: random.Random | None = None
                 ) -> Question | None:
    """A question never asked, at my current depth. Kinds take turns (by how many I've answered);
    a kind that can't be made from what's here gives way to the next."""
    rng = rng or random.Random()
    plies = LEVELS[level(conn)]
    asked = conn.execute("SELECT COUNT(*) FROM calc_attempts").fetchone()[0]
    seen = {r[0] for r in conn.execute("SELECT key FROM calc_attempts")} | set(exclude)
    for i in range(len(KINDS)):
        kind = KINDS[(asked + i) % len(KINDS)]
        sources = _library_sources(conn, plies, kind == "best", rng)
        if kind != "best":
            sources = [s for pair in zip(sources, _game_sources(conn, rng)) for s in pair] or \
                sources or _game_sources(conn, rng)
        for source in sources:
            q = build(conn, source, plies, kind, rng=rng)
            if q is not None and q.key not in seen and not any(k.startswith(source + "~") for k in exclude):
                return q
    return None


def next_question(conn: sqlite3.Connection, exclude: set[str] = frozenset()) -> tuple[Question | None, bool]:
    """(question, due): a due card first, else a new one."""
    q = due_question(conn, exclude)
    if q is not None:
        return q, True
    return new_question(conn, exclude), False


# ------------------------------------------------------------ answering

class Unreadable(ValueError):
    """Not an answer at all (a typo): nothing is recorded."""


UCI = re.compile(r"[a-h][1-8][a-h][1-8][qrbn]?")


def read_move(board: chess.Board, text: str) -> chess.Move | None:
    """A typed move in this position: SAN (also with a lowercase piece letter, "nxe5") or UCI. None
    when it reads as a move but isn't legal here (a miscalculation); Unreadable otherwise."""
    if not text:
        raise Unreadable("type a move")
    tries = [text] + ([text[0].upper() + text[1:]] if text[0] in "nbrqk" else [])   # "bxc3": pawn, then bishop
    illegal = False
    for t in tries:
        try:
            return board.parse_san(t)
        except (chess.IllegalMoveError, chess.AmbiguousMoveError):
            illegal = True
        except chess.InvalidMoveError:
            pass
    if UCI.fullmatch(text.lower()):
        move = chess.Move.from_uci(text.lower())
        return move if move in board.legal_moves else None
    if illegal:
        return None
    raise Unreadable(f"{text!r} isn't a move")


def judge(q: Question, answer: str) -> tuple[bool, bool]:
    """(right, illegal): `illegal` = a readable move that isn't legal at the end of the line.
    Unreadable for an answer of the wrong shape (a typo), which isn't a miss."""
    answer = (answer or "").strip()
    if q.kind == "capture":
        if answer.lower() not in ("yes", "no"):
            raise Unreadable("answer yes or no")
        return answer.lower() == q.answer, False
    if q.kind == "where":
        if not re.fullmatch(r"[a-h][1-8]", answer.lower()):
            raise Unreadable("answer with a square, like e4")
        return answer.lower() == q.answer, False
    final = chess.Board(q.final_fen)
    move = read_move(final, answer)
    if move is None:
        return False, True
    expected = final.parse_san(q.answer)
    if move == expected:
        return True, False
    final.push(expected)
    mates = final.is_checkmate()
    final.pop()
    final.push(move)
    return mates and final.is_checkmate(), False            # any mate when the solution mates


def answer(conn: sqlite3.Connection, key: str, given: str) -> dict | None:
    """Judge an answer, store the attempt, move the card and (for a new question at my depth) the level."""
    q = from_key(conn, key)
    if q is None:
        return None
    correct, illegal = judge(q, given)                      # Unreadable: nothing recorded
    card = conn.execute("SELECT 1 FROM cards WHERE kind = 'calc' AND key = ?", (key,)).fetchone()
    fresh = card is None and conn.execute("SELECT 1 FROM calc_attempts WHERE key = ?", (key,)).fetchone() is None
    with conn:
        conn.execute("""INSERT INTO calc_attempts (key, source, puzzle_id, kind, plies, result)
                        VALUES (?, ?, ?, ?, ?, ?)""",
                     (key, q.source, q.source[4:] if q.source.startswith("lib:") else None, q.kind, q.plies,
                      "solved" if correct else "failed"))
    if card or not correct:
        srs.record_key(conn, "calc", key, "solved" if correct else "failed")
    lvl, changed = level(conn), False
    if fresh and q.plies == LEVELS[lvl]:
        lvl, changed = _adapt(conn, correct)
    return {"correct": correct, "illegal": illegal, "expected": q.answer, "final_fen": q.final_fen,
            "line_san": q.line_san,
            "level": lvl, "plies": LEVELS[lvl], "level_changed": changed}


def overview(conn: sqlite3.Connection) -> dict:
    lvl = level(conn)
    day = srs.today().isoformat()
    today = conn.execute(f"""SELECT COUNT(*) AS n, SUM(result = 'solved') AS ok FROM calc_attempts
                             WHERE {srs.day_sql('at')} = ?""", (day,)).fetchone()
    by_depth = [{"plies": r["plies"], "tried": r["n"], "solved": r["ok"] or 0} for r in conn.execute(
        "SELECT plies, COUNT(*) AS n, SUM(result = 'solved') AS ok FROM calc_attempts GROUP BY plies ORDER BY plies")]
    due = conn.execute("""SELECT COUNT(*) FROM cards WHERE kind = 'calc' AND mastered_on IS NULL
                          AND due_on IS NOT NULL AND due_on <= ?""", (day,)).fetchone()[0]
    return {"level": lvl, "plies": LEVELS[lvl], "levels": LEVELS, "run": int(db.get_meta(conn, RUN_KEY) or 0),
            "today": {"tried": today["n"], "solved": today["ok"] or 0}, "by_depth": by_depth, "due": due,
            "library": library.count(conn) > 0}
