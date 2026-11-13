"""Endgame tablebase: perfect play and exact results for positions with 7 pieces or fewer (E3).

Asks the free Lichess tablebase API (Syzygy up to 7 pieces, DTM up to 5; https://tablebase.lichess.ovh):

  GET https://tablebase.lichess.ovh/standard?fen=<FEN>
  -> {"category": "win", "dtz": 9, "dtm": 43, "moves": [{"uci", "san", "category", "dtz", "dtm"}, ...]}

`category` is from the side to move; a move's category is from the opponent's side after it.
"cursed-win" wins only by ignoring the 50-move rule and "blessed-loss" loses only that way: our
playouts apply the rule, so both count as (almost) draws: WDL +1 / -1.

Answers are cached in memory and in SQLite (`tablebase_cache`, keyed by EPD + halfmove clock: the
API's answer depends on it, a win turns "cursed" near the 50-move limit), so an endgame
played once works offline afterwards. Every failure (offline, timeout, HTTP error, rate limit)
gives None and the caller falls back to Stockfish; after one, the API is left alone for a while
(BACKOFF seconds, RATE_LIMIT_BACKOFF after HTTP 429) so a dead network doesn't slow every move.
Settings can switch it off (meta `tablebase` = 'off').
"""

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass

import chess

from . import db

API = "https://tablebase.lichess.ovh/standard"
USER_AGENT = "chess-review (local training app)"
MAX_PIECES = 7
TIMEOUT = 3.0  # seconds; then Stockfish plays
BACKOFF = 30.0  # seconds without asking after a failure
RATE_LIMIT_BACKOFF = 60.0  # ... after HTTP 429
MEMORY_SIZE = 4096
SETTING_KEY = "tablebase"  # meta: 'on' (default) | 'off'

WDL = {
    "win": 2,
    "maybe-win": 2,
    "syzygy-win": 2,
    "cursed-win": 1,
    "draw": 0,
    "blessed-loss": -1,
    "maybe-loss": -2,
    "syzygy-loss": -2,
    "loss": -2,
}


@dataclass(frozen=True)
class Move:
    """A move from the tablebase answer, with its result for the side that plays it."""

    uci: str
    san: str
    wdl: int  # for the side that plays it
    dtm: int | None
    dtz: int | None
    checkmate: bool = False

    @property
    def distance(self) -> tuple[int, int]:
        """How far the end is: mate in 0 for a mating move (the API gives it no DTM), else DTM where
        known, else DTZ."""
        if self.checkmate:
            return (0, 0)
        return (abs(self.dtm) if self.dtm is not None else abs(self.dtz or 0), abs(self.dtz or 0))


@dataclass(frozen=True)
class TbResult:
    """The tablebase's verdict on a position and its moves."""

    wdl: int  # side to move: 2 win, 1 cursed win, 0 draw, -1 blessed loss, -2 loss
    dtm: int | None  # moves to mate (signed, side to move), when the API knows it (<= 5 pieces)
    dtz: int | None
    moves: tuple[Move, ...]

    @property
    def best(self) -> Move | None:
        """Perfect play: the quickest win (DTM, else DTZ), else a draw, else the longest defence."""
        if not self.moves:
            return None

        def key(m: Move) -> tuple:
            if m.wdl > 0:
                return (m.wdl, tuple(-x for x in m.distance))  # winning: shortest
            return (m.wdl, m.distance)  # drawn or losing: longest

        return max(self.moves, key=key)


def choose(tb: TbResult, engine_move: chess.Move | None) -> Move | None:
    """The move to play (the defender's reply, a hint): perfect play, but in a draw the engine's own
    choice when it keeps the draw (every drawing move scores the same; the engine's sets the most
    problems, or is the natural defence)."""
    best = tb.best
    if best is None or engine_move is None:
        return best
    same = next((m for m in tb.moves if m.uci == engine_move.uci()), None)
    return same if same is not None and same.wdl == best.wdl and best.wdl == 0 else best


