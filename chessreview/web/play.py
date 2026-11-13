"""Playing positions out against Stockfish: Convert, Spar and the calculation trainer."""

import sqlite3
from typing import Any

import chess
from flask import Blueprint, jsonify, request
from flask.typing import ResponseReturnValue
from werkzeug.exceptions import BadRequest

from .. import calc, coach, convert, level, spar, srs, tablebase
from ..classify import judge, refresh
from .common import (
    REVIEW_RESULTS,
    board_from_fen,
    color_field,
    config,
    conn,
    int_field,
    json_body,
    live_engine,
    moves_field,
)

bp = Blueprint("play", __name__)

REVIEW_CHUNK = 12  # plies judged per /api/spar/review request


@bp.get("/api/convert")
def convert_overview() -> ResponseReturnValue:
    cfg = config()
    c = conn()
    refresh(c, cfg)
    rating = level.current_rating(c)
    return jsonify(
        positions=convert.convert_overview(c),
        endgames=convert.endgame_overview(c),
        next=convert.next_convert(c),
        rating=rating,
        winning_cp=level.winning_cp(rating),
        levels=convert.LEVELS,
        analysed=coach.analysed_games(c),
    )


@bp.get("/api/convert/task")
def convert_task() -> ResponseReturnValue:
    task = convert.task_for(conn(), request.args.get("key", ""))
    if task is None:
        return jsonify(error="unknown position"), 404
    return jsonify(task.as_dict())


def _replay(task: convert.Task, moves: list[str]) -> chess.Board:
    """The playout so far; raises ValueError on an illegal move."""
    board = chess.Board(task.fen)
    for uci in moves:
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise ValueError(f"illegal move {uci}")
        board.push(move)
    return board


@bp.post("/api/play/move")
def play_move() -> ResponseReturnValue:
    """My move in a playout; Stockfish replies. Body: {key, moves (so far), uci, streak}."""
    live = live_engine()
    data = json_body()
    streak = int_field(data, "streak", 0)
    c = conn()
    task = convert.task_for(c, str(data.get("key", "")))
    if task is None:
        return jsonify(error="unknown position"), 404
    try:
        board = _replay(task, moves_field(data))
        move = chess.Move.from_uci(data.get("uci", ""))
        if move not in board.legal_moves:
            raise ValueError(f"illegal move {data.get('uci')}")
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    me = chess.WHITE if task.color == "white" else chess.BLACK
    rating = level.current_rating(c)
    out: dict[str, Any] = {"san": board.san(move), "uci": move.uci()}
    before = tablebase.probe(c, board)  # the position I moved in (E3: blame a move only if known)
    board.push(move)
    st = convert.state(task, board, None, streak, rating)
    if st.status == "playing":
        analysis = live.analyse(board)  # Stockfish's eval of the position after its best reply
        # With 7 pieces or fewer the tablebase defends perfectly and judges my move exactly (E3).
        tb = tablebase.probe(c, board)
        best = tablebase.choose(tb, analysis.best) if tb else None
        reply = chess.Move.from_uci(best.uci) if best else analysis.best
        assert reply is not None  # the game isn't over, so there is a move
        out |= {
            "reply_uci": reply.uci(),
            "reply_san": board.san(reply),
            "reply_source": "tablebase" if best else "stockfish",
        }
        board.push(reply)
        ev = analysis.for_side(me)
        out |= {"eval": str(ev), "cp": ev.as_cp()}
        exact = (_start_wdl(c, task), before.wdl if before else None, -tb.wdl) if tb else None
        if exact:  # my side, before and after my move (the Convert lesson finds the slip by it)
            out["tablebase"] = {
                "wdl": exact[2],
                "verdict": tablebase.verdict(exact[2]),
                "before_wdl": exact[1],
                "before_verdict": tablebase.verdict(exact[1]) if exact[1] is not None else None,
            }
        st = convert.state(task, board, ev, st.streak, rating, tb=exact)
    out |= {"fen": board.fen(), "status": st.status, "reason": st.reason, "streak": st.streak}
    return jsonify(out)


