"""Stockfish analysis of every position in every game, run in the background.

Every position of a game (both sides' moves) is analysed once and stored in the
`positions` table. Scores are stored from White's point of view. A game is written
in a single transaction and marked `analysed_at`, so analysis is cached forever and
an interrupted run simply resumes with the games that are not finished yet.

AnalysisJob runs a pool of worker threads, each owning one Stockfish process, and
exposes progress counters that the CLI and the web app poll.
"""

import queue
import sqlite3
import threading
import time
from dataclasses import dataclass, field

import chess
import chess.engine

from . import db
from .config import Config

# Stored instead of a mate score when the side to move is already checkmated.
MATED_CP = 10_000


@dataclass
class PositionResult:
    ply: int
    fen: str
    move_uci: str | None
    move_san: str | None
    score_cp: int | None
    score_mate: int | None
    best_uci: str | None
    pv_uci: str
    depth: int | None


def open_engine(cfg: Config) -> chess.engine.SimpleEngine:
    engine = chess.engine.SimpleEngine.popen_uci(cfg.engine.path)
    engine.configure({"Threads": cfg.engine.threads, "Hash": cfg.engine.hash_mb})
    return engine


def analyse_position(engine: chess.engine.SimpleEngine, board: chess.Board,
                     cfg: Config) -> tuple[int | None, int | None, str | None, str, int | None]:
    """Return (score_cp, score_mate, best_uci, pv_uci, depth), White's point of view."""
    if board.is_checkmate():
        cp = -MATED_CP if board.turn == chess.WHITE else MATED_CP
        return cp, None, None, "", None
    if board.is_game_over(claim_draw=False):  # stalemate, insufficient material, ...
        return 0, None, None, "", None

    info = engine.analyse(board, chess.engine.Limit(time=cfg.engine.time_per_position))
    score = info["score"].white()
    pv = info.get("pv", [])[: cfg.engine.pv_length]
    return (
        score.score(),
        score.mate(),
        pv[0].uci() if pv else None,
        " ".join(m.uci() for m in pv),
        info.get("depth"),
    )


def analyse_game(engine: chess.engine.SimpleEngine, game: sqlite3.Row, cfg: Config,
                 on_position=None, stop: threading.Event | None = None) -> list[PositionResult] | None:
    """Analyse every position of a game. Returns None if stopped part-way."""
    board = chess.Board(game["start_fen"]) if game["start_fen"] else chess.Board()
    moves = [chess.Move.from_uci(u) for u in game["moves_uci"].split()]
    results = []
    for ply in range(len(moves) + 1):
        if stop is not None and stop.is_set():
            return None
        move = moves[ply] if ply < len(moves) else None
        cp, mate, best, pv, depth = analyse_position(engine, board, cfg)
        results.append(PositionResult(
            ply=ply, fen=board.fen(),
            move_uci=move.uci() if move else None,
            move_san=board.san(move) if move else None,
            score_cp=cp, score_mate=mate, best_uci=best, pv_uci=pv, depth=depth,
        ))
        if on_position:
            on_position()
        if move:
            board.push(move)
    return results


def save_game_analysis(conn: sqlite3.Connection, game_id: int,
                       results: list[PositionResult]) -> None:
    with conn:  # one transaction: a game is either fully stored or not at all
        conn.execute("DELETE FROM positions WHERE game_id = ?", (game_id,))
        conn.executemany(
            """INSERT INTO positions (game_id, ply, fen, move_uci, move_san, score_cp,
                   score_mate, best_uci, pv_uci, depth) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [(game_id, r.ply, r.fen, r.move_uci, r.move_san, r.score_cp, r.score_mate,
              r.best_uci, r.pv_uci, r.depth) for r in results],
        )
        conn.execute("UPDATE games SET analysed_at = CURRENT_TIMESTAMP WHERE id = ?", (game_id,))


def pending_games(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM games WHERE analysed_at IS NULL ORDER BY date DESC, id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


@dataclass
class Progress:
    state: str = "idle"            # idle | running | stopping | done | error
    games_total: int = 0
    games_done: int = 0
    positions_total: int = 0
    positions_done: int = 0
    started_at: float | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def fraction(self) -> float:
        return self.positions_done / self.positions_total if self.positions_total else 0.0

    @property
    def eta_seconds(self) -> float | None:
        if not self.started_at or not self.positions_done:
            return None
        elapsed = time.time() - self.started_at
        return elapsed / self.positions_done * (self.positions_total - self.positions_done)

    def as_dict(self) -> dict:
        return {
            "state": self.state, "games_total": self.games_total, "games_done": self.games_done,
            "positions_total": self.positions_total, "positions_done": self.positions_done,
            "fraction": self.fraction, "eta_seconds": self.eta_seconds, "errors": self.errors[-5:],
        }


class AnalysisJob:
    """Analyse all un-analysed games with a pool of Stockfish workers in the background."""

    def __init__(self, cfg: Config, limit: int | None = None):
        self.cfg = cfg
        self.limit = limit
        self.progress = Progress()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self.progress = Progress(state="running", started_at=time.time())
        self._thread = threading.Thread(target=self._run, daemon=True, name="analysis-job")
        self._thread.start()

    def stop(self) -> None:
        if self.is_running():
            self.progress.state = "stopping"
            self._stop.set()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        try:
            conn = db.connect(self.cfg.db_path)
            games = pending_games(conn, self.limit)
            conn.close()
            self.progress.games_total = len(games)
            self.progress.positions_total = sum(g["num_plies"] + 1 for g in games)

            work: queue.Queue = queue.Queue()
            for g in games:
                work.put(g)
            n_workers = max(1, min(self.cfg.engine.workers, len(games)))
            workers = [threading.Thread(target=self._worker, args=(work,), daemon=True)
                       for _ in range(n_workers)]
            for w in workers:
                w.start()
            for w in workers:
                w.join()
            self.progress.state = "done"
        except Exception as exc:  # surface unexpected failures (e.g. bad engine path)
            self.progress.errors.append(str(exc))
            self.progress.state = "error"

    def _count_position(self) -> None:
        with self._lock:
            self.progress.positions_done += 1

    def _worker(self, work: queue.Queue) -> None:
        conn = db.connect(self.cfg.db_path)
        engine = None
        try:
            engine = open_engine(self.cfg)
            while not self._stop.is_set():
                try:
                    game = work.get_nowait()
                except queue.Empty:
                    break
                try:
                    results = analyse_game(engine, game, self.cfg,
                                           on_position=self._count_position, stop=self._stop)
                except chess.engine.EngineError as exc:
                    # Engine crashed or misbehaved: note it, restart and carry on.
                    self.progress.errors.append(f"game {game['id']}: {exc}")
                    _quit_quietly(engine)
                    engine = open_engine(self.cfg)
                    continue
                if results is None:
                    break
                save_game_analysis(conn, game["id"], results)
                with self._lock:
                    self.progress.games_done += 1
        except Exception as exc:
            self.progress.errors.append(str(exc))
        finally:
            if engine is not None:
                _quit_quietly(engine)
            conn.close()


def _quit_quietly(engine: chess.engine.SimpleEngine) -> None:
    try:
        engine.quit()
    except Exception:
        pass
