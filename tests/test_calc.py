"""E6: the calculation trainer (F10)."""
import shutil
from dataclasses import replace

import chess
import pytest

from chessreview import calc, coach, db, library, srs
from chessreview.config import load_config
from chessreview.web import create_app

LINE = "e2e4 e7e5 g1f3 b8c6 f1c4 g8f6 f3g5 d7d5"        # the puzzle's stored moves (opponent first)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "c.db")
    c.execute("""INSERT INTO puzzles (id, fen, moves, rating, deviation, popularity, plays, themes, game_url)
                 VALUES ('p1', ?, ?, 1500, 80, 90, 1000, ' opening ', '')""", (chess.STARTING_FEN, LINE))
    c.execute("""INSERT INTO games (id, dedupe_key, white, black, my_color, my_result, date, moves_uci, num_plies, pgn,
                                    analysed_at, end_time)
                 VALUES (1, 'k1', 'me', 'opp', 'white', 'win', '2026-10-01', ?, 8, '', '2026-10-01', '2026-10-01')""",
              (LINE,))
    c.execute("INSERT INTO mistakes (game_id, ply, judgement, phase, is_mine) VALUES (1, 2, 'good', 'middlegame', 1)")
    c.commit()
    yield c
    c.close()


def test_depths_start_where_the_solver_is_to_move_at_the_end(conn):
    odd = calc.build(conn, "lib:p1", 3, "best")
    assert odd.fen == chess.STARTING_FEN and odd.line == LINE.split()[:3] and odd.answer == "Nc6"
    even = calc.build(conn, "lib:p1", 4, "best")           # from after the opponent's first move
    assert even.line == LINE.split()[1:5] and even.answer == "Nf6" and even.to_move == "black"
    assert even.orientation == "black" and even.line_san == "1...e5 2. Nf3 Nc6 3. Bc4"
    assert calc.build(conn, "lib:p1", 6, "best").answer == "d5"   # 1 + 6 + the answer: all 8 stored moves
    conn.execute("UPDATE puzzles SET moves = ? WHERE id = 'p1'", (" ".join(LINE.split()[:7]),))
    assert calc.build(conn, "lib:p1", 6, "best") is None     # one move short
    assert calc.build(conn, "game:1:2", 3, "best") is None   # best moves only from checked puzzle lines


def test_where_needs_tracking(conn):
    assert calc.build(conn, "lib:p1", 6, "where", "g1") is None   # Nf3, Ng5: the only white knight that moved
    assert calc.build(conn, "lib:p1", 6, "where", "f1") is None   # one bishop moved: just reading
    both = calc.build(conn, "lib:p1", 6, "where", "b8")      # both black knights moved: which went where?
    assert both.answer == "c6" and "black knight that starts on b8" in both.prompt
    assert calc.build(conn, "lib:p1", 6, "where", "g8").answer == "f6"


def test_capture_is_a_static_exchange_at_the_end(conn):
    q = calc.build(conn, "lib:p1", 6, "capture", "e4")       # ...Nf6 attacks e4, but Ng5 guards it
    assert q.answer == "no" and "the white pawn on e4" in q.prompt and q.to_move == "black"
    assert calc.build(conn, "lib:p1", 6, "capture", "c4") is None   # not attacked: no question


def test_a_key_rebuilds_the_same_question(conn):
    for kind in calc.KINDS:
        q = calc.build(conn, "lib:p1", 6 if kind != "best" else 3, kind)
        assert calc.from_key(conn, q.key) == q
    assert calc.from_key(conn, "lib:gone~3~best~") is None and calc.from_key(conn, "nonsense") is None


def test_game_lines_are_the_moves_played(conn):
    q = calc.build(conn, "game:1:2", 3, "capture") or calc.build(conn, "game:1:2", 3, "where")
    assert q is None or q.line == LINE.split()[2:5]
    q = calc.build(conn, "game:1:2", 4, "where", "b8")       # Nc6 and Nf6: both black knights moved
    assert q.line == LINE.split()[2:6] and q.orientation == "white" and q.answer == "c6"


