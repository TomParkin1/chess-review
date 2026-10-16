"""Spaced repetition (srs.py) and the new-games session (session.py)."""
from dataclasses import replace
from datetime import date, timedelta

import pytest

from chessreview import db, session, srs
from chessreview.config import load_config

DAY = date(2026, 10, 16)


def test_schedule_moves_through_boxes_and_retires():
    box, due, mastered = 0, None, None
    expected_gaps = [1, 3, 7, 14, 30, 60]
    for gap in expected_gaps:
        box, due, mastered = srs.schedule(box, "solved", DAY)
        assert date.fromisoformat(due) - DAY == timedelta(days=gap)
        assert mastered is None
    box, due, mastered = srs.schedule(box, "solved", DAY)   # passed the 60-day box
    assert mastered == DAY.isoformat() and due is None


@pytest.mark.parametrize("result,box_after,days", [
    ("solved_with_help", 3, 1),   # stays in its box, back tomorrow
    ("failed", 0, 1),             # back to the start
    ("skipped", 0, 1),
])
def test_schedule_after_help_or_failure(result, box_after, days):
    box, due, mastered = srs.schedule(3, result, DAY)
    assert box == box_after and (date.fromisoformat(due) - DAY).days == days and mastered is None


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    # Two games in one finished batch and one game in an open (new) batch, one mistake each.
    old = c.execute("INSERT INTO imports (source, games_added, session_done_at) VALUES ('upload', 2, '2026-10-01')").lastrowid
    new = c.execute("INSERT INTO imports (source, games_added) VALUES ('chess.com', 1)").lastrowid
    for i, (batch, drop) in enumerate([(old, 0.5), (old, 0.3), (new, 0.6)], start=1):
        c.execute("""INSERT INTO games (id, dedupe_key, white, black, my_color, my_result, date, moves_uci,
                         num_plies, pgn, batch_id, analysed_at, end_time, clocks_ms, time_control,
                         accuracy_white, accuracy_black)
                     VALUES (?, ?, 'me', 'opp', 'white', 'loss', '2026-10-01', 'e2e4', 1, '', ?, '2026-10-01',
                             ?, '500000 590000 100000 580000 15000', '600', ?, 80)""",
                  (i, f"k{i}", batch, f"2026-10-0{i} 12:00:00", 60 + i))
        c.execute("""INSERT INTO mistakes (game_id, ply, judgement, win_drop, phase, kind, is_mine)
                     VALUES (?, 4, 'blunder', ?, 'middlegame', 'hanging_piece', 1)""", (i, drop))
        c.execute("""INSERT INTO mistakes (game_id, ply, judgement, win_drop, phase, is_mine)
                     VALUES (?, 5, 'blunder', 0.9, 'middlegame', 0)""", (i,))   # opponent's: no card
    c.commit()
    yield c
    c.close()


@pytest.fixture
def cfg():
    base = load_config()
    return replace(base, training=replace(base.training, new_per_day=10))


def test_queue_new_cards_skip_open_session(conn, cfg):
    q = srs.queue(conn, cfg, DAY)
    assert q["due"] == []
    assert q["new"] == [(1, 4), (2, 4)]          # worst first; game 3 waits for its session
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 3  # opponent moves get none


def test_record_makes_cards_due_and_counts_new_per_day(conn, cfg):
    srs.queue(conn, cfg, DAY)
    srs.record(conn, 1, 4, "failed", DAY)
    q = srs.queue(conn, cfg, DAY)
    assert (1, 4) not in q["due"] and (1, 4) not in q["new"]   # due tomorrow, not today
    tomorrow = date(2026, 10, 17)
    assert srs.queue(conn, cfg, tomorrow)["due"] == [(1, 4)]

    small = replace(cfg, training=replace(cfg.training, new_per_day=1))
    assert srs.queue(conn, small, DAY)["new"] == []           # today's one new card was used up
    assert srs.queue(conn, small, tomorrow)["new"] == [(2, 4)]


def test_streak(conn):
    for day in ("2026-10-14 10:00:00", "2026-10-15 10:00:00", "2026-10-16 09:00:00"):
        conn.execute("INSERT INTO review_results (game_id, ply, result, at) VALUES (1, 4, 'solved', ?)", (day,))
    conn.commit()
    assert srs.streak(conn, DAY) >= 2   # local-time conversion may shift by a day at most


def test_session_status_summary_and_done(conn, cfg):
    st = session.status(conn, cfg)
    assert st["open"] and st["games"] == 1 and st["puzzles"] == 1
    summary = session.summary(conn, cfg, st["batch_ids"])
    assert summary["games"] == 1 and summary["losses"] == 1 and summary["blunders"] == 1
    assert summary["top_kinds"] == [("hanging_piece", 1)]
    # ply 4 blunder: my clock before it is the value after ply 2 (100000 ms) -> "1–3 min"
    assert dict(summary["blunder_clock"])["1–3 min"] == 1
    session.mark_done(conn, st["batch_ids"])
    assert session.status(conn, cfg)["open"] is False
    assert (3, 4) in srs.queue(conn, cfg, DAY)["new"]          # now it joins daily training
