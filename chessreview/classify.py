"""Judge each of my moves as good / inaccuracy / mistake / blunder.

Works purely from the stored engine evals (positions table), so changing the
thresholds in config.toml only needs a cheap re-classification, never new
engine analysis.

Win chance uses the Lichess formula on a -1..1 scale:
    winning_chances(cp) = 2 / (1 + exp(-0.00368208 * cp)) - 1
A move's "drop" is my win chance before the move minus my win chance after it.

`ensure_classified()` classifies what is missing (blocking: CLI, tests). Web requests call
`refresh()` instead: new games are classified on the spot, but a full rebuild (thresholds or
version changed) runs in a background thread while requests keep reading the stored rows.
"""

import hashlib
import json
import math
import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from itertools import pairwise
from typing import Any

import chess

from . import backup, db, explain, openings, phases, report
from .config import Config

CP_CAP = 1000  # Lichess caps evals at +-10 pawns for win chances
MATE_CP = 1000  # a forced mate counts as the cap
SETTINGS_KEY = "classification_settings"
CLASSIFY_VERSION = 6  # bump when judge() logic changes so stored judgements are rebuilt
# A lost mate longer than this (moves) while still >= +7 is at most an inaccuracy: the short search
# after the move often can't see the slower mate (it is usually still there), and +7 still wins.
LONG_MATE = 5


@dataclass(frozen=True)
class Eval:
    """An evaluation from one side's point of view."""

    cp: int | None
    mate: int | None  # >0: this side mates in N; <0: this side gets mated

    @classmethod
    def from_row(cls, row: sqlite3.Row | Mapping[str, Any], color: chess.Color) -> Eval:
        """Convert a stored (White point of view) eval to `color`'s point of view."""
        sign = 1 if color == chess.WHITE else -1
        cp = row["score_cp"]
        mate = row["score_mate"]
        return cls(cp * sign if cp is not None else None, mate * sign if mate is not None else None)

    def as_cp(self) -> int:
        if self.mate is not None:
            return MATE_CP if self.mate > 0 else -MATE_CP
        return max(-CP_CAP, min(CP_CAP, self.cp or 0))

    def win_chance(self) -> float:
        return 2 / (1 + math.exp(-0.00368208 * self.as_cp())) - 1

    def __str__(self) -> str:
        if self.mate is not None:
            return f"#{self.mate}" if self.mate > 0 else f"#-{-self.mate}"
        return f"{(self.cp or 0) / 100:+.2f}"


def _mate_judgement(before: Eval, after: Eval) -> str | None:
    """Lichess' special cases for forced mates, which win chances barely register
    (missing a mate in 1 while +7 is only a 0.1 drop). Losing a long mate (LONG_MATE)
    while still clearly winning is an inaccuracy."""
    had_mate = before.mate is not None and before.mate > 0
    has_mate = after.mate is not None and after.mate > 0
    if had_mate and not has_mate and not (after.mate is not None and after.mate < 0):
        cp = after.as_cp()  # lost a forced mate but still have this much
        if cp > 999 or (cp > 700 and (before.mate or 0) > LONG_MATE):
            return "inaccuracy"
        return "mistake" if cp > 700 else "blunder"
    allowed_mate = after.mate is not None and after.mate < 0
    already_mated = before.mate is not None and before.mate < 0
    if allowed_mate and not already_mated:
        cp = before.as_cp()  # walked into a forced mate from this eval
        return "inaccuracy" if cp < -999 else "mistake" if cp < -700 else "blunder"
    return None


# Adaptive thresholds: the lower the mover's rating, the bigger a swing must be to count.
# (rating below, inaccuracy, mistake, blunder) as win-chance drops on Lichess' -1..1 scale;
# the 1600-2000 band equals Lichess' standard 0.1 / 0.2 / 0.3.
ADAPTIVE_BANDS = [
    (800, 0.16, 0.28, 0.40),
    (1200, 0.13, 0.24, 0.35),
    (1600, 0.11, 0.22, 0.32),
    (2000, 0.10, 0.20, 0.30),
    (None, 0.08, 0.17, 0.27),
]
MODE_KEY = "thresholds_mode"  # meta override set from the Settings page: 'adaptive' | 'standard'


