"""Sparring (E5, F9): play a position out against Stockfish at about my strength.

Start positions come from my games: per game, the first middlegame position that was about
equal (within EQUAL_CP) and the first endgame position that was equal or better for me, newest
games first. The standard starting position (either colour) or any FEN works too.

Strength (`strength`): my current rating (level.current_rating) plus an offset I choose
(OFFSETS, saved in meta `spar_offset`). Stockfish limits itself with UCI_LimitStrength /
UCI_Elo, but only from ELO_FLOOR (1320 in Stockfish 16-19); below that it plays at Skill Level 0
with a shallow depth limit (BELOW_FLOOR_DEPTH by target), which is an approximation: the label
says so, and my results against each level (`record`) show whether to move the offset. The
limited engine is its own process (live.LiveEngine.play), so these options never reach the
engine that judges moves.

Games are stored in `spar_games` (migration 11) and count as training for the streak. After a
game, `/api/spar/review` judges my moves at full strength (live.analyse + classify.judge at my
current thresholds), chunk by chunk.
"""

import sqlite3
from dataclasses import dataclass

import chess
import chess.engine

from . import db, level
from .classify import Eval

OFFSETS = list(range(-200, 301, 50))
OFFSET_KEY = "spar_offset"
DEFAULT_RATING = 1200               # before any rated game
ELO_FLOOR, ELO_MAX = 1320, 3190     # Stockfish's UCI_Elo range
MOVE_TIME = 0.3                     # seconds per engine move
# Below the floor: Skill Level 0 searching at most this deep (lower targets, shallower).
BELOW_FLOOR_DEPTH = [(700, 1), (950, 2), (1150, 3), (ELO_FLOOR, 4)]
EQUAL_CP = 150
MAX_POSITIONS = 40                  # per phase
PHASES = ("middlegame", "endgame")


@dataclass(frozen=True)
class Strength:
    elo: int                        # the target
    options: dict                   # UCI options for the limited engine
    limit: chess.engine.Limit
    exact: bool                     # False below ELO_FLOOR: an approximation

    @property
    def label(self) -> str:
        return f"Stockfish ≈ {self.elo}" + ("" if self.exact else " (approximate below 1320)")


def target_elo(rating: int | None, offset: int) -> int:
    return (rating or DEFAULT_RATING) + clamp_offset(offset)


def clamp_offset(offset: int) -> int:
    return min(max(int(offset), OFFSETS[0]), OFFSETS[-1])


def strength(elo: int) -> Strength:
    elo = min(max(elo, 400), ELO_MAX)
    if elo >= ELO_FLOOR:
        return Strength(elo, {"UCI_LimitStrength": True, "UCI_Elo": elo, "Skill Level": 20},
                        chess.engine.Limit(time=MOVE_TIME), True)
    depth = next(d for below, d in BELOW_FLOOR_DEPTH if elo < below)
    return Strength(elo, {"UCI_LimitStrength": False, "Skill Level": 0},
                    chess.engine.Limit(time=MOVE_TIME, depth=depth), False)


def levels(conn: sqlite3.Connection) -> list[dict]:
    """Every offset with its target and label (the strength slider)."""
    rating = level.current_rating(conn)
    out = []
    for offset in OFFSETS:
        s = strength(target_elo(rating, offset))
        out.append({"offset": offset, "elo": s.elo, "label": s.label, "exact": s.exact})
    return out


def saved_offset(conn: sqlite3.Connection) -> int:
    stored = db.get_meta(conn, OFFSET_KEY)
    return clamp_offset(int(stored)) if stored and stored.lstrip("-").isdigit() else 0


def save_offset(conn: sqlite3.Connection, offset: int) -> None:
    with conn:
        db.set_meta(conn, OFFSET_KEY, str(clamp_offset(offset)))


# ------------------------------------------------------------ start positions

