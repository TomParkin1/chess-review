"""My opening repertoire, built from my own games, and Chessly-style line drills.

The tree holds every position of my games up to MAX_PLY, per colour, keyed by position
(EPD) so transpositions merge. Each node knows how often each move was played there, my
results after it, and (for my moves) how the engine judged them.

My *repertoire move* in a position is the move I play most often there (at least
MIN_GAMES games), unless the engine usually judged it an inaccuracy or worse: then the
engine's best move replaces it and the position is flagged as a "repertoire problem".

Drills replay lines from move 1: the opponent's moves are picked in proportion to how
often my opponents played them, and I must find my repertoire move. Each of my positions
is a spaced-repetition card (cards.kind = 'line', key = '<colour>:<epd>').

`build()` keeps the last few trees in memory (F17): a tree is reused until the games or their
classification change (`_stamp`), so clicking through the explorer doesn't rebuild it.
"""

import random
import sqlite3
import threading
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Any

import chess

from . import openings

MAX_PLY = 20
MIN_GAMES = 2
BAD = {"inaccuracy", "mistake", "blunder"}
CACHE_SIZE = 8

_cache: OrderedDict = OrderedDict()  # (database, colour, family, stamp) -> tree
_cache_lock = threading.Lock()


@dataclass
class Edge:
    """A move from a position in my games, with how it went."""

    uci: str
    san: str
    count: int = 0
    results: Counter = field(default_factory=Counter)  # my results after this move
    judgements: Counter = field(default_factory=Counter)  # engine verdicts on this move
    drops: list[float] = field(default_factory=list)  # win-chance lost by this move


@dataclass
class Node:
    """A position in my games (by EPD) and the moves played from it."""

    epd: str
    fen: str
    ply: int
    path: list[str]  # first path seen from the start
    my_turn: bool
    games: int = 0
    results: Counter = field(default_factory=Counter)
    edges: dict[str, Edge] = field(default_factory=dict)
    best: Counter = field(default_factory=Counter)  # engine best moves seen here

    def repertoire(self) -> tuple[Edge | None, str | None, bool]:
        """(my usual move, the move to learn, is my usual move a problem?)."""
        usual = max((e for e in self.edges.values() if e.count >= MIN_GAMES), key=lambda e: e.count, default=None)
        if usual is None:
            return None, None, False
        bad_share = sum(usual.judgements[j] for j in BAD) / max(1, sum(usual.judgements.values()))
        if bad_share > 0.5 and self.best:
            return usual, self.best.most_common(1)[0][0], True
        return usual, usual.uci, False


def build(conn: sqlite3.Connection, color: str, family: str | None = None) -> dict[str, Node]:
    """The repertoire tree for my games as `color` (optionally one opening family), from the
    cache while nothing it is built from has changed. Callers must not modify it."""
    key = (conn.execute("PRAGMA database_list").fetchone()["file"], color, family, _stamp(conn))
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    tree = _build(conn, color, family)
    with _cache_lock:
        _cache[key] = tree
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return tree


def clear_cache() -> None:
    """Forget cached trees (after a restore replaces the database)."""
    with _cache_lock:
        _cache.clear()


def _stamp(conn: sqlite3.Connection) -> tuple:
    """Changes whenever a game is imported, analysed or deleted, or any game is (re)classified:
    classify_game always inserts fresh mistakes rows, so their max rowid grows."""
    return tuple(
        conn.execute("""SELECT (SELECT COUNT(*) FROM games), (SELECT MAX(id) FROM games),
                               (SELECT MAX(analysed_at) FROM games), (SELECT MAX(rowid) FROM mistakes)""").fetchone()
    )


def _build(conn: sqlite3.Connection, color: str, family: str | None) -> dict[str, Node]:
    sql = "SELECT * FROM games WHERE my_color = ? AND start_fen IS NULL"
    params: list = [color]
    if family:
        sql += " AND opening_family = ?"
        params.append(family)
    games = conn.execute(sql, params).fetchall()
    ids = [g["id"] for g in games]
    marks = ",".join("?" * len(ids)) or "NULL"
    verdicts = {
        (r["game_id"], r["ply"]): r
        for r in conn.execute(
            f"SELECT game_id, ply, judgement, win_drop FROM mistakes WHERE game_id IN ({marks}) AND ply < ?",
            ids + [MAX_PLY],
        )
    }
    bests = {
        (r["game_id"], r["ply"]): r["best_uci"]
        for r in conn.execute(
            f"SELECT game_id, ply, best_uci FROM positions WHERE game_id IN ({marks}) AND ply < ?", ids + [MAX_PLY]
        )
    }

    me = chess.WHITE if color == "white" else chess.BLACK
    tree: dict[str, Node] = {}
    for g in games:
        board = chess.Board()
        path: list[str] = []
        for ply, uci in enumerate(g["moves_uci"].split()[:MAX_PLY]):
            epd = board.epd()
            node = tree.get(epd)
            if node is None:
                node = tree[epd] = Node(epd, board.fen(), ply, list(path), board.turn == me)
            node.games += 1
            node.results[g["my_result"]] += 1
            if bests.get((g["id"], ply)):
                node.best[bests[(g["id"], ply)]] += 1
            move = chess.Move.from_uci(uci)
            edge = node.edges.get(uci)
            if edge is None:
                edge = node.edges[uci] = Edge(uci, board.san(move))
            edge.count += 1
            edge.results[g["my_result"]] += 1
            v = verdicts.get((g["id"], ply))
            if v is not None:
                edge.judgements[v["judgement"]] += 1
                if v["win_drop"] is not None:
                    edge.drops.append(v["win_drop"])
            board.push(move)
            path.append(uci)
    return tree


