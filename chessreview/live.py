"""Interactive engine work for the web app: judging moves I try, and live analysis.

Two long-lived Stockfish processes are started on first use:
  - one judges moves in review mode (short fixed-time searches, cached per position)
  - one streams live multi-line analysis for the analysis board
Call LiveEngine.close() on shutdown: python-chess engine threads keep the
interpreter alive otherwise (`serve` does this in a finally block).
"""

import queue
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import chess
import chess.engine

from . import explain
from .classify import Eval, Thresholds, judge, keeps_long_mate
from .config import Config
from .engine import open_engine

CACHE_SIZE = 2000
LIVE_THREADS = 4   # interactive work: one position at a time, so give it more threads


@dataclass(frozen=True)
class PositionAnalysis:
    """Engine view of a position. `score` is from White's point of view."""
    score: Eval
    best: chess.Move | None
    pv: tuple[chess.Move, ...] = ()

    def for_side(self, color: chess.Color) -> Eval:
        sign = 1 if color == chess.WHITE else -1
        s = self.score
        return Eval(s.cp * sign if s.cp is not None else None,
                    s.mate * sign if s.mate is not None else None)


@dataclass
class Judgement:
    uci: str
    san: str
    judgement: str                 # good | inaccuracy | mistake | blunder
    is_best: bool
    before: Eval                   # my point of view
    after: Eval                    # my point of view
    win_drop: float
    best_uci: str | None
    kind: str | None = None        # explanation category when the move is not good
    explanation: str | None = None
    fen_after: str = ""
    reply_uci: str | None = None   # opponent's best reply after my move
    reply_san: str | None = None
    fen_after_reply: str | None = None
    game_over: bool = False

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["before"], d["after"] = str(self.before), str(self.after)
        return d


@dataclass
class _Stream:
    cancel: threading.Event = field(default_factory=threading.Event)


class LiveEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._judge_engine: chess.engine.SimpleEngine | None = None
        self._stream_engine: chess.engine.SimpleEngine | None = None
        self._judge_lock = threading.Lock()
        self._stream_lock = threading.Lock()
        self._current_stream: _Stream | None = None
        self._cache: OrderedDict[str, PositionAnalysis] = OrderedDict()
        self._cache_lock = threading.Lock()     # requests run on several threads

    # ------------------------------------------------------------ judging

    def analyse(self, board: chess.Board) -> PositionAnalysis:
        """Short search of one position (cached by FEN)."""
        if board.is_checkmate():  # side to move is mated
            return PositionAnalysis(Eval(None, -1 if board.turn == chess.WHITE else 1), None)
        if board.is_game_over(claim_draw=False):
            return PositionAnalysis(Eval(0, None), None)
        key = board.fen()
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached

        with self._judge_lock:
            if self._judge_engine is None:
                self._judge_engine = open_engine(self.cfg, threads=LIVE_THREADS)
            try:
                info = self._judge_engine.analyse(
                    board, chess.engine.Limit(time=self.cfg.review.live_time, depth=22))
            except chess.engine.EngineError:
                _quit(self._judge_engine)
                self._judge_engine = None  # restart on the next request
                raise
        score = info["score"].white()
        pv = tuple(info.get("pv", []))
        result = PositionAnalysis(Eval(score.score(), score.mate()), pv[0] if pv else None, pv)
        with self._cache_lock:
            self._cache[key] = result
            if len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return result

    def judge(self, fen: str, uci: str, before: PositionAnalysis | None = None,
              thresholds: Thresholds | None = None, prev_capture: chess.Square | None = None) -> Judgement:
        """Judge my move `uci` in `fen` and work out the opponent's best reply.

        `before` lets the caller pass the stored analysis of the position (so the
        first move of a review puzzle is judged against the same eval as the game),
        and `prev_capture` where the opponent's last move captured, if known.
        Raises ValueError for an illegal move.
        """
        board = chess.Board(fen)
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise ValueError(f"illegal move {uci}")
        me = board.turn
        before = before or self.analyse(board)
        san = board.san(move)

        after_board = board.copy(stack=False)
        after_board.push(move)
        after = self.analyse(after_board)

        before_eval, after_eval = before.for_side(me), after.for_side(me)
        verdict, drop, _ = judge(before_eval, after_eval, self.cfg, thresholds)
        is_best = before.best is not None and move == before.best
        if is_best or keeps_long_mate(before_eval, after_eval):
            verdict = "good"   # (a long mate the 0.35 s search after the move can't see is still a mate)

        result = Judgement(
            uci=uci, san=san, judgement=verdict, is_best=is_best,
            before=before_eval, after=after_eval, win_drop=drop,
            best_uci=before.best.uci() if before.best else None,
            fen_after=after_board.fen(), game_over=after_board.is_game_over(),
        )
        if verdict != "good":
            result.kind, result.explanation = explain.explain(explain.MoveContext(
                board=board, move=move, best=before.best, best_line=list(before.pv),
                reply_line=list(after.pv), before=before_eval, after=after_eval, prev_capture=prev_capture,
                min_loss=thresholds.min_loss if thresholds else explain.MIN_LOSS),
                reveal_best=False)  # an attempt at a puzzle: don't give the answer away
        if after.best is not None and not result.game_over:
            result.reply_uci = after.best.uci()
            result.reply_san = after_board.san(after.best)
            reply_board = after_board.copy(stack=False)
            reply_board.push(after.best)
            result.fen_after_reply = reply_board.fen()
        return result

    # ------------------------------------------------------------ live analysis

    def stream(self, fen: str):
        """Yield analysis snapshots for `fen` as the search deepens.

        Each snapshot: {"depth", "lines": [{"eval": {cp, mate} (White POV), "uci", "pv_uci",
        "san_line"}]}; the last one has "done": True. Starting a new stream stops the
        previous one. The search runs on a worker thread and hands snapshots over a
        queue, so a slow or abandoned reader never blocks the engine.
        """
        board = chess.Board(fen)
        if board.is_game_over(claim_draw=False):
            final = self.analyse(board).score
            yield {"depth": 0, "lines": [], "eval": {"cp": final.cp, "mate": final.mate}, "done": True}
            return

        me = _Stream()
        previous, self._current_stream = self._current_stream, me
        if previous is not None:
            previous.cancel.set()
        out: queue.Queue = queue.Queue()
        threading.Thread(target=self._search, args=(board, me, out), daemon=True).start()
        try:
            while True:
                snapshot = out.get()
                if snapshot is None:
                    return
                yield snapshot
        finally:
            me.cancel.set()  # reader went away: stop searching

    def _search(self, board: chess.Board, me: _Stream, out: queue.Queue) -> None:
        a = self.cfg.analysis
        try:
            with self._stream_lock:
                if me.cancel.is_set():
                    return
                if self._stream_engine is None:
                    self._stream_engine = open_engine(self.cfg, threads=LIVE_THREADS)
                lines: dict[int, dict] = {}
                last_emit, last_depth = 0.0, 0
                wanted = min(a.multipv, board.legal_moves.count())
                with self._stream_engine.analysis(board, chess.engine.Limit(time=a.max_seconds),
                                                  multipv=a.multipv) as analysis:
                    for info in analysis:
                        if me.cancel.is_set():
                            return
                        if "pv" not in info or "score" not in info:
                            continue
                        lines[info.get("multipv", 1)] = _line_json(board, info)
                        depth = info.get("depth", 0)
                        now = time.monotonic()
                        # Emit on a new depth once all lines are known, at most ~5 times a second.
                        if depth > last_depth and len(lines) >= wanted and now - last_emit > 0.2:
                            last_emit, last_depth = now, depth
                            out.put({"depth": depth, "lines": [lines[k] for k in sorted(lines)]})
                if not me.cancel.is_set():
                    out.put({"depth": last_depth, "lines": [lines[k] for k in sorted(lines)], "done": True})
        except chess.engine.EngineError:
            _quit(self._stream_engine)
            self._stream_engine = None  # restart next time
        finally:
            out.put(None)

    def close(self) -> None:
        if self._current_stream is not None:
            self._current_stream.cancel.set()
        for engine in (self._judge_engine, self._stream_engine):
            _quit(engine)
        self._judge_engine = self._stream_engine = None


def _quit(engine: chess.engine.SimpleEngine | None) -> None:
    """Stop an engine process that may already be gone (an EngineError doesn't always kill it)."""
    if engine is not None:
        try:
            engine.quit()
        except Exception:
            pass


def _line_json(board: chess.Board, info: dict) -> dict:
    score = info["score"].white()
    pv = info["pv"][:12]
    return {
        "eval": {"cp": score.score(), "mate": score.mate()},
        "uci": pv[0].uci(),
        "pv_uci": [m.uci() for m in pv],
        "san_line": explain.san_line(board, pv, 12),
    }
