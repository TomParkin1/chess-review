"""v3 milestone 2: the Today workout (coach.py) and the progress report (progress.py)."""
import json
from dataclasses import replace

import pytest

from chessreview import coach, db, progress, srs
from chessreview.config import load_config
from chessreview.progress import GameStats, window_metrics

DAY = srs.today()    # review results are stamped with the real time (training day from 04:00)


@pytest.fixture
def cfg():
    """10 minutes at the default 60 s a puzzle: the first essential endgame (2 min), 8 puzzle slots."""
    base = load_config()
    return replace(base, coach=replace(base.coach, calm_ratio=0.5, budget_minutes=10, seconds_per_puzzle=60),
                   training=replace(base.training, new_per_day=10))


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    fill(c)
    yield c
    c.close()


def fill(c):
    """One analysed game: five hanging-piece blunders with due/new cards, and calm positions."""
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
    for ply in range(1, 20):        # every analysed move has the positions before and after it
        c.execute("""INSERT OR IGNORE INTO positions (game_id, ply, fen, score_cp)
                     VALUES (1, ?, '7r/8/8/8/7N/8/8/K6k w - - 0 1', 0)""", (ply,))
    c.commit()
    srs.ensure_cards(c, load_config())
    c.execute("UPDATE cards SET due_on = ?, box = 1, introduced_on = '2026-10-01' WHERE ply = 2", (DAY.isoformat(),))
    c.commit()


WEAKNESS = {"title": "Hanging pieces", "detail": "5 of your mistakes", "train": {"kind": "hanging_piece"}}


def test_plan_blocks_and_progress(conn, cfg, monkeypatch):
    weakness = {"title": "Hanging pieces", "detail": "5 of your mistakes", "train": {"kind": "hanging_piece"}}
    monkeypatch.setattr(coach.insights, "compute", lambda *a, **k: {"weaknesses": [weakness]})
    plan = coach.today_plan(conn, cfg, DAY)
    blocks = {b["id"]: b for b in plan["blocks"]}
    assert [g_p[:2] for g_p in blocks["reviews"]["items"]] == [[1, 2]]           # the one due card
    assert blocks["convert"]["key"] == "endgame/kq_k"                         # endgames first: 2 of 10 minutes
    assert "endgame" not in blocks                                            # no endgame mistakes, no library
    # 7 fresh slots: 4 real-game (2 new + 2 calm) and 3 focus, of which two can be filled (the
    # other blunders); nothing else is due, so the spare one goes to a third calm position.
    real_tags = [tag for *_, tag in blocks["real"]["items"]]
    assert real_tags.count("new") == 2 and real_tags.count("calm") == 3
    focus = blocks["focus"]["items"]
    assert blocks["focus"]["title"] == "Focus: Hanging pieces" and focus == [[1, 8, "focus"], [1, 10, "focus"]]
    assert plan["budget"] == 10 and plan["pace"] == 60 and plan["minutes_left"] == 10
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


# ------------------------------------------------------------ M1: plans that can't break (B1, B6)

def test_plan_on_an_empty_database(tmp_path, cfg):
    """B1: no analysed game yet -> insights.compute has no "weaknesses"; Today used to crash (500)."""
    c = db.connect(tmp_path / "empty.db")
    plan = coach.today_plan(c, cfg, DAY)
    assert plan["blocks"] == [] and plan["total"] == 0 and not plan["stale"]
    c.close()


def test_empty_plan_is_rebuilt_once_games_are_analysed(tmp_path, cfg, monkeypatch):
    """B6: the plan was frozen at the first page load of the day, e.g. before any game was analysed."""
    monkeypatch.setattr(coach.insights, "compute", lambda *a, **k: {"weaknesses": [WEAKNESS]})
    c = db.connect(tmp_path / "first.db")
    assert coach.today_plan(c, cfg, DAY)["total"] == 0
    fill(c)                                   # the first sync's games are analysed
    plan = coach.today_plan(c, cfg, DAY)
    assert plan["total"] > 0 and not plan["stale"]
    c.close()


def test_plan_rebuilt_when_its_positions_vanish(conn, cfg, monkeypatch):
    """B6: after Delete all games the old plan listed 27 items that could never be served."""
    monkeypatch.setattr(coach.insights, "compute", lambda *a, **k: {"weaknesses": [WEAKNESS]})
    assert coach.today_plan(conn, cfg, DAY)["total"] > 0
    db.delete_all_games(conn)
    plan = coach.today_plan(conn, cfg, DAY)
    assert plan["total"] == 0 and plan["blocks"] == [] and plan["complete"]


def test_new_games_mark_a_started_plan_stale_and_refresh_keeps_progress(conn, cfg, monkeypatch):
    """B6: games analysed later in the day never reached the plan. A plan I have started stays
    (no reshuffle mid-workout) but says it's stale; Refresh rebuilds it and keeps what I did."""
    monkeypatch.setattr(coach.insights, "compute", lambda *a, **k: {"weaknesses": [WEAKNESS]})
    plan = coach.today_plan(conn, cfg, DAY)
    conn.execute("INSERT INTO review_results (game_id, ply, result) VALUES (1, 2, 'solved')")
    conn.execute("""INSERT INTO games (id, dedupe_key, my_color, my_result, moves_uci, num_plies, pgn, analysed_at)
                    VALUES (2, 'k2', 'white', 'win', 'e2e4', 1, '', '2026-10-19')""")
    conn.commit()
    stale = coach.today_plan(conn, cfg, DAY)
    assert stale["stale"] and stale["new_games"] == 1
    assert [b["items"] for b in stale["blocks"]] == [b["items"] for b in plan["blocks"]]
    fresh = coach.today_plan(conn, cfg, DAY, refresh=True)
    assert not fresh["stale"] and fresh["finished"] == 1
    reviews = next(b for b in fresh["blocks"] if b["id"] == "reviews")["items"]
    assert reviews == [[1, 2, "due"]]                      # kept, and the block is still its usual size


