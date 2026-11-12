"""Stockfish analysis of every position in every game, run in the background.

Every position of a game (both sides' moves) is analysed once and stored in the
`positions` table. Scores are stored from White's point of view. A game is written
in a single transaction and marked `analysed_at`, so analysis is cached forever and
an interrupted run simply resumes with the games that are not finished yet.

For every position where it is my move (and I'm not in check) a second, null-move
search asks "what would the opponent do if I passed?": the opponent's threat. It feeds
the real-game training (threat check) and is stored in positions.threat_*.

AnalysisJob runs a pool of worker threads, each owning one Stockfish process, and
exposes progress counters that the CLI and the web app poll.
"""

import contextlib
import queue
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass, field

import chess
import chess.engine

from . import db
from .config import Config, effective_workers

# Stored instead of a mate score when the side to move is already checkmated.
MATED_CP = 10_000

# Bump when the stored analysis gains information; older games are re-analysed in the
# background. v2: second-best line (MultiPV 2), needed for "Great"/"Brilliant" marks.
ANALYSIS_VERSION = 2
# Bump when the threat search changes; games with an older (or no) version get a threat-only pass.
THREAT_VERSION = 1


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
    second_cp: int | None = None
    second_mate: int | None = None
    second_uci: str | None = None
    threat_cp: int | None = None      # eval after a null move (White's POV), my positions only
    threat_mate: int | None = None
    threat_uci: str | None = None     # the opponent's best move if I passed
    threat_pv: str | None = None      # ...and the line that follows (space-separated)


@dataclass
class PositionEval:
    """Engine view of one position, White's point of view."""
    score_cp: int | None
    score_mate: int | None
    best_uci: str | None
    pv_uci: str
    depth: int | None
    second_cp: int | None = None      # the second-best move's score (None if only one move)
    second_mate: int | None = None
    second_uci: str | None = None


class StockfishNotFound(RuntimeError):
    """No Stockfish executable at the configured path, on PATH or in the usual install folders."""


# Where package managers put Stockfish when it isn't on PATH (Homebrew, a manual install, Debian/Ubuntu's apt).
USUAL_PLACES = ("/opt/homebrew/bin/stockfish", "/usr/local/bin/stockfish", "/usr/games/stockfish")

INSTALL_HINT = (
    "Install Stockfish (macOS: `brew install stockfish`; Debian/Ubuntu: `sudo apt install stockfish`; "
    "Windows: download it from https://stockfishchess.org/download/), then put it on your PATH, set "
    "STOCKFISH_PATH, set [engine] path in config.toml or pass --stockfish PATH."
)


def find_stockfish(configured: str = "") -> str:
    """The Stockfish executable to run.

    A configured path (config, $STOCKFISH_PATH or --stockfish) must exist; a bare name is looked up on
    PATH. Without one: `stockfish` on PATH, then USUAL_PLACES. Raises StockfishNotFound otherwise.
    """
    if configured:
        found = shutil.which(configured)
        if found is None:
            raise StockfishNotFound(f"Stockfish not found at {configured!r}. {INSTALL_HINT}")
        return found
    found = shutil.which("stockfish") or next((p for p in USUAL_PLACES if shutil.which(p)), None)
    if found is None:
        raise StockfishNotFound(f"Stockfish not found. {INSTALL_HINT}")
    return found


def open_engine(cfg: Config, threads: int | None = None) -> chess.engine.SimpleEngine:
    """Start a Stockfish process with the configured threads and hash size."""
    engine = chess.engine.SimpleEngine.popen_uci(find_stockfish(cfg.engine.path))
    engine.configure({"Threads": threads or cfg.engine.threads, "Hash": cfg.engine.hash_mb})
    return engine


def analyse_position(engine: chess.engine.SimpleEngine, board: chess.Board,
                     cfg: Config) -> PositionEval:
    """Analyse one position with the two best lines (MultiPV 2)."""
    if board.is_checkmate():
        cp = -MATED_CP if board.turn == chess.WHITE else MATED_CP
        return PositionEval(cp, None, None, "", None)
    if board.is_game_over(claim_draw=False):  # stalemate, insufficient material, ...
        return PositionEval(0, None, None, "", None)

    limit = chess.engine.Limit(time=cfg.engine.time_per_position, depth=cfg.engine.max_depth or None)
    infos = engine.analyse(board, limit, multipv=2)
    first = infos[0]
    score = first["score"].white()
    pv = first.get("pv", [])[: cfg.engine.pv_length]
    result = PositionEval(
        score.score(), score.mate(),
        pv[0].uci() if pv else None,
        " ".join(m.uci() for m in pv),
        first.get("depth"),
    )
    if len(infos) > 1 and infos[1].get("pv"):
        second = infos[1]["score"].white()
        result.second_cp, result.second_mate = second.score(), second.mate()
        result.second_uci = infos[1]["pv"][0].uci()
    return result


