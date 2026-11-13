"""v3 milestone 3: converting won positions and essential endgames (convert.py)."""

import chess
import pytest

from chessreview import convert, db, srs
from chessreview.classify import Eval
from chessreview.convert import Task, state


def task(
    goal="win",
    finish="resign",
    kind="convert",
    color="white",
    max_plies=120,
    fen=chess.STARTING_FEN,
    resign_cp=convert.RESIGN_CP,
    start_eval="+5.00",
):
    return Task(
        key="x/1",
        kind=kind,
        fen=fen,
        color=color,
        goal=goal,
        finish=finish,
        title="t",
        max_plies=max_plies,
        start_eval=start_eval,
        resign_cp=resign_cp,
    )


def played(fen, *ucis):
    board = chess.Board(fen)
    for uci in ucis:
        board.push_uci(uci)
    return board


def test_endgames_are_valid_and_my_side_moves_first():
    games = convert.endgames()
    assert len({t.key for t in games}) == len(games) >= 12
    for t in games:
        board = chess.Board(t.fen)
        assert board.is_valid(), t.key
        assert t.goal in ("win", "draw") and t.finish in convert.FINISHES
        assert (t.goal == "draw") == (t.finish == "hold"), t.key  # every win names its milestone (P11)
        assert t.finish != "resign", t.key  # resigning on the eval is for converts
        assert (board.turn == chess.WHITE) == (t.color == "white")
        assert t.level in convert.LEVELS


def test_levels_unlock_in_order(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    overview = convert.endgame_overview(conn)
    assert {e["level"] for e in overview if e["unlocked"]} == {1}
    for e in overview:
        if e["level"] == 1:
            srs.record_key(conn, "endgame", e["key"].split("/")[1], "solved")
    assert {e["level"] for e in convert.endgame_overview(conn) if e["unlocked"]} == {1, 2}


def test_checkmate_and_draws():
    mated = chess.Board("R5k1/5ppp/8/8/8/8/5PPP/6K1 b - - 1 1")  # black is mated, I am white
    assert state(task(), mated, None, 0, 1000).status == "won"
    stalemate = chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
    assert state(task(), stalemate, None, 0, 1000).status == "failed"
    assert state(task(goal="draw"), stalemate, None, 0, 1000).status == "won"


def test_stockfish_resigns_at_a_short_mate_except_in_the_basic_mates():
    b = chess.Board()
    assert state(task(), b, Eval(None, 4), 0, 1000).status == "won"  # mate in 4
    assert state(task(finish="promote", kind="endgame"), b, Eval(None, 5), 0, 1000).status == "won"
    assert state(task(finish="bare_king", kind="endgame"), b, Eval(None, 6), 0, 1000).status == "playing"
    assert state(task(finish="mate", kind="endgame"), b, Eval(None, 4), 0, 1000).status == "playing"


def test_resigning_is_relative_to_the_start():
    """P11: a convert that starts at +8.45 isn't won by three moves that keep +9."""
    assert convert.resign_cp(Eval(845, None)) == 1145
    assert convert.resign_cp(Eval(500, None)) == 800
    assert convert.resign_cp(Eval(200, None)) == convert.RESIGN_CP  # never below +7
    assert convert.resign_cp(Eval(None, 7)) is None  # a forced mate: finish it
    t = task(resign_cp=1145, start_eval="+8.45")
    b = chess.Board()
    s = state(t, b, Eval(900, None), 2, 1000)
    assert s.status == "playing" and s.streak == 0
    s = state(t, b, Eval(1200, None), 0, 1000)
    assert s.status == "playing" and s.streak == 1
    s = state(t, b, Eval(1200, None), 2, 1000)
    assert s.status == "won" and "+8.45" in s.reason
    mate_start = task(resign_cp=None, start_eval="#7")
    assert state(mate_start, b, Eval(None, 8), 2, 1000).status == "playing"
    assert state(mate_start, b, Eval(5000, None), 2, 1000).streak == 0


PAWN_E7 = "8/4P1k1/8/8/8/8/8/4K3 w - - 0 1"


def test_promotion_finishes_only_when_still_decisive():
    promote = task(finish="promote", kind="endgame", fen=PAWN_E7)
    b = played(PAWN_E7, "e7e8q", "g7f6")
    assert state(promote, b, Eval(None, 9), 0, 1000).reason == "You promoted safely: Stockfish resigns."
    assert state(promote, b, Eval(40, None), 0, 1000).status == "playing"  # e.g. the queen was lost
    # A high eval without the promotion is not the milestone.
    assert state(promote, played(PAWN_E7, "e1d2", "g7f6"), Eval(2000, None), 2, 1000).status == "playing"
    # Stockfish's promotion is not mine.
    fen = "8/8/8/8/8/k7/1p6/4K2R w - - 0 1"
    b = played(fen, "h1h2", "b2b1q")
    assert state(task(finish="promote", kind="endgame", fen=fen), b, Eval(800, None), 0, 1000).status == "playing"


def test_bare_king_finishes_only_if_stockfish_had_more():
    fen = "K7/8/8/8/8/8/1p5Q/3k4 w - - 0 1"
    b = played(fen, "h2b2", "d1e1")
    bare = task(finish="bare_king", kind="endgame", fen=fen)
    assert state(bare, b, Eval(None, 9), 0, 1000).reason == "Only Stockfish's king is left: it resigns."
    assert state(bare, played(fen, "h2h3", "d1e1"), Eval(None, 9), 0, 1000).status == "playing"
    cutoff = "8/8/8/8/4k3/8/R3P3/4K3 w - - 0 1"  # a bare king from the start
    b = played(cutoff, "a2a3", "e4d4")
    assert state(task(finish="bare_king", kind="endgame", fen=cutoff), b, Eval(1500, None), 2, 1000).status == "playing"
    # Converts count both milestones.
    assert state(task(fen=fen, resign_cp=1500), played(fen, "h2b2", "d1e1"), Eval(None, 9), 0, 1000).status == "won"


def test_finish_text_names_the_milestone():
    assert "+11.45" in convert.finish_text(task(resign_cp=1145))
    assert "+" not in convert.finish_text(task(resign_cp=None))
    texts = {t.key: t.as_dict()["finish_text"] for t in convert.endgames()}
    assert "promote" in texts["endgame/kp_front"] and "only its king" in texts["endgame/q_vs_r"]
    assert texts["endgame/kq_k"] == "Done at checkmate." and "15" in texts["endgame/vancura"]


def test_slipping_advantage_scales_with_rating():
    """+1.8 counts as having let it slip at 900 (bar +5), not yet at 2000 (bar +3)."""
    b = chess.Board()
    assert state(task(), b, Eval(180, None), 0, 900).status == "failed"
    assert state(task(), b, Eval(180, None), 0, 2000).status == "playing"
    # Endgames are judged by the result only (the eval of a B+N mate can look modest).
    assert state(task(kind="endgame", finish="mate"), b, Eval(180, None), 0, 900).status == "playing"


def test_draw_goal_holds_or_fails():
    b = chess.Board()
    for _ in range(2 * convert.HOLD_MOVES):
        b.push(next(iter(b.legal_moves)))
    assert state(task(goal="draw"), b, Eval(0, None), 0, 1000).status == "won"
    assert state(task(goal="draw"), chess.Board(), Eval(-400, None), 0, 1000).status == "failed"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "c.db")
    for gid, result, wins in (
        (1, "loss", [0.1, 0.75, 0.8, 0.2]),
        (2, "win", [0.75, 0.8, 0.9, 0.9]),
        (3, "draw", [0.1, 0.75, 0.3, 0.75]),
    ):
        c.execute(
            """INSERT INTO games (id, dedupe_key, white, black, white_elo, black_elo, my_color, my_result, date,
                         moves_uci, num_plies, pgn, analysed_at, end_time, time_class, rated)
                     VALUES (?, ?, 'me', 'opp', 1000, 1000, 'white', ?, '2026-10-01', 'e2e4', 8, '', '2026-10-01',
                             ?, 'rapid', 1)""",
            (gid, f"k{gid}", result, f"2026-10-0{gid} 12:00"),
        )
        for i, w in enumerate(wins):
            c.execute(
                """INSERT INTO mistakes (game_id, ply, judgement, win_before, win_drop, is_mine)
                         VALUES (?, ?, 'good', ?, 0, 1)""",
                (gid, 2 * i, w),
            )
            c.execute(
                "INSERT INTO positions (game_id, ply, fen, score_cp) VALUES (?, ?, ?, 600)",
                (gid, 2 * i, chess.STARTING_FEN),
            )
    c.commit()
    yield c
    c.close()