@bp.post("/api/play/hint")
def play_hint() -> ResponseReturnValue:
    live = live_engine()
    data = json_body()
    c = conn()
    task = convert.task_for(c, str(data.get("key", "")))
    if task is None:
        return jsonify(error="unknown position"), 404
    try:
        board = _replay(task, moves_field(data))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    tb = tablebase.probe(c, board)
    if tb and tb.best:  # perfect play (E3); in a draw, the engine's drawing move
        move = tablebase.choose(tb, live.analyse(board).best if tb.best.wdl == 0 else None)
        if move is not None:
            return jsonify(uci=move.uci, san=move.san, source="tablebase")
    best = live.analyse(board).best
    return jsonify(uci=best.uci() if best else None, san=board.san(best) if best else None, source="stockfish")


def _start_wdl(c: sqlite3.Connection, task: convert.Task) -> int | None:
    """The task's start from my side in the tablebase (None: more than 7 pieces, or unknown)."""
    tb = tablebase.probe(c, chess.Board(task.fen))
    return tb.wdl if tb else None


@bp.post("/api/convert/result")
def convert_result() -> ResponseReturnValue:
    data = json_body()
    task = convert.task_for(conn(), str(data.get("key", "")))
    if task is None or data.get("result") not in REVIEW_RESULTS:
        return jsonify(error="unknown position or result"), 400
    srs.record_key(conn(), task.kind, task.card_key, data["result"])
    return jsonify(ok=True)


@bp.get("/api/calc")
def calc_overview() -> ResponseReturnValue:
    return jsonify(calc.overview(conn()))


@bp.get("/api/calc/next")
def calc_next() -> ResponseReturnValue:
    """The next question (a due card first), not one of ?exclude=key,key (this session's)."""
    exclude = {k for k in request.args.get("exclude", "").split(",") if k}
    q, due = calc.next_question(conn(), exclude)
    return jsonify(question=q.public() if q else None, due=due)


@bp.post("/api/calc/answer")
def calc_answer() -> ResponseReturnValue:
    data = json_body()
    if not isinstance(data.get("key"), str) or not isinstance(data.get("answer"), str):
        return jsonify(error="key and answer are required"), 400
    try:
        result = calc.answer(conn(), data["key"], data["answer"])
    except calc.Unreadable as exc:  # a typo isn't a miss: nothing recorded
        return jsonify(error=str(exc)), 400
    if result is None:
        return jsonify(error="no such question"), 404
    return jsonify(result)


@bp.get("/api/spar")
def spar_overview() -> ResponseReturnValue:
    cfg = config()
    c = conn()
    refresh(c, cfg)
    return jsonify(
        positions=spar.positions(c),
        levels=spar.levels(c),
        offset=spar.saved_offset(c),
        rating=level.current_rating(c),
        history=spar.history(c),
    )


@bp.post("/api/spar/offset")
def spar_offset() -> ResponseReturnValue:
    c = conn()
    spar.save_offset(c, int_field(json_body(), "offset"))
    return jsonify(offset=spar.saved_offset(c))


def _spar_game(data: dict) -> tuple[chess.Board, chess.Color, list[str]]:
    start = board_from_fen(data.get("fen"))
    if not start.is_valid():  # e.g. no kings, pawns on the back rank: the engine can't play it
        raise BadRequest("that position isn't legal")
    moves = moves_field(data)
    try:
        board = spar.replay(start.fen(), moves)
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    return board, chess.WHITE if color_field(data.get("color")) == "white" else chess.BLACK, moves