def threat_board(board: chess.Board) -> chess.Board | None:
    """The position with the move passed to the opponent, or None if passing isn't possible
    (in check, or the game is over)."""
    if board.is_check() or board.is_game_over(claim_draw=False):
        return None
    passed = board.copy(stack=False)
    passed.push(chess.Move.null())
    return chess.Board(passed.fen())   # no null move in the history (UCI can't transmit it)


Threat = tuple[int | None, int | None, str | None, str | None]   # cp, mate, move, line


def analyse_threat(engine: chess.engine.SimpleEngine, board: chess.Board, cfg: Config) -> Threat:
    """(cp, mate, move, line) of the opponent's best play if the side to move passed (White's POV)."""
    passed = threat_board(board)
    if passed is None:
        return None, None, None, None
    limit = chess.engine.Limit(time=cfg.engine.time_per_position, depth=cfg.engine.max_depth or None)
    info = engine.analyse(passed, limit)
    score = info["score"].white()
    pv = info.get("pv", [])[: cfg.engine.pv_length]
    return score.score(), score.mate(), pv[0].uci() if pv else None, " ".join(m.uci() for m in pv) or None


def _game_moves(game: sqlite3.Row) -> tuple[chess.Board, list[chess.Move], chess.Color]:
    board = chess.Board(game["start_fen"]) if game["start_fen"] else chess.Board()
    moves = [chess.Move.from_uci(u) for u in game["moves_uci"].split()]
    return board, moves, chess.WHITE if game["my_color"] == "white" else chess.BLACK


def analyse_game(engine: chess.engine.SimpleEngine, game: sqlite3.Row, cfg: Config,
                 on_position=None, stop: threading.Event | None = None) -> list[PositionResult] | None:
    """Analyse every position of a game (plus threats on my moves). None if stopped part-way."""
    board, moves, me = _game_moves(game)
    results = []
    for ply in range(len(moves) + 1):
        if stop is not None and stop.is_set():
            return None
        move = moves[ply] if ply < len(moves) else None
        e = analyse_position(engine, board, cfg)
        r = PositionResult(
            ply=ply, fen=board.fen(),
            move_uci=move.uci() if move else None,
            move_san=board.san(move) if move else None,
            score_cp=e.score_cp, score_mate=e.score_mate, best_uci=e.best_uci, pv_uci=e.pv_uci,
            depth=e.depth, second_cp=e.second_cp, second_mate=e.second_mate, second_uci=e.second_uci,
        )
        if move and board.turn == me:
            r.threat_cp, r.threat_mate, r.threat_uci, r.threat_pv = analyse_threat(engine, board, cfg)
            if on_position:
                on_position()
        results.append(r)
        if on_position:
            on_position()
        if move:
            board.push(move)
    return results


def analyse_game_threats(engine: chess.engine.SimpleEngine, game: sqlite3.Row, cfg: Config,
                         on_position=None, stop: threading.Event | None = None
                         ) -> list[tuple[int, Threat]] | None:
    """Threat search only (for games analysed before threats existed): [(ply, threat)]."""
    board, moves, me = _game_moves(game)
    out = []
    for ply, move in enumerate(moves):
        if stop is not None and stop.is_set():
            return None
        if board.turn == me:
            out.append((ply, analyse_threat(engine, board, cfg)))
            if on_position:
                on_position()
        board.push(move)
    return out


def my_move_count(game: sqlite3.Row) -> int:
    """Positions where I'm to move (the threat search's work for a game)."""
    first_white = not game["start_fen"] or chess.Board(game["start_fen"]).turn == chess.WHITE
    mine_first = first_white == (game["my_color"] == "white")
    return (game["num_plies"] + (1 if mine_first else 0)) // 2