def root_epd() -> str:
    """EPD of the starting position."""
    return chess.Board().epd()


def node_json(tree: dict[str, Node], epd: str) -> dict | None:
    """A position of the tree for the Openings page; None if it isn't in my games."""
    node = tree.get(epd)
    if node is None:
        return None
    board = chess.Board(node.fen)
    usual, learn, problem = node.repertoire() if node.my_turn else (None, None, False)
    moves = []
    for e in sorted(node.edges.values(), key=lambda e: -e.count):
        child = board.copy(stack=False)
        child.push_uci(e.uci)
        n = e.count
        moves.append(
            {
                "uci": e.uci,
                "san": e.san,
                "games": n,
                "wins": e.results["win"],
                "draws": e.results["draw"],
                "losses": e.results["loss"],
                "score": 100 * (e.results["win"] + 0.5 * e.results["draw"]) / n,
                "book": openings.is_book(child),
                "errors": sum(e.judgements[j] for j in BAD) if node.my_turn else None,
                "avg_drop": sum(e.drops) / len(e.drops) if (node.my_turn and e.drops) else None,
                "repertoire": node.my_turn and e.uci == learn,
                "usual": usual is not None and e.uci == usual.uci,
            }
        )
    name = openings._positions().get(epd)
    learn_san = board.san(chess.Move.from_uci(learn)) if learn else None
    best = node.best.most_common(1)[0][0] if node.best else None
    return {
        "fen": node.fen,
        "epd": epd,
        "ply": node.ply,
        "path": node.path,
        "my_turn": node.my_turn,
        "games": node.games,
        "wins": node.results["win"],
        "draws": node.results["draw"],
        "losses": node.results["loss"],
        "name": name[1] if name else None,
        "moves": moves,
        "learn_uci": learn,
        "learn_san": learn_san,
        "problem": problem,
        "usual_san": usual.san if usual else None,
        "best_uci": best,
        "best_san": board.san(chess.Move.from_uci(best)) if best else None,
    }


def problems(tree: dict[str, Node], limit: int = 12) -> list[dict]:
    """Positions where my usual move is usually judged bad, most-played first."""
    out: list[dict[str, Any]] = []
    for node in tree.values():
        if not node.my_turn:
            continue
        usual, learn, problem = node.repertoire()
        if not problem or usual is None or learn is None:
            continue
        board = chess.Board(node.fen)
        drops = usual.drops
        out.append(
            {
                "epd": node.epd,
                "fen": node.fen,
                "path": node.path,
                "ply": node.ply,
                "games": usual.count,
                "usual_san": usual.san,
                "learn_san": board.san(chess.Move.from_uci(learn)),
                "learn_uci": learn,
                "usual_uci": usual.uci,
                "avg_drop": sum(drops) / len(drops) if drops else None,
                "line": chess.Board().variation_san([chess.Move.from_uci(u) for u in node.path]) if node.path else "",
            }
        )
    out.sort(key=lambda p: (-p["games"], p["ply"]))
    return out[:limit]


def card_key(color: str, epd: str) -> str:
    """The card key of a repertoire position: '<colour>:<epd>'."""
    return f"{color}:{epd}"


def drill_lines(tree: dict[str, Node], color: str, count: int = 6, seed: int | None = None) -> list[list[dict]]:
    """Random lines through my repertoire: opponent moves weighted by how often they were
    played against me (only moves seen in >= MIN_GAMES games), my moves = repertoire moves."""
    rng = random.Random(seed)
    lines: list[list[dict]] = []
    seen: set = set()
    for _ in range(count * 4):
        if len(lines) >= count:
            break
        node = tree.get(root_epd())
        steps: list[dict] = []
        while node is not None and node.ply < MAX_PLY:
            board = chess.Board(node.fen)
            if node.my_turn:
                _, learn, problem = node.repertoire()
                if learn is None:
                    break
                steps.append(
                    {
                        "uci": learn,
                        "san": board.san(chess.Move.from_uci(learn)),
                        "mine": True,
                        "key": card_key(color, node.epd),
                        "fix": problem,
                    }
                )
                board.push_uci(learn)
            else:
                options = [e for e in node.edges.values() if e.count >= MIN_GAMES]
                if not options:
                    break
                e = rng.choices(options, weights=[o.count for o in options])[0]
                steps.append({"uci": e.uci, "san": e.san, "mine": False})
                board.push_uci(e.uci)
            node = tree.get(board.epd())
        signature = tuple(s["uci"] for s in steps)
        if any(s["mine"] for s in steps) and signature not in seen:
            seen.add(signature)
            lines.append(steps)
    return lines


def due_exercises(tree: dict[str, Node], color: str, keys: list[str]) -> list[list[dict]]:
    """One short 'line' per due card: the moves leading to the position, then my move."""
    out = []
    for key in keys:
        epd = key.split(":", 1)[1]
        node = tree.get(epd)
        if node is None or not node.my_turn:
            continue
        _, learn, problem = node.repertoire()
        if learn is None:
            continue
        board = chess.Board()
        steps = []
        for uci in node.path:
            move = chess.Move.from_uci(uci)
            steps.append({"uci": uci, "san": board.san(move), "mine": False, "setup": True})
            board.push(move)
        steps.append(
            {"uci": learn, "san": board.san(chess.Move.from_uci(learn)), "mine": True, "key": key, "fix": problem}
        )
        out.append(steps)
    return out