def verdict(wdl: int) -> str:
    """A WDL value in words."""
    return {
        2: "win",
        1: "win, but not within the 50-move rule",
        0: "draw",
        -1: "loss, but saved by the 50-move rule",
        -2: "loss",
    }[wdl]


# ------------------------------------------------------------ state

_memory: OrderedDict[str, TbResult] = OrderedDict()
_lock = threading.Lock()
_blocked_until = 0.0


def clear_cache() -> None:
    """Forget the in-memory answers and any back-off (tests, restore)."""
    global _blocked_until
    with _lock:
        _memory.clear()
        _blocked_until = 0.0


def enabled(conn: sqlite3.Connection) -> bool:
    """Whether tablebase lookups are on (Settings)."""
    return db.get_meta(conn, SETTING_KEY) != "off"


def set_enabled(conn: sqlite3.Connection, on: bool) -> None:
    """Turn tablebase lookups on or off."""
    with conn:
        db.set_meta(conn, SETTING_KEY, "on" if on else "off")


def eligible(board: chess.Board) -> bool:
    """In the tablebase: 7 pieces or fewer (kings included) and no castling rights."""
    return chess.popcount(board.occupied) <= MAX_PIECES and not board.castling_rights


# ------------------------------------------------------------ probing


def probe(conn: sqlite3.Connection, board: chess.Board) -> TbResult | None:
    """The tablebase's answer for this position, or None (not eligible, switched off, game over,
    or the API can't be reached: use Stockfish)."""
    if not eligible(board) or board.is_game_over() or not enabled(conn):
        return None
    key = f"{board.epd()} {board.halfmove_clock}"
    with _lock:
        if key in _memory:
            _memory.move_to_end(key)
            return _memory[key]
    row = conn.execute("SELECT data FROM tablebase_cache WHERE epd = ?", (key,)).fetchone()
    if row is not None:
        result = _parse(json.loads(row["data"]))
    else:
        data = _ask(board.fen())
        if data is None:
            return None
        result = _parse(data)
        if result is None:
            return None
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO tablebase_cache (epd, data) VALUES (?, ?)", (key, json.dumps(_compact(data)))
            )
    if result is not None:
        with _lock:
            _memory[key] = result
            while len(_memory) > MEMORY_SIZE:
                _memory.popitem(last=False)
    return result


def _ask(fen: str) -> dict | None:
    """One API request, or None (and a back-off) on any failure."""
    global _blocked_until
    with _lock:
        if time.monotonic() < _blocked_until:
            return None
    try:
        return fetch(f"{API}?{urllib.parse.urlencode({'fen': fen})}")
    except urllib.error.HTTPError as exc:
        wait = RATE_LIMIT_BACKOFF if exc.code == 429 else BACKOFF
    except OSError, ValueError:  # offline, timeout, not JSON
        wait = BACKOFF
    with _lock:
        _blocked_until = time.monotonic() + wait
    return None


def fetch(url: str) -> dict:
    """The HTTP call (replaced in tests: they never touch the network)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.load(response)


def _compact(data: dict) -> dict:
    keep = ("category", "dtm", "dtz")
    return {
        **{k: data.get(k) for k in keep},
        "moves": [{k: m.get(k) for k in ("uci", "san", "checkmate", *keep)} for m in data.get("moves", [])],
    }


def _parse(data: dict) -> TbResult | None:
    if data.get("category") not in WDL:
        return None  # "unknown": not in the tablebase after all
    moves = tuple(
        Move(
            m["uci"],
            m.get("san") or m["uci"],
            -WDL[m["category"]],
            m.get("dtm"),
            m.get("dtz"),
            bool(m.get("checkmate")),
        )
        for m in data.get("moves", [])
        if m.get("category") in WDL
    )
    return TbResult(WDL[data["category"]], data.get("dtm"), data.get("dtz"), moves)