def save_game_analysis(conn: sqlite3.Connection, game_id: int,
                       results: list[PositionResult]) -> None:
    with conn:  # one transaction: a game is either fully stored or not at all
        conn.execute("DELETE FROM positions WHERE game_id = ?", (game_id,))
        conn.executemany(
            """INSERT INTO positions (game_id, ply, fen, move_uci, move_san, score_cp,
                   score_mate, best_uci, pv_uci, depth, second_cp, second_mate, second_uci,
                   threat_cp, threat_mate, threat_uci, threat_pv)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(game_id, r.ply, r.fen, r.move_uci, r.move_san, r.score_cp, r.score_mate,
              r.best_uci, r.pv_uci, r.depth, r.second_cp, r.second_mate, r.second_uci,
              r.threat_cp, r.threat_mate, r.threat_uci, r.threat_pv)
             for r in results],
        )
        # New evals invalidate the classification; ensure_classified() rebuilds it.
        conn.execute("DELETE FROM mistakes WHERE game_id = ?", (game_id,))
        conn.execute("""UPDATE games SET analysed_at = CURRENT_TIMESTAMP, analysis_version = ?,
                            threat_version = ? WHERE id = ?""", (ANALYSIS_VERSION, THREAT_VERSION, game_id))


def save_threats(conn: sqlite3.Connection, game_id: int, threats: list[tuple[int, Threat]]) -> None:
    with conn:
        conn.executemany("""UPDATE positions SET threat_cp = ?, threat_mate = ?, threat_uci = ?, threat_pv = ?
                            WHERE game_id = ? AND ply = ?""",
                         [(*threat, game_id, ply) for ply, threat in threats])
        conn.execute("UPDATE games SET threat_version = ? WHERE id = ?", (THREAT_VERSION, game_id))


def pending_games(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    """Games never analysed (newest first), then games analysed by an older version."""
    sql = f"""SELECT * FROM games
              WHERE analysed_at IS NULL OR COALESCE(analysis_version, 1) < {ANALYSIS_VERSION}
              ORDER BY analysed_at IS NOT NULL, end_time DESC, id DESC"""
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def pending_threat_games(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Fully analysed games that still need the threat search (newest first)."""
    return conn.execute(f"""SELECT * FROM games
                            WHERE analysed_at IS NOT NULL AND analysis_version >= {ANALYSIS_VERSION}
                              AND COALESCE(threat_version, 0) < {THREAT_VERSION}
                            ORDER BY end_time DESC, id DESC""").fetchall()


def count_pending(conn: sqlite3.Connection) -> int:
    """Games waiting for (re-)analysis, including the threat-only pass."""
    return conn.execute(f"""SELECT COUNT(*) FROM games WHERE analysed_at IS NULL
                            OR COALESCE(analysis_version, 1) < {ANALYSIS_VERSION}
                            OR COALESCE(threat_version, 0) < {THREAT_VERSION}""").fetchone()[0]


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

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for the job to end; True once it is no longer running."""
        if self._thread:
            self._thread.join(timeout)
        return not self.is_running()

    def _run(self) -> None:
        attempted: set[int] = set()
        try:
            # Keep going in rounds so games uploaded while a job is running get picked up too.
            while not self._stop.is_set():
                conn = db.connect(self.cfg.db_path)
                try:
                    games = [g for g in pending_games(conn, self.limit) if g["id"] not in attempted]
                    if self.limit:
                        games = games[: max(0, self.limit - len(attempted))]
                    # Threat-only pass for games analysed before threats existed (not when limited:
                    # `analyse --limit` is about new games).
                    threat_only = [] if self.limit else [
                        g for g in pending_threat_games(conn) if g["id"] not in attempted]
                finally:
                    conn.close()
                if not games and not threat_only:
                    break
                attempted.update(g["id"] for g in games + threat_only)
                with self._lock:
                    self.progress.games_total += len(games) + len(threat_only)
                    self.progress.positions_total += (sum(g["num_plies"] + 1 + my_move_count(g) for g in games)
                                                      + sum(my_move_count(g) for g in threat_only))

                work: queue.Queue = queue.Queue()
                for g in games:
                    work.put(("full", g))
                for g in threat_only:
                    work.put(("threats", g))
                games = games + threat_only
                n_workers = max(1, min(effective_workers(self.cfg.engine), len(games)))
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
                    task, game = work.get_nowait()
                except queue.Empty:
                    break
                analyse = analyse_game if task == "full" else analyse_game_threats
                try:
                    results = analyse(engine, game, self.cfg,
                                      on_position=self._count_position, stop=self._stop)
                except chess.engine.EngineError as exc:
                    # Engine crashed or misbehaved: note it, restart and carry on.
                    self.progress.errors.append(f"game {game['id']}: {exc}")
                    _quit_quietly(engine)
                    engine = open_engine(self.cfg)
                    continue
                if results is None:
                    break
                if task == "full":
                    save_game_analysis(conn, game["id"], results)
                else:
                    save_threats(conn, game["id"], results)
                with self._lock:
                    self.progress.games_done += 1
        except Exception as exc:
            self.progress.errors.append(str(exc))
        finally:
            if engine is not None:
                _quit_quietly(engine)
            conn.close()


def _quit_quietly(engine: chess.engine.SimpleEngine) -> None:
    with contextlib.suppress(Exception):
        engine.quit()
