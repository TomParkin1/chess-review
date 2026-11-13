"""v3 real-game training: threats (null-move search), level scaling, puzzle quality, deck."""

from dataclasses import replace
from datetime import date

import chess
import pytest

from chessreview import db, level, srs
from chessreview.classify import Eval, thresholds_for
from chessreview.config import load_config
from chessreview.engine import analyse_position, analyse_threat, my_move_count, open_engine, threat_board
from chessreview.threats import threat_of

DAY = date(2026, 10, 16)


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def engine_cfg(tmp_path, stockfish):
    base = load_config()
    return replace(base, engine=replace(base.engine, time_per_position=0.1, threads=1), db_path=tmp_path / "e.db")


# ------------------------------------------------------------ null-move threats


def test_threat_board_passes_the_move_unless_in_check():
    board = chess.Board()
    passed = threat_board(board)
    assert passed is not None and passed.turn == chess.BLACK
    assert threat_board(chess.Board("4k3/8/8/8/8/8/4r3/4K3 w - - 0 1")) is None  # in check


def test_lost_chance_is_not_a_threat(cfg):
    """Passing when I have a tactic lets them defuse it (here: castle). Costly, but not their threat."""
    row = {
        "fen": "r3k2r/8/8/8/8/8/8/4K3 w kq - 0 1",
        "score_cp": 300,
        "score_mate": None,
        "threat_cp": -100,
        "threat_mate": None,
        "threat_uci": "e8g8",
        "threat_pv": "e8g8 e1d2",
    }
    assert not threat_of(row, chess.WHITE, thresholds_for(2100, cfg, True)).real


def test_castling_is_not_a_threat_even_if_the_line_later_wins_material(cfg):
    """The engine castles first when I pass and only wins material further down the line: that is
    my lost chance (or a later threat), not "they threaten O-O"."""
    row = {
        "fen": "r3k2r/8/8/8/8/8/1N6/4K3 w kq - 0 1",
        "score_cp": 300,
        "score_mate": None,
        "threat_cp": -300,
        "threat_mate": None,
        "threat_uci": "e8g8",
        "threat_pv": "e8g8 e1d1 a8a2 d1c1 a2b2",
    }
    assert not threat_of(row, chess.WHITE, thresholds_for(2100, cfg, True)).real


@pytest.mark.parametrize(
    "fen,uci,real",
    [
        ("4k3/8/2p5/8/3R4/4P3/8/4K3 b - - 0 1", "c6c5", True),  # pawn hits a defended rook: real
        ("4k3/n7/8/8/8/2N5/1P6/4K3 b - - 0 1", "a7b5", False),  # knight hits a defended knight: even trade
    ],
)
def test_quiet_threats_need_a_piece_in_danger(cfg, fen, uci, real):
    """`fen` is the position with the opponent to move (as after my pass); rebuild mine from it."""
    mine = fen.replace(" b ", " w ")
    row = {
        "fen": mine,
        "score_cp": 0,
        "score_mate": None,
        "threat_cp": -400,
        "threat_mate": None,
        "threat_uci": uci,
        "threat_pv": uci,
    }
    assert threat_of(row, chess.WHITE, thresholds_for(1500, cfg, True)).real is real


@pytest.mark.parametrize(
    "fen,real",
    [
        ("8/8/8/6k1/4N3/3P4/8/4K3 w - - 0 40", False),  # B4: the king can't take a defended knight
        ("8/8/8/6k1/4N3/8/8/4K3 w - - 0 40", True),  # ...but it can take an undefended one
    ],
)
def test_a_king_among_the_attackers(cfg, fen, real):
    """B4: a king attacking a piece used to crash the threat check (no king in PIECE_VALUES), and
    with it Practice, Today and Progress, once the threat's gain reached the mistake threshold."""
    th = thresholds_for(None, cfg, False)
    row = {
        "fen": fen,
        "score_cp": 300,
        "score_mate": None,
        "threat_cp": -300,
        "threat_mate": None,
        "threat_uci": "g5f5",
        "threat_pv": "g5f5 e1e2",
    }
    threat = threat_of(row, chess.WHITE, th)
    assert threat.gain >= th.mistake and threat.real is real


@pytest.mark.parametrize(
    "fen,threat,real",
    [
        # Black's pawn on d5 attacks the white knight on e4: if White passed, dxe4.
        ("r3k3/p7/8/3p4/4N3/8/P7/R3K3 w - - 0 1", "d5e4", True),
        # Back rank: Black threatens Re1#.
        ("4r1k1/8/8/8/8/8/5PPP/6K1 w - - 0 1", "e8e1", True),
        # Quiet: nothing attacked.
        ("4k3/8/8/8/8/8/3PP3/4K3 w - - 0 1", None, False),
    ],
)
def test_null_move_threats(engine_cfg, fen, threat, real):
    engine = open_engine(engine_cfg)
    board = chess.Board(fen)
    try:
        e = analyse_position(engine, board, engine_cfg)
        cp, mate, uci, pv = analyse_threat(engine, board, engine_cfg)
    finally:
        engine.quit()
    row = {
        "fen": fen,
        "score_cp": e.score_cp,
        "score_mate": e.score_mate,
        "threat_cp": cp,
        "threat_mate": mate,
        "threat_uci": uci,
        "threat_pv": pv,
    }
    t = threat_of(row, chess.WHITE, thresholds_for(1500, engine_cfg, True))
    assert t.real is real
    if threat:
        assert t.uci == threat