def test_refreshing_a_finished_day_adds_no_second_workout(conn, cfg, monkeypatch):
    monkeypatch.setattr(coach.insights, "compute", lambda *a, **k: {"weaknesses": [WEAKNESS]})
    plan = coach.today_plan(conn, cfg, DAY)
    assert "convert" in [b["id"] for b in plan["blocks"]]
    for block in plan["blocks"]:
        for g, p, _ in block["items"]:
            conn.execute("INSERT INTO review_results (game_id, ply, result) VALUES (?, ?, 'solved')", (g, p))
    conn.commit()
    srs.record_key(conn, "endgame", "kq_k", "solved", DAY)    # the endgame played: the next one isn't added
    assert coach.today_plan(conn, cfg, DAY)["complete"]
    again = coach.today_plan(conn, cfg, DAY, refresh=True)
    assert again["complete"] and again["total"] == plan["total"]
    assert [b["id"] for b in again["blocks"]] == [b["id"] for b in plan["blocks"]]   # the played block stays


def test_a_plan_from_before_the_basis_existed_can_go_stale(conn, cfg, monkeypatch):
    """Plans stored by the previous version have no basis: the first look records one."""
    monkeypatch.setattr(coach.insights, "compute", lambda *a, **k: {"weaknesses": [WEAKNESS]})
    plan = coach.build_plan(conn, cfg, DAY)
    conn.execute("INSERT INTO review_results (game_id, ply, result) VALUES (1, 2, 'solved')")
    db.set_meta(conn, coach.PLAN_KEY, json.dumps(plan))
    conn.commit()
    assert not coach.today_plan(conn, cfg, DAY)["stale"]
    conn.execute("""INSERT INTO games (id, dedupe_key, my_color, my_result, moves_uci, num_plies, pgn, analysed_at)
                    VALUES (2, 'k2', 'white', 'win', 'e2e4', 1, '', '2026-10-19')""")
    conn.commit()
    assert coach.today_plan(conn, cfg, DAY)["stale"]


def test_a_plan_from_before_the_budget_is_resized_keeping_progress(conn, cfg, monkeypatch):
    """M2: a plan stored by the previous version (fixed sizes, no budget) is rebuilt once."""
    monkeypatch.setattr(coach.insights, "compute", lambda *a, **k: {"weaknesses": [WEAKNESS]})
    plan = coach.build_plan(conn, cfg, DAY)
    del plan["budget"]
    conn.execute("INSERT INTO review_results (game_id, ply, result) VALUES (1, 2, 'solved')")
    db.set_meta(conn, coach.PLAN_KEY, json.dumps({**plan, "basis": coach.analysed_games(conn)}))
    conn.commit()
    resized = coach.today_plan(conn, cfg, DAY)
    assert resized["budget"] == 10 and resized["finished"] == 1


def test_untouched_plan_follows_new_games(conn, cfg, monkeypatch):
    monkeypatch.setattr(coach.insights, "compute", lambda *a, **k: {"weaknesses": [WEAKNESS]})
    coach.today_plan(conn, cfg, DAY)
    conn.execute("""INSERT INTO games (id, dedupe_key, my_color, my_result, moves_uci, num_plies, pgn, analysed_at)
                    VALUES (2, 'k2', 'white', 'win', 'e2e4', 1, '', '2026-10-19')""")
    conn.commit()
    plan = coach.today_plan(conn, cfg, DAY)
    assert not plan["stale"] and plan["basis"] == 2


def test_pace_counts_retries_on_the_same_training_day(conn, cfg):
    """E1: attempts were grouped by UTC calendar date, so a retry around midnight UTC looked like a
    new puzzle and the plan underestimated how long each puzzle takes."""
    with conn:
        conn.execute("""INSERT INTO review_results (game_id, ply, result, at) VALUES
                        (1, 2, 'failed', '2026-11-09 23:50:00'), (1, 2, 'solved', '2026-11-10 00:10:00')""")
    assert coach.pace(conn, cfg) == 2 * cfg.coach.seconds_per_puzzle     # one puzzle, two attempts


def test_new_per_day_counts_only_positions_from_my_games(conn, cfg):
    """E1: failed library puzzles and drilled lines used up training.new_per_day."""
    with conn:
        conn.execute("INSERT INTO cards (kind, key, introduced_on) VALUES ('library', 'p1', ?)", (DAY.isoformat(),))
        conn.execute("INSERT INTO cards (kind, key, introduced_on) VALUES ('line', 'white:x', ?)", (DAY.isoformat(),))
    assert srs.introduced_on(conn, DAY) == 0


def test_training_weeks_dont_split_at_new_year(conn):
    """E1: %Y-%W put 31 Dec 2026 and 1 Jan 2027 (the same Monday-Sunday week) in two weeks."""
    with conn:
        conn.execute("""INSERT INTO review_results (game_id, ply, result, at) VALUES
                        (1, 2, 'solved', '2026-12-31 12:00:00'), (1, 4, 'failed', '2027-01-01 12:00:00')""")
    weeks = progress.training_weeks(conn)
    assert [(w["week"], w["attempts"], w["days"]) for w in weeks] == [("2026-W53", 2, 2)]
