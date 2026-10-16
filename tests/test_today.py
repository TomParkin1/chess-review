"""v3 milestone 2: the Today workout (coach.py) and the progress report (progress.py)."""
from dataclasses import replace
from datetime import date

import pytest

from chessreview import coach, db, progress, srs
from chessreview.config import load_config
from chessreview.progress import GameStats, window_metrics

DAY = date.today()   # review results are stamped with the real date


@pytest.fixture
def cfg():
    base = load_config()
    return replace(base, coach=replace(base.coach, real_game=4, calm_ratio=0.5, focus=3, due_cap=1))


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    batch = c.execute("INSERT INTO imports (source, games_added, session_done_at) VALUES ('upload', 1, '2026-10-01')").lastrowid
    c.execute("""INSERT INTO games (id, dedupe_key, white, black, white_elo, black_elo, my_color, my_result, date,
                     moves_uci, num_plies, pgn, batch_id, analysed_at, end_time, time_class, rated)
                 VALUES (1, 'k1', 'me', 'opp', 1000, 1000, 'white', 'loss', '2026-10-01', 'e2e4', 1, '', ?,
                         '2026-10-01', '2026-10-01 12:00', 'rapid', 1)""", (batch,))
    # Five hanging-piece blunders (plies 2..10) and good moves with/without a threat (12..18).
    for ply in range(2, 12, 2):
        c.execute("""INSERT INTO mistakes (game_id, ply, judgement, win_drop, phase, kind, is_mine)
                     VALUES (1, ?, 'blunder', 0.5, 'middlegame', 'hanging_piece', 1)""", (ply,))
    for ply, threat_cp in ((12, -600), (14, 0), (16, -600), (18, 0)):
        c.execute("""INSERT INTO mistakes (game_id, ply, judgement, win_drop, phase, is_mine, mark)
                     VALUES (1, ?, 'good', 0, 'middlegame', 1, 'good')""", (ply,))
        c.execute("""INSERT INTO positions (game_id, ply, fen, score_cp, threat_cp, threat_uci, threat_pv)
                     VALUES (1, ?, '7r/8/8/8/7N/8/8/K6k w - - 0 1', 0, ?, 'h8h4', 'h8h4')""", (ply, threat_cp))
    c.commit()
    srs.ensure_cards(c, load_config())
    c.execute("UPDATE cards SET due_on = ?, box = 1, introduced_on = '2026-10-01' WHERE ply = 2", (DAY.isoformat(),))
    c.commit()
    yield c
    c.close()


def test_plan_blocks_and_progress(conn, cfg, monkeypatch):
    weakness = {"title": "Hanging pieces", "detail": "5 of your mistakes", "train": {"kind": "hanging_piece"}}
    monkeypatch.setattr(coach.insights, "compute", lambda *a, **k: {"weaknesses": [weakness]})
    plan = coach.today_plan(conn, cfg, DAY)
    blocks = {b["id"]: b for b in plan["blocks"]}
    assert [g_p[:2] for g_p in blocks["reviews"]["items"]] == [[1, 2]]           # due card, capped at 1
    real_tags = [tag for *_, tag in blocks["real"]["items"]]
    assert real_tags.count("new") == 2 and real_tags.count("calm") == 2
    focus = blocks["focus"]["items"]
    assert blocks["focus"]["title"] == "Focus: Hanging pieces" and 0 < len(focus) <= 3
    used = [tuple(i[:2]) for b in plan["blocks"] for i in b["items"]]
    assert len(used) == len(set(used))                                         # no position twice
    assert plan["finished"] == 0 and not plan["complete"]

    conn.execute("INSERT INTO review_results (game_id, ply, result) VALUES (1, 2, 'solved')")
    conn.commit()
    again = coach.today_plan(conn, cfg, DAY)
    assert again["blocks"][0]["done"] and again["finished"] == 1
    assert [b["items"] for b in again["blocks"]] == [b["items"] for b in plan["blocks"]]   # frozen for the day


def test_window_metrics():
    games = [GameStats("2026-10-01", "win", blunders=2, after_error=1, threats_missed=1, reached_win=True, accuracy=80, rating=1000),
             GameStats("2026-10-02", "loss", blunders=0, reached_win=True, accuracy=60, rating=1010),
             GameStats("2026-10-03", "loss", blunders=1, accuracy=70, rating=990)]
    m = window_metrics(games)
    assert m["blunders"] == 1 and m["threats_missed"] == pytest.approx(1 / 3)
    assert m["after_error"] == pytest.approx(100 / 3) and m["conversion"] == 50 and m["rating"] == 990


def test_report_needs_enough_games_for_a_baseline(conn, cfg):
    r = progress.report(conn, cfg)
    assert r["games"] == 1 and r["baseline"] is None
    assert "more to see a trend" in r["summary"]
    assert r["current"]["blunders"] == 5 and r["current"]["threats_missed"] == 0   # no threat data on blunders
