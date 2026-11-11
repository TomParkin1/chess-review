"""E5: sparring against a rating-limited Stockfish from my positions (F9)."""
import shutil
from dataclasses import replace

import chess
import pytest

from chessreview import db, spar, srs
from chessreview.config import load_config
from chessreview.web import create_app

START = chess.STARTING_FEN
MATE_IN_ONE = "6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1"       # Ra8#


# ------------------------------------------------------------ strength

def test_strength_uses_uci_elo_from_the_floor():
    s = spar.strength(1600)
    assert s.exact and s.options == {"UCI_LimitStrength": True, "UCI_Elo": 1600, "Skill Level": 20}
    assert spar.strength(5000).elo == spar.ELO_MAX


def test_below_the_floor_it_is_skill_zero_with_a_shallow_search():
    depths = [spar.strength(elo).limit.depth for elo in (400, 800, 1000, 1300)]
    assert depths == sorted(depths) and depths[0] == 1 and depths[-1] == 4      # weaker targets search less
    s = spar.strength(860)
    assert not s.exact and s.options == {"UCI_LimitStrength": False, "Skill Level": 0} and "approximate" in s.label


def test_the_target_is_my_rating_plus_a_clamped_offset():
    assert spar.target_elo(860, 200) == 1060
    assert spar.target_elo(860, 9999) == 860 + spar.OFFSETS[-1]
    assert spar.target_elo(None, 0) == spar.DEFAULT_RATING


# ------------------------------------------------------------ positions and outcomes

@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "s.db")
    c.execute("""INSERT INTO games (id, dedupe_key, white, black, my_color, my_result, date, moves_uci, num_plies, pgn,
                                    analysed_at, end_time)
                 VALUES (1, 'k1', 'me', 'opp', 'white', 'loss', '2026-10-01', '', 60, '', '2026-10-01', '2026-10-01')""")
    rows = [  # ply, phase, my eval (White = me), FEN (White to move)
        (20, "middlegame", 400, START), (22, "middlegame", 50, START), (24, "middlegame", -20, START),
        (40, "endgame", -400, MATE_IN_ONE), (42, "endgame", 120, MATE_IN_ONE)]
    for ply, phase, cp, fen in rows:
        c.execute("INSERT INTO mistakes (game_id, ply, judgement, phase, is_mine) VALUES (1, ?, 'good', ?, 1)", (ply, phase))
        c.execute("INSERT INTO positions (game_id, ply, fen, score_cp) VALUES (1, ?, ?, ?)", (ply, fen, cp))
    c.commit()
    yield c
    c.close()


def test_positions_are_the_first_equal_middlegame_and_equal_or_better_endgame(conn):
    p = spar.positions(conn)
    assert [x["key"] for x in p["middlegame"]] == ["1:22"]     # +4 isn't equal; the first equal one
    assert [x["key"] for x in p["endgame"]] == ["1:42"]        # -4 is lost; +1.2 is fine
    assert p["middlegame"][0]["color"] == "white" and p["middlegame"][0]["opponent"] == "opp"


def test_outcomes():
    board = chess.Board(MATE_IN_ONE)
    board.push_uci("a1a8")
    assert spar.outcome(board, chess.WHITE) == ("win", "Checkmate!")
    assert spar.outcome(board, chess.BLACK) == ("loss", "Checkmated.")
    assert spar.outcome(chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1"), chess.WHITE)[0] == "draw"   # stalemate
    assert spar.outcome(chess.Board(), chess.WHITE) is None


# ------------------------------------------------------------ the API

@pytest.fixture
def client(tmp_path):
    base = load_config()
    if not shutil.which(base.engine.path):
        pytest.skip("Stockfish not installed")
    app = create_app(replace(base, db_path=tmp_path / "web.db", review=replace(base.review, live_time=0.05)))
    app.testing = True
    yield app.test_client(), app
    app.extensions["live"].close()


def test_a_game_from_the_start_as_black(client):
    c, app = client
    r = c.post("/api/spar/move", json={"fen": START, "color": "black", "moves": [], "offset": 0}).get_json()
    assert r["status"] == "playing" and r["reply_uci"] and "uci" not in r          # Stockfish moved first
    moves = [r["reply_uci"]]
    board = chess.Board()
    board.push_uci(moves[0])
    mine = next(iter(board.legal_moves)).uci()
    r = c.post("/api/spar/move", json={"fen": START, "color": "black", "moves": moves, "uci": mine}).get_json()
    assert r["uci"] == mine and r["reply_uci"]
    bad = c.post("/api/spar/move", json={"fen": START, "color": "black", "moves": moves, "uci": "a1a8"})
    assert bad.status_code == 400
    assert c.post("/api/spar/move", json={"fen": START, "color": "white", "moves": []}).status_code == 400   # my move
    assert c.post("/api/spar/move", json={"fen": "8/8/8/8/8/8/8/8 w - - 0 1", "color": "white", "moves": []}).status_code == 400


def test_the_limited_options_never_reach_the_judging_engine(client):
    c, app = client
    c.post("/api/spar/move", json={"fen": START, "color": "black", "moves": [], "offset": 0})
    live = app.extensions["live"]
    assert live._spar_engine.protocol.config.get("Skill Level") == 0
    live.analyse(chess.Board())                       # the judging engine starts now
    judge = live._judge_engine.protocol.config
    assert judge.get("Skill Level", 20) == 20 and not judge.get("UCI_LimitStrength", False)


def test_finish_review_history_and_streak(client, tmp_path):
    c, app = client
    r = c.post("/api/spar/move", json={"fen": MATE_IN_ONE, "color": "white", "moves": [], "uci": "a1a8"}).get_json()
    assert r["status"] == "win" and "reply_uci" not in r
    unfinished = {"fen": START, "color": "white", "moves": ["e2e4"]}
    assert c.post("/api/spar/finish", json=unfinished).status_code == 400            # not over, not resigned
    resigned = c.post("/api/spar/finish", json=unfinished | {"resigned": True, "offset": 100}).get_json()
    assert resigned["result"] == "loss" and resigned["reason"] == "You resigned."
    won = c.post("/api/spar/finish", json={"source": "fen", "fen": MATE_IN_ONE, "color": "white",
                                           "moves": ["a1a8"], "resigned": True}).get_json()
    assert won["result"] == "win"                                                    # over: the board decides
    assert {(h["offset"], h["won"], h["lost"]) for h in won["history"]["by_offset"]} == {(0, 1, 0), (100, 0, 1)}
    review = c.post("/api/spar/review", json={"id": won["id"]}).get_json()
    assert review["next"] is None and len(review["moves"]) == 1
    move = review["moves"][0]
    assert move["mine"] and move["san"] == "Ra8#" and move["judgement"] == "good" and move["best_san"] is None
    assert c.post("/api/spar/review", json={"id": 999}).status_code == 404
    conn = db.connect(tmp_path / "web.db")
    assert srs.streak(conn) == 1                                                     # sparring counts as training
    conn.close()


def test_the_offset_is_remembered(client):
    c, _ = client
    assert c.post("/api/spar/offset", json={"offset": 150}).get_json() == {"offset": 150}
    assert c.post("/api/spar/offset", json={"offset": 9999}).get_json() == {"offset": spar.OFFSETS[-1]}
    overview = c.get("/api/spar").get_json()
    assert overview["offset"] == spar.OFFSETS[-1] and len(overview["levels"]) == len(spar.OFFSETS)
