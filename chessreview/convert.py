"""Convert won positions from my games, and essential endgames, played against Stockfish.

Convert: from each game, the first position where I was clearly winning (level.winning_cp:
the bar is lower for stronger players) and still was on my next move. Games I didn't win
come first. Stockfish defends at full strength, which is level-neutral.

Rules of a playout (`state()`), from my point of view after Stockfish's reply. A win goal ends at
its milestone (`Task.finish`, P11), never just because the eval stayed high:
  mate       checkmate (the basic mates)
  promote    I promoted and am still decisive after the reply (mate, or +7 or more)
  bare_king  Stockfish is left with only its king (it had more at the start) and I keep a queen or rook
  resign     convert positions: mate, a safe promotion or a bare king as above, or the eval at
             `Task.resign_cp` (3 pawns above the start, at least +7) on three of my moves in a row
Every finish but "mate" also ends when Stockfish sees itself mated in <= 5.
  win goal   fail = a draw of any kind, being mated, running out of moves (max_plies), or for
                    game positions the advantage slipping below 40% of the "clearly winning" bar
  draw goal  (finish "hold") won = a draw by rule, or holding for HOLD_MOVES of my moves
             fail = falling below -60% of the bar, or being mated

Tasks are keyed "convert/<game_id>:<ply>" or "endgame/<id>"; SRS cards use kind 'convert' /
'endgame' with the part after the slash as key.
"""

import sqlite3
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

import chess

from . import level, srs
from .classify import Eval
from .config import Config

ENDGAMES_PATH = Path(__file__).parent / "data" / "endgames.toml"
LEVELS = {1: "Beginner", 2: "Intermediate", 3: "Advanced"}
RESIGN_CP = 700          # decisive: a promotion or bare king counts from here; convert resigns no lower
RESIGN_GAIN = 300        # convert positions resign 3 pawns above their start (P11)...
RESIGN_MOVES = 3         # ...held for this many of my moves in a row
RESIGN_MATE = 5          # every finish but "mate" also ends when Stockfish is mated in this many or fewer
FINISHES = ("mate", "promote", "bare_king", "hold", "resign")
HOLD_MOVES = 15          # a draw goal is reached after holding this many of my moves
CONVERT_MAX_PLIES = 120
ENDGAME_MAX_PLIES = 100
SLIP_SHARE = 0.4         # convert fails below 40% of the "clearly winning" bar
LOSING_SHARE = 0.6       # a draw goal fails below -60% of it


@dataclass(frozen=True)
class Task:
    key: str               # "convert/12:34" | "endgame/kq_k"
    kind: str              # 'convert' | 'endgame'
    fen: str
    color: str             # my colour: 'white' | 'black'
    goal: str              # 'win' | 'draw'
    finish: str            # FINISHES: 'mate' | 'promote' | 'bare_king' | 'hold' (endgames), 'resign' (convert)
    title: str
    subtitle: str = ""
    tip: str = ""
    max_plies: int = ENDGAME_MAX_PLIES
    level: int | None = None
    game_id: int | None = None
    ply: int | None = None
    start_eval: str | None = None
    start_cp: int | None = None    # my eval at the start, in centipawns (convert positions only)
    resign_cp: int | None = None   # convert: Stockfish resigns at this eval (None: the start is a mate)

    @property
    def card_key(self) -> str:
        return self.key.split("/", 1)[1]

    def as_dict(self) -> dict:
        return asdict(self) | {"finish_text": finish_text(self)}