@bp.post("/api/spar/move")
def spar_move() -> ResponseReturnValue:
    """Body: {fen (start), color (mine), moves (so far), uci (mine; omitted when Stockfish is to
    move, e.g. I play Black from the start), offset}. Stockfish replies at the chosen strength."""
    live = live_engine()
    data = json_body()
    board, me, _ = _spar_game(data)
    c = conn()
    st = spar.strength(spar.target_elo(level.current_rating(c), int_field(data, "offset", 0)))
    out: dict = {}
    if data.get("uci") is not None:
        if board.turn != me:
            return jsonify(error="it isn't your move"), 400
        try:
            move = chess.Move.from_uci(str(data["uci"]))
        except ValueError:
            move = None
        if move is None or move not in board.legal_moves:
            return jsonify(error=f"illegal move {data['uci']}"), 400
        out |= {"uci": move.uci(), "san": board.san(move)}
        board.push(move)
    elif board.turn == me:
        return jsonify(error="your move"), 400
    over = spar.outcome(board, me)
    if over is None:
        reply = live.play(board, st.options, st.limit, st.swap, st.swap_kind)
        out |= {"reply_uci": reply.uci(), "reply_san": board.san(reply)}
        board.push(reply)
        over = spar.outcome(board, me)
    out |= {
        "fen": board.fen(),
        "status": over[0] if over else "playing",
        "reason": over[1] if over else "",
        "elo": st.elo,
    }
    return jsonify(out)


@bp.post("/api/spar/finish")
def spar_finish() -> ResponseReturnValue:
    """Store a finished game: {source, fen, color, moves, offset, resigned}. The result comes from
    the position (a game not over yet can only be resigned: a loss)."""
    data = json_body()
    board, me, moves = _spar_game(data)
    over = spar.outcome(board, me)
    start = chess.Board(data["fen"])
    if not any(i % 2 == (0 if start.turn == me else 1) for i in range(len(moves))):
        return jsonify(error="you haven't played a move: nothing to record"), 400  # leaving isn't losing
    if over is None:
        if not data.get("resigned"):
            return jsonify(error="the game isn't over"), 400
        over = ("loss", "You resigned.")
    c = conn()
    offset = spar.clamp_offset(int_field(data, "offset", 0))
    elo = spar.strength(spar.target_elo(level.current_rating(c), offset)).elo
    source = str(data.get("source") or "fen")[:40]
    game_id = spar.record(
        c,
        source,
        chess.Board(data["fen"]).fen(),
        "white" if me == chess.WHITE else "black",
        elo,
        offset,
        moves,
        over[0],
        over[1],
    )
    return jsonify(id=game_id, result=over[0], reason=over[1], history=spar.history(c))


@bp.post("/api/spar/review")
def spar_review() -> ResponseReturnValue:
    """Judge my moves of a stored game at full strength: {id, from} -> REVIEW_CHUNK plies from
    `from` ({ply, san, uci, mine, judgement, before, after, best_san}), and `next` (or None)."""
    cfg = config()
    live = live_engine()
    data = json_body()
    c = conn()
    g = spar.game(c, int_field(data, "id"))
    if g is None:
        return jsonify(error="no such game"), 404
    moves = g["moves"].split()
    start = max(0, int_field(data, "from", 0))
    me = chess.WHITE if g["color"] == "white" else chess.BLACK
    th = level.current_thresholds(c, cfg)
    board = spar.replay(g["start_fen"], moves[:start])
    out = []
    for ply in range(start, min(len(moves), start + REVIEW_CHUNK)):
        move = chess.Move.from_uci(moves[ply])
        entry = {"ply": ply, "uci": move.uci(), "san": board.san(move), "mine": board.turn == me}
        if entry["mine"]:
            before = live.analyse(board)
            after_board = board.copy()
            after_board.push(move)
            after = live.analyse(after_board)
            verdict, _, _ = judge(before.for_side(me), after.for_side(me), cfg, th)
            is_best = before.best == move
            entry |= {
                "judgement": "good" if is_best else verdict,
                "before": str(before.for_side(me)),
                "after": str(after.for_side(me)),
                "best_san": board.san(before.best) if before.best and not is_best else None,
            }
        out.append(entry)
        board.push(move)
    nxt = start + REVIEW_CHUNK
    return jsonify(moves=out, next=nxt if nxt < len(moves) else None, total=len(moves))