def test_convert_positions(conn):
    keys = [p["key"] for p in convert.convert_positions(conn)]
    # winning bar at 1000: +5 = 0.726 win chance; game 3 never holds it for two moves
    assert keys == ["convert/1:2", "convert/2:0"]  # the lost game first
    assert convert.next_convert(conn) == "convert/1:2"
    srs.record_key(conn, "convert", "1:2", "failed")
    assert convert.next_convert(conn) == "convert/2:0"  # tried today: due again tomorrow
    t = convert.task_for(conn, "convert/1:2")
    assert t.goal == "win" and t.color == "white" and "lost" in t.subtitle
    assert t.start_cp is not None and t.start_cp > 0  # for "where it slipped" after a failed try
    assert t.resign_cp == 900  # +6 at the start: Stockfish resigns at +9


def test_migration_resets_endgames_solved_under_the_old_rule(conn):
    """kp_front and square were "solved" by three safe moves; the mates keep their cards."""
    for key in ("kp_front", "square", "kq_k"):
        srs.record_key(conn, "endgame", key, "solved")
    conn.execute("PRAGMA user_version = 8")
    conn.commit()
    db.migrate(conn)
    assert [r["key"] for r in conn.execute("SELECT key FROM cards WHERE kind = 'endgame'")] == ["kq_k"]
    assert conn.execute("SELECT COUNT(*) FROM training_log WHERE kind = 'endgame'").fetchone()[0] == 3
    assert {e["key"]: e["status"] for e in convert.endgame_overview(conn)}["endgame/kp_front"] == "new"