@dataclass(frozen=True)
class Thresholds:
    inaccuracy: float
    mistake: float
    blunder: float
    band: str = "standard"

    @property
    def decided(self) -> float:
        """Win chance beyond which a game counts as decided for this level: stronger players
        are expected to win (and to lose) from smaller edges. 0.87 at 2000+, 0.95 below 1200."""
        return min(0.95, 0.6 + self.blunder)

    @property
    def min_loss(self) -> int:
        """Material (pawns) that counts as a tactic at this level: the smallest loss that, from an
        equal position, is a mistake on its own. 1 pawn at 2000+, 2 below (and with the standard
        thresholds). Explanations call smaller material swings positional."""
        for pawns in range(1, 9):
            if -Eval(-100 * pawns, None).win_chance() >= self.mistake:
                return pawns
        return 9

    @property
    def cp_factor(self) -> float:
        """Scale for the centipawn-mode thresholds, relative to Lichess' 0.1."""
        return self.inaccuracy / 0.1


def keeps_long_mate(before: Eval, after: Eval) -> bool:
    """A move in a position with a long forced mate (more than LONG_MATE moves) that keeps a
    position the search scores at the cap (+10) but without a mate: a short search after the
    move usually can't see the slower mate, so the move most likely still mates."""
    return before.mate is not None and before.mate > LONG_MATE and after.mate is None and after.as_cp() >= CP_CAP


def adaptive_enabled(conn: sqlite3.Connection, cfg: Config) -> bool:
    mode = db.get_meta(conn, MODE_KEY)
    return cfg.classification.adaptive if mode is None else mode == "adaptive"


def thresholds_for(rating: int | None, cfg: Config, adaptive: bool) -> Thresholds:
    c = cfg.classification
    if not adaptive or not rating:
        return Thresholds(c.inaccuracy, c.mistake, c.blunder)
    lower = 0
    for below, inacc, mistake, blunder in ADAPTIVE_BANDS:
        if below is None or rating < below:
            band = f"{lower}–{below - 1}" if below else f"{lower}+"
            return Thresholds(inacc, mistake, blunder, band)
        lower = below
    raise AssertionError("unreachable")


def judge(before: Eval, after: Eval, cfg: Config, th: Thresholds | None = None) -> tuple[str, float, int]:
    """Return (judgement, win-chance drop, centipawn loss) for one move."""
    c = cfg.classification
    th = th or Thresholds(c.inaccuracy, c.mistake, c.blunder)
    drop = before.win_chance() - after.win_chance()
    cp_loss = max(0, before.as_cp() - after.as_cp())
    mate_verdict = _mate_judgement(before, after)
    if mate_verdict:
        return mate_verdict, drop, cp_loss
    value: float
    limits: tuple[float, float, float]
    if c.mode == "cp":
        f = th.cp_factor
        value, limits = cp_loss, (c.cp_blunder * f, c.cp_mistake * f, c.cp_inaccuracy * f)
    else:
        value, limits = drop, (th.blunder, th.mistake, th.inaccuracy)
    for name, limit in zip(("blunder", "mistake", "inaccuracy"), limits, strict=True):
        if value >= limit:
            return name, drop, cp_loss
    return "good", drop, cp_loss