def test_judging():
    q = calc.Question(key="k", source="lib:x", kind="best", plies=3, subject="", fen="", line=[], line_san="",
                      to_move="white", orientation="white", prompt="", answer="Ra8#",
                      final_fen="6k1/5ppp/8/8/8/8/8/R2R2K1 w - - 0 1", title="")
    assert calc.judge(q, "Ra8#") and calc.judge(q, "Ra8") and calc.judge(q, "a1a8")
    assert calc.judge(q, "Rd8#")                             # another mate counts
    assert not calc.judge(q, "Kf2") and not calc.judge(q, "zz") and not calc.judge(q, "a1a9")
    yn = replace(q, kind="capture", answer="no")
    assert calc.judge(yn, "No") and not calc.judge(yn, "yes")


def test_answers_cards_and_the_adaptive_depth(conn):
    key3 = calc.build(conn, "lib:p1", 3, "best").key
    conn.execute("INSERT INTO puzzles (id, fen, moves, rating, deviation, popularity, plays, themes, game_url) "
                 "SELECT 'p2', fen, moves, rating, deviation, popularity, plays, themes, game_url FROM puzzles WHERE id = 'p1'")
    key3b = calc.build(conn, "lib:p2", 3, "best").key               # a second new question
    r = calc.answer(conn, key3, "Nc6")
    assert r["correct"] and r["level"] == 1 and not r["level_changed"]
    assert conn.execute("SELECT COUNT(*) FROM cards WHERE kind = 'calc'").fetchone()[0] == 0   # right: no card
    r = calc.answer(conn, key3b, "Nc6")                              # two new ones right at depth 3: deeper
    assert r["level_changed"] and r["level"] == 2 and r["plies"] == 4
    key4 = calc.build(conn, "lib:p1", 4, "best").key
    r = calc.answer(conn, key4, "Kf2")                               # a miss: shallower, and a card
    assert not r["correct"] and r["level"] == 1 and r["expected"] == "Nf6"
    assert conn.execute("SELECT key FROM cards WHERE kind = 'calc'").fetchone()["key"] == key4
    r = calc.answer(conn, key4, "Nf6")                               # the same one again: a review
    assert r["correct"] and not r["level_changed"] and r["level"] == 1
    r = calc.answer(conn, key3, "Nc6")                               # answered before: not new either
    assert not r["level_changed"] and r["level"] == 1


def test_due_cards_come_first_and_used_puzzles_leave_the_puzzle_page(conn):
    key = calc.build(conn, "lib:p1", 4, "best").key
    calc.answer(conn, key, "Kf2")                            # missed: a card, due tomorrow
    conn.execute("UPDATE cards SET due_on = ? WHERE kind = 'calc'", (srs.today().isoformat(),))
    conn.commit()
    q, due = calc.next_question(conn)
    assert due and q.key == key
    assert library.pick(conn, 5, around=1500) == []          # p1 was used here: not offered on Puzzles
    assert srs.streak(conn) == 1                            # calculating counts as training
    db.delete_all_games(conn)
    assert conn.execute("SELECT COUNT(*) FROM calc_attempts").fetchone()[0] == 0


def test_today_links_the_trainer_for_a_calculation_weakness(conn, monkeypatch):
    cfg = load_config()
    weakness = {"title": "Letting advantages slip", "detail": "", "train": {"kind": "missed_opportunity"}}
    monkeypatch.setattr(coach.insights, "compute", lambda *a, **k: {"weaknesses": [weakness]})
    focus = coach.build_plan(conn, cfg, srs.today())
    block = next((b for b in focus["blocks"] if b["id"] == "focus"), None)
    assert block is None or block["calc"]
    plan = {"blocks": [{"id": "focus", "title": "", "items": [], "calc": True}]}
    assert coach.with_progress(conn, plan, srs.today())["total"] == 0      # the link isn't counted


@pytest.fixture
def client(tmp_path):
    base = load_config()
    if not shutil.which(base.engine.path):
        pytest.skip("Stockfish not installed")
    app = create_app(replace(base, db_path=tmp_path / "web.db"))
    app.testing = True
    yield app.test_client()
    app.extensions["live"].close()


def test_the_api(client):
    o = client.get("/api/calc").get_json()
    assert o["level"] == 1 and o["plies"] == 3 and o["due"] == 0
    assert client.get("/api/calc/next").get_json() == {"question": None, "due": False}   # nothing to ask yet
    assert client.post("/api/calc/answer", json={"key": "lib:x~3~best~", "answer": "e4"}).status_code == 404
    assert client.post("/api/calc/answer", json={"key": 3}).status_code == 400