def finish_text(task: Task) -> str:
    """What ends the task, shown under the prompt before the first move."""
    if task.goal == "draw":
        return f"Held after {HOLD_MOVES} of your moves, or as soon as it is a draw by rule."
    if task.finish == "mate":
        return "Done at checkmate."
    mated = f"when it is mated in {RESIGN_MATE} or less"
    if task.finish == "promote":
        return f"Done when you promote and are still clearly winning after the reply (Stockfish also resigns {mated})."
    if task.finish == "bare_king":
        return f"Done when Stockfish has only its king left (it also resigns {mated})."
    ways = [mated, "after a safe promotion", "when it has only its king left"]
    if task.resign_cp is not None:
        ways.append(f"at {task.resign_cp / 100:+.2f} or more on {RESIGN_MOVES} of your moves in a row")
    return f"Stockfish resigns {', '.join(ways[:-1])} or {ways[-1]}."


# ------------------------------------------------------------ essential endgames

def endgames() -> list[Task]:
    with open(ENDGAMES_PATH, "rb") as f:
        raw = tomllib.load(f)["endgame"]
    out = []
    for e in raw:
        board = chess.Board(e["fen"])
        out.append(Task(
            key=f"endgame/{e['id']}", kind="endgame", fen=e["fen"],
            color="white" if board.turn == chess.WHITE else "black", goal=e["goal"], finish=e["finish"],
            title=e["title"], subtitle=f"{LEVELS[e['level']]} · {'win' if e['goal'] == 'win' else 'hold the draw'}",
            tip=e.get("tip", ""), max_plies=e.get("max_plies", ENDGAME_MAX_PLIES), level=e["level"]))
    return out


def endgame_overview(conn: sqlite3.Connection) -> list[dict]:
    """Endgames with card status; a level unlocks once every endgame of the level before is solved."""
    cards = {r["key"]: r for r in conn.execute("SELECT * FROM cards WHERE kind = 'endgame'")}
    tasks = endgames()
    solved: dict[int, list[bool]] = {}
    for t in tasks:
        solved.setdefault(t.level, []).append(_status(cards.get(t.card_key)) in ("solved", "mastered"))
    unlocked = 1
    while all(solved.get(unlocked, [False])) and unlocked + 1 in solved:
        unlocked += 1
    return [{**t.as_dict(), "unlocked": t.level <= unlocked, "status": _status(cards.get(t.card_key))}
            for t in tasks]


def _status(card: sqlite3.Row | None) -> str:
    """new / solved / failed / mastered. A retry solved the same day counts as solved (it unlocks
    the next level), although only the day's first attempt moves the card (srs.next_state)."""
    if card is None:
        return "new"
    if card["mastered_on"]:
        return "mastered"
    return "solved" if card["box"] >= 1 or card["last_result"] in srs.SOLVED else "failed"


# ------------------------------------------------------------ won positions from my games

def convert_positions(conn: sqlite3.Connection) -> list[dict]:
    """The first clearly winning position of each game (held on my next move too); games I
    didn't win first (losses, then draws), newest first within each."""
    winning = level.winning_chance(level.current_rating(conn))
    rows = conn.execute("""
        SELECT m.game_id, m.ply, m.win_before, g.my_result, g.my_color, g.white, g.black, g.date, g.end_time,
               p.fen, p.score_cp, p.score_mate
        FROM mistakes m JOIN games g ON g.id = m.game_id
        JOIN positions p ON p.game_id = m.game_id AND p.ply = m.ply
        WHERE m.is_mine = 1 ORDER BY m.game_id, m.ply""").fetchall()
    by_game: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        by_game.setdefault(r["game_id"], []).append(r)
    out = []
    for game_rows in by_game.values():
        for here, nxt in zip(game_rows, game_rows[1:]):
            if (here["win_before"] or -1) >= winning and (nxt["win_before"] or -1) >= winning:
                out.append(_convert_entry(here))
                break
    out.sort(key=lambda e: e["end_time"] or "", reverse=True)               # newest first...
    return sorted(out, key=lambda e: {"loss": 0, "draw": 1, "win": 2}[e["result"]])   # ...within losses, draws, wins


