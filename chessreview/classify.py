"""Judge each of my moves as good / inaccuracy / mistake / blunder.

Works purely from the stored engine evals (positions table), so changing the
thresholds in config.toml only needs a cheap re-classification, never new
engine analysis.

Win chance uses the Lichess formula on a -1..1 scale:
    winning_chances(cp) = 2 / (1 + exp(-0.00368208 * cp)) - 1
A move's "drop" is my win chance before the move minus my win chance after it.
"""

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass

import chess

from . import db, explain, phases
from .config import Config

CP_CAP = 1000          # Lichess caps evals at +-10 pawns for win chances
MATE_CP = 1000         # a forced mate counts as the cap
SETTINGS_KEY = "classification_settings"

JUDGEMENTS = ("good", "inaccuracy", "mistake", "blunder")


@dataclass(frozen=True)
class Eval:
    """An evaluation from one side's point of view."""
    cp: int | None
    mate: int | None   # >0: this side mates in N; <0: this side gets mated

    @classmethod
    def from_row(cls, row: sqlite3.Row, color: chess.Color) -> "Eval":
        """Convert a stored (White point of view) eval to `color`'s point of view."""
        sign = 1 if color == chess.WHITE else -1
        cp = row["score_cp"]
        mate = row["score_mate"]
        return cls(cp * sign if cp is not None else None,
                   mate * sign if mate is not None else None)

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


def judge(before: Eval, after: Eval, cfg: Config) -> tuple[str, float, int]:
    """Return (judgement, win-chance drop, centipawn loss) for one move."""
    c = cfg.classification
    drop = before.win_chance() - after.win_chance()
    cp_loss = max(0, before.as_cp() - after.as_cp())
    if c.mode == "cp":
        value, limits = cp_loss, (c.cp_blunder, c.cp_mistake, c.cp_inaccuracy)
    else:
        value, limits = drop, (c.blunder, c.mistake, c.inaccuracy)
    for name, limit in zip(("blunder", "mistake", "inaccuracy"), limits):
        if value >= limit:
            return name, drop, cp_loss
    return "good", drop, cp_loss


def classify_game(conn: sqlite3.Connection, game_id: int, cfg: Config) -> int:
    """(Re)build the mistakes rows for one analysed game. Returns rows written."""
    game = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    positions = conn.execute(
        "SELECT * FROM positions WHERE game_id = ? ORDER BY ply", (game_id,)).fetchall()
    me = chess.WHITE if game["my_color"] == "white" else chess.BLACK

    rows = []
    for before_row, after_row in zip(positions, positions[1:]):
        board = chess.Board(before_row["fen"])
        if board.turn != me or before_row["move_uci"] is None:
            continue
        before = Eval.from_row(before_row, me)
        after = Eval.from_row(after_row, me)
        judgement, drop, cp_loss = judge(before, after, cfg)
        move = chess.Move.from_uci(before_row["move_uci"])
        if board.legal_moves.count() == 1 or move.uci() == before_row["best_uci"]:
            judgement = "good"  # forced move, or exactly the engine's choice

        kind = text = None
        if judgement != "good":
            ctx = explain.MoveContext(
                board=board, move=move,
                best=chess.Move.from_uci(before_row["best_uci"]) if before_row["best_uci"] else None,
                best_line=_line(before_row["pv_uci"]),
                reply_line=_line(after_row["pv_uci"]),
                before=before, after=after,
            )
            kind, text = explain.explain(ctx)

        rows.append((game_id, before_row["ply"], judgement, before.win_chance(), after.win_chance(),
                     drop, cp_loss, phases.phase(board, before_row["ply"], cfg), kind, text))

    with conn:
        conn.execute("DELETE FROM mistakes WHERE game_id = ?", (game_id,))
        conn.executemany(
            """INSERT INTO mistakes (game_id, ply, judgement, win_before, win_after, win_drop,
                   cp_loss, phase, kind, explanation) VALUES (?,?,?,?,?,?,?,?,?,?)""", rows)
    return len(rows)


def settings_fingerprint(cfg: Config) -> str:
    """Changes whenever anything that affects classification output changes."""
    data = {"classification": asdict(cfg.classification), "phases": asdict(cfg.phases),
            "explain_version": explain.VERSION}
    return hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()


def ensure_classified(conn: sqlite3.Connection, cfg: Config, force: bool = False) -> int:
    """Classify analysed games that have no classification yet, or all of them if the
    settings changed since the last run. Returns the number of games classified."""
    fingerprint = settings_fingerprint(cfg)
    rebuild_all = force or db.get_meta(conn, SETTINGS_KEY) != fingerprint
    if rebuild_all:
        sql = "SELECT id FROM games WHERE analysed_at IS NOT NULL"
    else:
        sql = """SELECT id FROM games g WHERE analysed_at IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM mistakes m WHERE m.game_id = g.id)"""
    game_ids = [r["id"] for r in conn.execute(sql)]
    for game_id in game_ids:
        classify_game(conn, game_id, cfg)
    with conn:
        db.set_meta(conn, SETTINGS_KEY, fingerprint)
    return len(game_ids)


def _line(pv_uci: str | None) -> list[chess.Move]:
    return [chess.Move.from_uci(u) for u in (pv_uci or "").split()]