def positions(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Per game, the first about-equal middlegame position and the first equal-or-better endgame
    position where it was my move; newest games first, MAX_POSITIONS per phase."""
    rows = conn.execute("""
        SELECT m.game_id, m.ply, m.phase, g.my_color, g.my_result, g.white, g.black, g.date, g.end_time,
               p.fen, p.score_cp, p.score_mate
        FROM mistakes m JOIN games g ON g.id = m.game_id
        JOIN positions p ON p.game_id = m.game_id AND p.ply = m.ply
        WHERE m.is_mine = 1 AND m.phase IN ('middlegame', 'endgame')
        ORDER BY g.end_time DESC, g.id DESC, m.ply""").fetchall()
    out: dict[str, list[dict]] = {p: [] for p in PHASES}
    seen: set[tuple[int, str]] = set()
    for r in rows:
        if (r["game_id"], r["phase"]) in seen or len(out[r["phase"]]) >= MAX_POSITIONS:
            continue
        me = chess.WHITE if r["my_color"] == "white" else chess.BLACK
        if chess.Board(r["fen"]).turn != me:
            continue
        ev = Eval.from_row(r, me)
        fits = (ev.mate is None and abs(ev.cp or 0) <= EQUAL_CP) if r["phase"] == "middlegame" \
            else (ev.mate is None and (ev.cp or 0) >= -EQUAL_CP) or (ev.mate is not None and ev.mate > 0)
        if not fits:
            continue
        seen.add((r["game_id"], r["phase"]))
        out[r["phase"]].append({
            "key": f"{r['game_id']}:{r['ply']}", "game_id": r["game_id"], "ply": r["ply"], "phase": r["phase"],
            "fen": r["fen"], "color": r["my_color"], "eval": str(ev), "result": r["my_result"],
            "opponent": r["black"] if r["my_color"] == "white" else r["white"], "date": r["date"]})
    return out


# ------------------------------------------------------------ playing

def outcome(board: chess.Board, me: chess.Color) -> tuple[str, str] | None:
    """('win' | 'draw' | 'loss', reason) once the game is over (draws by rule are taken at once)."""
    if board.is_checkmate():
        return ("win", "Checkmate!") if board.turn != me else ("loss", "Checkmated.")
    for test, reason in ((board.is_stalemate, "Stalemate"), (board.is_insufficient_material, "No mating material"),
                         (board.is_fifty_moves, "50-move rule"), (lambda: board.is_repetition(3), "Threefold repetition")):
        if test():
            return "draw", reason
    return None


def replay(fen: str, moves: list[str]) -> chess.Board:
    """The position after `moves` (UCI) from `fen`; ValueError for an illegal move."""
    board = chess.Board(fen)
    for uci in moves:
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise ValueError(f"illegal move {uci}")
        board.push(move)
    return board


# ------------------------------------------------------------ results

def record(conn: sqlite3.Connection, source: str, fen: str, color: str, elo: int, offset: int,
           moves: list[str], result: str, reason: str) -> int:
    with conn:
        return conn.execute(
            """INSERT INTO spar_games (source, start_fen, color, elo, elo_offset, moves, result, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (source, fen, color, elo, offset, " ".join(moves), result, reason)).lastrowid


def history(conn: sqlite3.Connection, limit: int = 20) -> dict:
    """My last games and my score by offset (so I can see where the level suits me)."""
    rows = conn.execute("SELECT * FROM spar_games ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    by_offset: dict[int, list[str]] = {}
    for r in rows:
        by_offset.setdefault(r["elo_offset"], []).append(r["result"])
    return {
        "games": [{"id": r["id"], "elo": r["elo"], "offset": r["elo_offset"], "result": r["result"],
                   "reason": r["reason"], "color": r["color"], "moves": len(r["moves"].split()), "at": r["at"]}
                  for r in rows],
        "by_offset": [{"offset": o, "won": rs.count("win"), "drawn": rs.count("draw"), "lost": rs.count("loss")}
                      for o, rs in sorted(by_offset.items())],
    }


def game(conn: sqlite3.Connection, game_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM spar_games WHERE id = ?", (game_id,)).fetchone()