def _convert_entry(r: sqlite3.Row) -> dict:
    me = chess.WHITE if r["my_color"] == "white" else chess.BLACK
    return {"key": f"convert/{r['game_id']}:{r['ply']}", "game_id": r["game_id"], "ply": r["ply"],
            "fen": r["fen"], "color": r["my_color"], "result": r["my_result"],
            "opponent": r["black"] if r["my_color"] == "white" else r["white"], "date": r["date"],
            "end_time": r["end_time"], "eval": str(Eval.from_row(r, me))}


def convert_overview(conn: sqlite3.Connection) -> list[dict]:
    cards = {r["key"]: r for r in conn.execute("SELECT * FROM cards WHERE kind = 'convert'")}
    return [{**e, "status": _status(cards.get(e["key"].split("/", 1)[1]))} for e in convert_positions(conn)]


def next_convert(conn: sqlite3.Connection, on=None) -> str | None:
    """A due convert card, else the first won position never tried."""
    return _due(conn, "convert", on) or _untried_convert(conn)


def next_endgame(conn: sqlite3.Connection, on=None) -> str | None:
    """Today's endgame to play (E2): a due essential endgame, else a due Convert position, else the
    next essential endgame never played (in unlock order), else a won position never tried."""
    return (_due(conn, "endgame", on) or _due(conn, "convert", on)
            or next((e["key"] for e in endgame_overview(conn) if e["unlocked"] and e["status"] == "new"), None)
            or _untried_convert(conn))


def _due(conn: sqlite3.Connection, kind: str, on=None) -> str | None:
    """The most overdue card of a kind ('convert' | 'endgame') whose task still exists."""
    on = on or srs.today()
    for r in conn.execute("""SELECT key FROM cards WHERE kind = ? AND mastered_on IS NULL
                             AND due_on IS NOT NULL AND due_on <= ? ORDER BY due_on, box""",
                          (kind, on.isoformat())):
        if task_for(conn, f"{kind}/{r['key']}") is not None:
            return f"{kind}/{r['key']}"
    return None


def _untried_convert(conn: sqlite3.Connection) -> str | None:
    tried = {r["key"] for r in conn.execute("SELECT key FROM cards WHERE kind = 'convert'")}
    return next((e["key"] for e in convert_positions(conn) if e["key"].split("/", 1)[1] not in tried), None)


def task_for(conn: sqlite3.Connection, key: str) -> Task | None:
    kind, _, rest = key.partition("/")
    if kind == "endgame":
        return next((t for t in endgames() if t.key == key), None)
    game, _, ply_text = rest.partition(":")
    if kind != "convert" or not game.isdigit() or not ply_text.isdigit():
        return None     # a malformed key is an unknown position, not an error
    game_id, ply = int(game), int(ply_text)
    r = conn.execute("""SELECT p.fen, p.score_cp, p.score_mate, g.my_color, g.my_result, g.white, g.black, g.date
                        FROM positions p JOIN games g ON g.id = p.game_id
                        WHERE p.game_id = ? AND p.ply = ?""", (game_id, ply)).fetchone()
    me = chess.WHITE if r is not None and r["my_color"] == "white" else chess.BLACK
    if r is None or chess.Board(r["fen"]).turn != me:     # only positions where it is my move
        return None
    opponent = r["black"] if r["my_color"] == "white" else r["white"]
    outcome = {"win": "won", "loss": "lost", "draw": "drew"}.get(r["my_result"], "played")
    start = Eval.from_row(r, me)
    return Task(key=key, kind="convert", fen=r["fen"], color=r["my_color"], goal="win", finish="resign",
                title=f"vs {opponent}, move {ply // 2 + 1}", subtitle=f"{r['date']} · in the game you {outcome}",
                max_plies=CONVERT_MAX_PLIES, game_id=game_id, ply=ply, start_eval=str(start),
                start_cp=start.as_cp(), resign_cp=resign_cp(start))