def classify_game(conn: sqlite3.Connection, game_id: int, cfg: Config, adaptive: bool | None = None) -> int:
    """(Re)build the mistakes rows for one analysed game: every move, both sides.

    My moves get explanations (they become review puzzles); opponent moves get a
    judgement, report mark and kind (used for accuracy, "miss" and the coach).
    Also stores both players' accuracy on the game row. Returns rows written.
    """
    game = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    positions = conn.execute("SELECT * FROM positions WHERE game_id = ? ORDER BY ply", (game_id,)).fetchall()
    me = chess.WHITE if game["my_color"] == "white" else chess.BLACK
    if adaptive is None:
        adaptive = adaptive_enabled(conn, cfg)
    # Each side is judged at its own level (its rating in this game).
    side_thresholds = {
        chess.WHITE: thresholds_for(game["white_elo"], cfg, adaptive),
        chess.BLACK: thresholds_for(game["black_elo"], cfg, adaptive),
    }

    rows = []
    white_pcts = [report.win_pct(Eval.from_row(p, chess.WHITE).win_chance()) for p in positions]
    accuracies = []
    previous_judgement = None
    previous_move = previous_board = None
    first_turn = chess.Board(positions[0]["fen"]).turn if positions else chess.WHITE
    for before_row, after_row in pairwise(positions):
        if before_row["move_uci"] is None:
            break
        board = chess.Board(before_row["fen"])
        mover = board.turn
        before = Eval.from_row(before_row, mover)
        after = Eval.from_row(after_row, mover)
        judgement, drop, cp_loss = judge(before, after, cfg, side_thresholds[mover])
        move = chess.Move.from_uci(before_row["move_uci"])
        forced = board.legal_moves.count() == 1
        is_best = move.uci() == before_row["best_uci"]
        if forced or is_best:
            judgement = "good"  # forced move, or exactly the engine's choice

        before_pct, after_pct = report.win_pct(before.win_chance()), report.win_pct(after.win_chance())
        accuracy = report.move_accuracy(before_pct, after_pct)
        accuracies.append(accuracy)
        board_after = board.copy(stack=False)
        board_after.push(move)
        gap = _second_gap(before_row, mover, before_pct)
        mark = report.mark_move(
            report.MarkInput(
                judgement=judgement,
                drop_pct=max(0.0, before_pct - after_pct),
                before_pct=before_pct,
                after_pct=after_pct,
                is_best=is_best,
                forced=forced,
                book=before_row["ply"] < report.BOOK_MAX_PLY and openings.is_book(board_after),
                second_gap_pct=gap,
                prev_opponent_judgement=previous_judgement,
                sacrifice=report.is_sacrifice(board, move),
                is_recapture=_is_recapture(board, move, previous_move, previous_board),
            )
        )

        th = side_thresholds[mover]
        # Hopeless before the move, or still crushing after it: little to learn from
        # (except a missed forced mate, which is always worth finding).
        had_mate = before.mate is not None and before.mate > 0
        decided = int(not had_mate and (before.win_chance() <= -th.decided or after.win_chance() >= th.decided))

        kind = text = None
        if judgement != "good":
            ctx = explain.MoveContext(
                board=board,
                move=move,
                best=chess.Move.from_uci(before_row["best_uci"]) if before_row["best_uci"] else None,
                best_line=_line(before_row["pv_uci"]),
                reply_line=_line(after_row["pv_uci"]),
                before=before,
                after=after,
                prev_capture=capture_square(previous_board, previous_move),
                min_loss=th.min_loss,
            )
            kind, text = explain.explain(ctx)
            if mover != me:
                text = None  # explanations are worded for my moves; the coach words opponent moves
        rows.append(
            (
                game_id,
                before_row["ply"],
                judgement,
                before.win_chance(),
                after.win_chance(),
                drop,
                cp_loss,
                phases.phase(board, before_row["ply"], cfg),
                kind,
                text,
                int(mover == me),
                mark,
                round(accuracy, 1),
                None if gap is None else round(gap / 50, 3),
                decided,
            )
        )
        previous_judgement = judgement
        previous_move, previous_board = move, board

    acc_white, acc_black = report.game_accuracy(
        white_pcts[: len(accuracies) + 1], accuracies, white_to_move_first=first_turn == chess.WHITE
    )
    with conn:
        conn.execute("DELETE FROM mistakes WHERE game_id = ?", (game_id,))
        conn.executemany(
            """INSERT INTO mistakes (game_id, ply, judgement, win_before, win_after, win_drop,
                   cp_loss, phase, kind, explanation, is_mine, mark, accuracy, sharpness, decided)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.execute(
            "UPDATE games SET accuracy_white = ?, accuracy_black = ? WHERE id = ?", (acc_white, acc_black, game_id)
        )
    return len(rows)


def capture_square(board: chess.Board | None, move: chess.Move | None) -> chess.Square | None:
    """Where `move` (played in `board`) captured, or None if it isn't a capture."""
    if board is None or move is None or not board.is_capture(move):
        return None
    return move.to_square


def _is_recapture(
    board: chess.Board, move: chess.Move, previous_move: chess.Move | None, previous_board: chess.Board | None
) -> bool:
    """Taking back on the square where the opponent just captured."""
    return (
        previous_move is not None
        and previous_board is not None
        and previous_board.is_capture(previous_move)
        and board.is_capture(move)
        and move.to_square == previous_move.to_square
    )


def _second_gap(row: sqlite3.Row, mover: chess.Color, best_pct: float) -> float | None:
    """How much worse (win% points, mover's view) the second-best move is than the best."""
    if row["second_cp"] is None and row["second_mate"] is None:
        return None
    second = Eval.from_row({"score_cp": row["second_cp"], "score_mate": row["second_mate"]}, mover)
    return best_pct - report.win_pct(second.win_chance())


def settings_fingerprint(cfg: Config, adaptive: bool = False) -> str:
    """Changes whenever anything that affects classification output changes."""
    data = {
        "classification": asdict(cfg.classification),
        "phases": asdict(cfg.phases),
        "adaptive": adaptive,
        "explain_version": explain.VERSION,
        "classify_version": CLASSIFY_VERSION,
    }
    return hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()


_lock = threading.Lock()  # one classification run at a time
_start_lock = threading.Lock()
REBUILD: dict[str, Any] = {"running": False, "done": 0, "total": 0, "error": None}  # the background rebuild's progress


def ensure_classified(conn: sqlite3.Connection, cfg: Config, force: bool = False) -> int:
    """Classify analysed games that have no classification yet, or all of them if the
    settings changed since the last run. Returns the number of games classified."""
    with _lock:
        return _classify(conn, cfg, force)


def needs_rebuild(conn: sqlite3.Connection, cfg: Config) -> bool:
    return db.get_meta(conn, SETTINGS_KEY) != settings_fingerprint(cfg, adaptive_enabled(conn, cfg))


def refresh(conn: sqlite3.Connection, cfg: Config) -> None:
    """For web requests (F17): classify newly analysed games now, which is quick, but leave a
    full rebuild of stored rows to `start_rebuild()`. Meanwhile requests read the rows as they
    are: each game is replaced in one transaction, so no game is ever half-classified."""
    if REBUILD["running"]:
        return  # the rebuild classifies new games too
    if needs_rebuild(conn, cfg) and conn.execute("SELECT 1 FROM mistakes LIMIT 1").fetchone():
        start_rebuild(cfg)
        return
    with _lock:
        _classify(conn, cfg, False)


def start_rebuild(cfg: Config) -> bool:
    """Reclassify every game in a background thread with its own connection. Progress is in
    REBUILD. Returns False if a rebuild is already running."""
    with _start_lock:
        if REBUILD["running"]:
            return False
        REBUILD.update(running=True, done=0, total=0, error=None)

    def run() -> None:
        conn = db.connect(cfg.db_path)
        try:
            with _lock:
                _classify(conn, cfg, False, progress=lambda done, total: REBUILD.update(done=done, total=total))
        except Exception as exc:  # shown in Settings; the next request tries again
            REBUILD["error"] = str(exc)
        finally:
            conn.close()
            REBUILD["running"] = False

    threading.Thread(target=run, daemon=True, name="reclassify").start()
    return True


def _classify(
    conn: sqlite3.Connection, cfg: Config, force: bool, progress: Callable[[int, int], None] | None = None
) -> int:
    adaptive = adaptive_enabled(conn, cfg)
    fingerprint = settings_fingerprint(cfg, adaptive)
    rebuild_all = force or db.get_meta(conn, SETTINGS_KEY) != fingerprint
    if rebuild_all:
        if conn.execute("SELECT 1 FROM mistakes LIMIT 1").fetchone():
            backup.make(conn, "before-reclassify")  # every judgement is about to be rewritten
        sql = "SELECT id FROM games WHERE analysed_at IS NOT NULL"
    else:
        sql = """SELECT id FROM games g WHERE analysed_at IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM mistakes m WHERE m.game_id = g.id)"""
    game_ids = [r["id"] for r in conn.execute(sql)]
    for done, game_id in enumerate(game_ids):
        if progress:
            progress(done, len(game_ids))
        classify_game(conn, game_id, cfg, adaptive)
    with conn:
        db.set_meta(conn, SETTINGS_KEY, fingerprint)
    if progress:
        progress(len(game_ids), len(game_ids))
    return len(game_ids)


def _line(pv_uci: str | None) -> list[chess.Move]:
    return [chess.Move.from_uci(u) for u in (pv_uci or "").split()]
