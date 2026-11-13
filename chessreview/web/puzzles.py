"""Puzzle items built from stored analysis: my mistakes, library puzzles and calm positions."""

import sqlite3

import chess

from .. import level, library
from ..classify import Eval, Thresholds, capture_square
from ..config import Config
from ..explain import ATTACKING_KINDS, MoveContext, explain_best, hint, san_line
from ..live import PositionAnalysis
from ..threats import Threat, training_threat
from .common import config

# Latest review result for a mistake row `m` (NULL if never attempted).

STATUS_SQL = """(SELECT r.result FROM review_results r
                 WHERE r.game_id = m.game_id AND r.ply = m.ply ORDER BY r.id DESC LIMIT 1)"""


def query_mistakes(c: sqlite3.Connection, cfg: Config, where: list[str], params: list, order: str) -> list[dict]:
    """My mistakes as puzzle items (shared by Practice, Train and the new-games session)."""
    rows = c.execute(
        f"""
        SELECT m.*, p.fen, p.move_uci, p.move_san, p.best_uci, p.pv_uci,
               p.score_cp AS before_cp, p.score_mate AS before_mate,
               p.score_cp, p.score_mate, p.threat_cp, p.threat_mate, p.threat_uci, p.threat_pv,
               p2.score_cp AS after_cp, p2.score_mate AS after_mate, p2.pv_uci AS after_pv,
               p0.move_uci AS prev_move_uci, p0.fen AS prev_fen,
               g.my_color, g.white, g.black, g.date, g.opening, g.opening_family,
               g.my_result, g.time_class, {STATUS_SQL} AS status,
               c.box AS card_box, c.due_on AS card_due
        FROM mistakes m
        JOIN positions p ON p.game_id = m.game_id AND p.ply = m.ply
        JOIN positions p2 ON p2.game_id = m.game_id AND p2.ply = m.ply + 1
        LEFT JOIN positions p0 ON p0.game_id = m.game_id AND p0.ply = m.ply - 1
        LEFT JOIN cards c ON c.game_id = m.game_id AND c.ply = m.ply
        JOIN games g ON g.id = m.game_id
        WHERE {" AND ".join(where)}
        ORDER BY {order}
    """,
        params,
    ).fetchall()
    th = level.current_thresholds(c, cfg)
    return [mistake_json(r, cfg, th) for r in rows]


def last_capture_square(prev_fen: str | None, prev_uci: str | None) -> chess.Square | None:
    """Where the opponent's last move (from the stored position before it) captured, if it did."""
    if not prev_fen or not prev_uci:
        return None
    return capture_square(chess.Board(prev_fen), chess.Move.from_uci(prev_uci))


def eval_white(cp: int | None, mate: int | None) -> dict:
    return {"cp": cp, "mate": mate}


def playout_moves(kind: str | None, before: Eval, cfg: Config) -> int:
    """How many of my moves a review puzzle asks for."""
    if kind == "missed_mate" and before.mate and before.mate > 0:
        return min(before.mate, 5)
    if kind in ATTACKING_KINDS:
        return cfg.review.playout_tactical
    return cfg.review.playout_other