def test_my_move_count():
    row = {"start_fen": None, "my_color": "white", "num_plies": 5}
    assert my_move_count(row) == 3  # plies 0, 2, 4
    assert my_move_count({**row, "my_color": "black"}) == 2


# ------------------------------------------------------------ nothing is fixed at one rating


def test_same_position_judged_differently_by_level(cfg):
    """A threat worth 0.2 win chance is a real threat for a 2100 player, not yet for a 900."""
    row = {
        "fen": "4k3/8/8/3p4/4N3/8/8/4K3 w - - 0 1",
        "score_cp": 0,
        "score_mate": None,
        "threat_cp": -110,
        "threat_mate": None,
        "threat_uci": "d5e4",
        "threat_pv": "d5e4",
    }
    gain = Eval(0, None).win_chance() - Eval(-110, None).win_chance()
    assert 0.17 < gain < 0.24
    assert threat_of(row, chess.WHITE, thresholds_for(2100, cfg, True)).real
    assert not threat_of(row, chess.WHITE, thresholds_for(900, cfg, True)).real
    # "Decided" also scales: stronger players are expected to win from smaller edges.
    assert thresholds_for(2100, cfg, True).decided < thresholds_for(900, cfg, True).decided <= 0.95


# ------------------------------------------------------------ deck, cards, level


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    batch = c.execute(
        "INSERT INTO imports (source, games_added, session_done_at) VALUES ('upload', 2, '2026-10-01')"
    ).lastrowid
    for gid, elo in ((1, 900), (2, 1450)):
        c.execute(
            """INSERT INTO games (id, dedupe_key, white, black, white_elo, black_elo, my_color, my_result,
                         date, moves_uci, num_plies, pgn, batch_id, analysed_at, end_time, time_class, rated)
                     VALUES (?, ?, 'me', 'opp', ?, 1000, 'white', 'loss', '2026-10-01', 'e2e4', 1, '', ?,
                             '2026-10-01', ?, 'rapid', 1)""",
            (gid, f"k{gid}", elo, batch, f"2026-10-0{gid} 12:00"),
        )
    # Game 1: a blunder (card) at ply 4; good moves at plies 6 (threat defended), 8 and 10 (quiet).
    c.execute("""INSERT INTO mistakes (game_id, ply, judgement, win_drop, phase, kind, is_mine, sharpness)
                 VALUES (1, 4, 'blunder', 0.5, 'middlegame', 'hanging_piece', 1, 0.4)""")
    for ply, threat_cp in ((4, -100), (6, -600), (8, 0), (10, -10)):
        # Black's rook on h8 can take the white knight on h4 (the threat line when it matters).
        c.execute(
            """INSERT INTO positions (game_id, ply, fen, score_cp, threat_cp, threat_uci, threat_pv)
                     VALUES (1, ?, '7r/8/8/8/7N/8/8/K6k w - - 0 1', 0, ?, 'h8h4', 'h8h4')""",
            (ply, threat_cp),
        )
        if ply != 4:
            c.execute(
                """INSERT INTO mistakes (game_id, ply, judgement, win_drop, phase, is_mine, mark)
                         VALUES (1, ?, 'good', 0.0, 'middlegame', 1, 'good')""",
                (ply,),
            )
    c.commit()
    yield c
    c.close()


def test_current_rating_uses_latest_game(conn):
    assert level.current_rating(conn) == 1450


def test_calm_positions_mix_threats_and_quiet(conn, cfg):
    picks = srs.calm_positions(conn, cfg, 2, "seed")
    assert (1, 6) in picks  # the one threatened position
    assert len(picks) == 2 and set(picks) <= {(1, 6), (1, 8), (1, 10)}
    assert srs.calm_positions(conn, cfg, 2, "seed") == picks  # same deck all day


def test_calm_cards_only_when_failed(conn, cfg):
    srs.record(conn, 1, 8, "solved", DAY, kind="position")
    assert conn.execute("SELECT COUNT(*) FROM cards WHERE ply = 8").fetchone()[0] == 0
    srs.record(conn, 1, 8, "failed", DAY, kind="position")
    assert conn.execute("SELECT kind FROM cards WHERE ply = 8").fetchone()[0] == "position"
    assert (1, 8) in srs.queue(conn, cfg, date(2026, 10, 17))["due"]


def test_decided_mistakes_are_not_new_cards(conn, cfg):
    conn.execute("UPDATE mistakes SET decided = 1 WHERE ply = 4")
    conn.commit()
    assert srs.queue(conn, cfg, DAY)["new"] == []


def test_recapture_pending_skips_the_threat_question():
    from chessreview.threats import recapture_pending

    prev = chess.Board("r1bqkbnr/pppp1ppp/2n5/4p3/3PP3/5N2/PPP2PPP/RNBQKB1R b KQkq - 0 3")
    exd4 = "e5d4"
    after = prev.copy()
    after.push_uci(exd4)
    assert recapture_pending(prev.fen(), exd4, after.fen())  # Nxd4 takes back
    quiet = chess.Board()
    quiet_after = quiet.copy()
    quiet_after.push_uci("e2e4")
    assert not recapture_pending(quiet.fen(), "e2e4", quiet_after.fen())