def resign_cp(start: Eval) -> int | None:
    """Relative to the start (P11): from the raw eval, since as_cp() caps at +10. A start that is
    already a forced mate has no eval bar: mate it, promote or take everything."""
    if start.mate is not None:
        return None
    return max(RESIGN_CP, (start.cp or 0) + RESIGN_GAIN)


# ------------------------------------------------------------ playing it out

@dataclass
class State:
    status: str            # 'playing' | 'won' | 'failed'
    reason: str = ""
    streak: int = 0        # convert: my moves in a row at Task.resign_cp or more


def state(task: Task, board: chess.Board, ev: Eval | None, streak: int, rating: int | None) -> State:
    """Where the playout stands after a move (`ev`: engine eval from my side, None if the game is over)."""
    me = chess.WHITE if task.color == "white" else chess.BLACK
    plies = len(board.move_stack)
    if board.is_checkmate():
        return State("won", "Checkmate!") if board.turn != me else State("failed", "You were checkmated.")
    draw = _draw_reason(board)
    if draw:
        return State("won", f"{draw}: you held the draw.") if task.goal == "draw" else State("failed", f"{draw}.")
    if ev is None:
        return State("playing", streak=streak)
    bar = level.winning_cp(rating)
    if ev.mate is not None and ev.mate < 0:
        return State("failed", "You are getting mated.")
    if task.goal == "draw":
        if ev.as_cp() <= -LOSING_SHARE * bar:
            return State("failed", f"The position is now lost ({ev}).")
        if (plies + 1) // 2 >= HOLD_MOVES:
            return State("won", f"You held for {HOLD_MOVES} moves.")
        return State("playing")
    decisive = (ev.mate is not None and ev.mate > 0) or (ev.cp or 0) >= RESIGN_CP
    if task.finish != "mate":
        milestone = _milestone(task, board, me, ev, decisive)
        if milestone:
            return State("won", milestone)
    if task.finish == "resign":
        at_bar = task.resign_cp is not None and (
            (ev.mate is not None and ev.mate > 0) or (ev.cp or 0) >= task.resign_cp)
        streak = streak + 1 if at_bar else 0
        if streak >= RESIGN_MOVES:
            return State("won", f"Stockfish resigns ({ev}, from {task.start_eval} at the start).", streak)
    if task.kind == "convert" and not decisive and ev.as_cp() < SLIP_SHARE * bar:
        return State("failed", f"The advantage slipped ({ev}).", streak)
    if plies >= task.max_plies:
        return State("failed", "Out of moves: you didn't finish it in time.", streak)
    return State("playing", streak=streak)


def _milestone(task: Task, board: chess.Board, me: chess.Color, ev: Eval, decisive: bool) -> str | None:
    """Why Stockfish resigns now (finishes other than "mate"), or None."""
    if ev.mate is not None and 0 < ev.mate <= RESIGN_MATE:
        return f"Stockfish resigns: you mate in {ev.mate}."
    if not decisive:
        return None
    if task.finish in ("promote", "resign") and any(m.promotion for m in board.move_stack[::2]):  # my moves
        return "You promoted safely: Stockfish resigns."
    if task.finish in ("bare_king", "resign") and _bare_king(chess.Board(task.fen), board, me):
        return "Only Stockfish's king is left: it resigns."
    return None


def _bare_king(start: chess.Board, board: chess.Board, me: chess.Color) -> bool:
    """The defender is down to its king (it had more at the start) and I still have a queen or rook."""
    def only_king(b: chess.Board) -> bool:
        return not b.occupied_co[not me] & ~b.kings
    return (not only_king(start) and only_king(board)
            and bool(board.pieces(chess.QUEEN, me) | board.pieces(chess.ROOK, me)))


def _draw_reason(board: chess.Board) -> str | None:
    if board.is_stalemate():
        return "Stalemate"
    if board.is_insufficient_material():
        return "No mating material left"
    if board.is_fifty_moves():
        return "50-move rule"
    if board.is_repetition(3):
        return "Threefold repetition"
    return None