def mistake_json(r: sqlite3.Row, cfg: Config, th: Thresholds) -> dict:
    me = chess.WHITE if r["my_color"] == "white" else chess.BLACK
    board = chess.Board(r["fen"])
    best = chess.Move.from_uci(r["best_uci"]) if r["best_uci"] else None
    line = [chess.Move.from_uci(u) for u in (r["pv_uci"] or "").split()]
    before = Eval.from_row({"score_cp": r["before_cp"], "score_mate": r["before_mate"]}, me)
    after = Eval.from_row({"score_cp": r["after_cp"], "score_mate": r["after_mate"]}, me)
    ply = r["ply"]
    reply = [chess.Move.from_uci(u) for u in (r["after_pv"] or "").split()]
    good = r["judgement"] == "good"  # a calm position: I played well in the game
    best_explanation = (
        explain_best(
            MoveContext(
                board=board,
                move=chess.Move.from_uci(r["move_uci"]),
                best=best,
                best_line=line,
                reply_line=reply,
                before=before,
                after=after,
                prev_capture=last_capture_square(r["prev_fen"], r["prev_move_uci"]),
                min_loss=th.min_loss,
            ),
            r["kind"],
        )
        if best and not good
        else ""
    )
    threat = training_threat(r, me, th)
    return {
        "game_id": r["game_id"],
        "ply": ply,
        "move_label": f"{ply // 2 + 1}{'.' if ply % 2 == 0 else '...'}{r['move_san']}",
        "judgement": r["judgement"],
        "kind": r["kind"],
        "explanation": r["explanation"],
        "phase": r["phase"],
        "win_drop": r["win_drop"],
        "fen": r["fen"],
        "move_uci": r["move_uci"],
        "move_san": r["move_san"],
        "best_uci": r["best_uci"],
        "best_san": board.san(best) if best else None,
        "best_line": san_line(board, line, 6),
        "best_explanation": best_explanation,
        "best_pv_uci": [m.uci() for m in line],
        "eval_before": str(before),
        "eval_after": str(after),
        "eval_before_white": eval_white(r["before_cp"], r["before_mate"]),
        "my_color": r["my_color"],
        "opponent": r["black"] if r["my_color"] == "white" else r["white"],
        "date": r["date"],
        "opening": r["opening"],
        "opening_family": r["opening_family"],
        "my_result": r["my_result"],
        "time_class": r["time_class"],
        "prev_move_uci": r["prev_move_uci"],
        "status": r["status"],
        "hint": calm_hint(threat) if good else hint(r["kind"]),
        "playout_moves": playout_moves(r["kind"], before, cfg),
        "card_box": r["card_box"],
        "card_due": r["card_due"],
        "mark": r["mark"],
        "threat": threat.as_dict(r["fen"]) if threat else None,
    }


def calm_hint(threat: Threat | None) -> str:
    """Hint for a position I handled well in the game (no mistake kind to hint at)."""
    if threat is not None and threat.real:
        return "Your opponent is threatening something. Find it and deal with it first."
    return "No tricks here: look for a solid, useful move that keeps everything protected."


def items_for(c: sqlite3.Connection, keyed: list[tuple[tuple, str]]) -> list[dict]:
    """Puzzle items for ((game_id, ply) | ("lib", puzzle_id), tag) pairs, in the given order,
    tagged due/new/calm/focus."""
    cfg = config()
    mine = [k for k, _ in keyed if k[0] != "lib"]
    by_key: dict = {}
    if mine:
        pairs = " OR ".join("(m.game_id = ? AND m.ply = ?)" for _ in mine)
        flat = [v for k in mine for v in k]
        by_key = {
            (i["game_id"], i["ply"]): i
            for i in query_mistakes(c, cfg, ["m.is_mine = 1", f"({pairs})"], flat, "m.game_id")
        }
    lib_ids = [k[1] for k, _ in keyed if k[0] == "lib"]
    if lib_ids:
        rows = c.execute(f"SELECT * FROM puzzles WHERE id IN ({','.join('?' * len(lib_ids))})", lib_ids)
        by_key |= {("lib", r["id"]): library.puzzle_json(r) for r in rows}
    out = []
    for k, tag in keyed:
        if k in by_key:
            item = by_key[k]
            out.append(
                {
                    **item,
                    "queue": tag,
                    "calm": item.get("mode") != "library" and (tag == "calm" or item["judgement"] == "good"),
                }
            )
    return out


def stored_analysis(pos: sqlite3.Row) -> PositionAnalysis:
    pv = tuple(chess.Move.from_uci(u) for u in (pos["pv_uci"] or "").split())
    best = chess.Move.from_uci(pos["best_uci"]) if pos["best_uci"] else None
    return PositionAnalysis(Eval(pos["score_cp"], pos["score_mate"]), best, pv)
