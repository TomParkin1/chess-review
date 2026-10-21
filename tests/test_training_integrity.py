"""Finish M2, training integrity: one box change per card per day (B3), the time budget and the
load it keeps (F4), the streak (B9), the 04:00 training day (B13) and sessions that never block
Today (B5). B12 (double clicks) is front-end only: see the browser scenario."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from chessreview import coach, convert, db, progress, session, simulate, srs
from chessreview.config import load_config
from chessreview.pgn_import import import_paths, import_pgn_text

DAY = date(2026, 10, 21)
SAMPLE = Path(__file__).parent / "data" / "sample.pgn"
WEAKNESS = {"title": "Hanging pieces", "detail": "your blunders", "train": {"kind": "hanging_piece"}}
FEN = "7r/8/8/8/7N/8/8/K6k w - - 0 1"


@pytest.fixture
def cfg():
    """10 minutes at the default 60 s a puzzle: 10 puzzle slots."""
    base = load_config()
    return replace(base, coach=replace(base.coach, budget_minutes=10, seconds_per_puzzle=60, calm_ratio=0.3),
                   training=replace(base.training, new_per_day=10))


def fill(c, games: int = 30, blunders: int = 6, calm: int = 4, done: bool = True) -> None:
    """`games` analysed games, each with `blunders` hanging-piece blunders of mine (all different
    sizes) and `calm` good moves, half of them against a real threat."""
    batch = c.execute("INSERT INTO imports (source, games_added, session_done_at) VALUES ('upload', ?, ?)",
                      (games, "2026-01-01" if done else None)).lastrowid
    for g in range(1, games + 1):
        c.execute("""INSERT INTO games (id, dedupe_key, white, black, white_elo, black_elo, my_color, my_result,
                         date, moves_uci, num_plies, pgn, batch_id, analysed_at, end_time, time_class, rated)
                     VALUES (?, ?, 'me', 'opp', 1000, 1000, 'white', 'loss', '2026-10-01', 'e2e4', 1, '', ?,
                             '2026-10-01', ?, 'rapid', 1)""", (g, f"k{g}", batch, f"2026-10-01 12:{g:02d}:00"))
        for i in range(blunders):
            c.execute("""INSERT INTO mistakes (game_id, ply, judgement, win_drop, phase, kind, is_mine)
                         VALUES (?, ?, 'blunder', ?, 'middlegame', 'hanging_piece', 1)""",
                      (g, 2 + 2 * i, 0.3 + 0.001 * (g * blunders + i)))
        for j in range(calm):
            ply = 2 + 2 * (blunders + j)
            c.execute("""INSERT INTO mistakes (game_id, ply, judgement, win_drop, phase, is_mine, mark)
                         VALUES (?, ?, 'good', 0, 'middlegame', 1, 'good')""", (g, ply))
            c.execute("""INSERT INTO positions (game_id, ply, fen, score_cp, threat_cp, threat_uci, threat_pv)
                         VALUES (?, ?, ?, 0, ?, 'h8h4', 'h8h4')""", (g, ply, FEN, -600 if j % 2 else 0))
        for ply in range(1, 2 * (blunders + calm) + 3):
            c.execute("INSERT OR IGNORE INTO positions (game_id, ply, fen, score_cp) VALUES (?, ?, ?, 0)", (g, ply, FEN))
    c.commit()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(coach.insights, "compute", lambda *a, **k: {"weaknesses": [WEAKNESS]})
    c = db.connect(tmp_path / "t.db")
    fill(c)
    yield c
    c.close()


def card(c, game_id=1, ply=2):
    return c.execute("SELECT * FROM cards WHERE game_id = ? AND ply = ?", (game_id, ply)).fetchone()


def due_on(row) -> int:
    return (date.fromisoformat(row["due_on"]) - DAY).days


# ------------------------------------------------------------ B3: one box change per card per day

def test_solve_and_retry_four_times_promotes_once(conn, cfg):
    """B3 repro: Practice, solve, Retry, solve again, four times: box 1 -> 2 -> 3 -> 4, due in 14 days."""
    srs.ensure_cards(conn, cfg)
    for _ in range(4):
        srs.record(conn, 1, 2, "solved", DAY)
    c = card(conn)
    assert c["box"] == 1 and due_on(c) == 1 and c["reps"] == 4


def test_again_after_a_failure_does_not_promote(conn, cfg):
    srs.ensure_cards(conn, cfg)
    srs.record(conn, 1, 2, "failed", DAY)
    srs.record(conn, 1, 2, "solved", DAY)          # Today's "Again" at the end of the block
    c = card(conn)
    assert c["box"] == 0 and due_on(c) == 1 and c["last_result"] == "solved"


def test_only_a_failure_counts_before_a_card_is_due(conn, cfg):
    srs.ensure_cards(conn, cfg)
    srs.record(conn, 1, 2, "solved", DAY)                          # box 1, due tomorrow
    srs.record(conn, 1, 2, "solved", DAY + timedelta(days=1))      # due: box 2, due in 3 days
    srs.record(conn, 1, 2, "solved", DAY + timedelta(days=2))      # early (Practice): no change
    assert card(conn)["box"] == 2 and card(conn)["due_on"] == (DAY + timedelta(days=4)).isoformat()
    srs.record(conn, 1, 2, "skipped", DAY + timedelta(days=3))     # skipping early isn't failing
    assert card(conn)["box"] == 2
    srs.record(conn, 1, 2, "failed", DAY + timedelta(days=3))      # already practised that day
    assert card(conn)["box"] == 2
    srs.record(conn, 1, 2, "failed", DAY + timedelta(days=4))
    assert card(conn)["box"] == 0


def test_a_line_drilled_six_times_moves_once(conn):
    """B3 repro: "Learn my lines" recorded the shared first move once per line, six per drill."""
    for _ in range(6):
        srs.record_line(conn, "white:start", "solved", DAY)
    c = conn.execute("SELECT * FROM cards WHERE kind = 'line'").fetchone()
    assert c["box"] == 1 and c["mastered_on"] is None
    assert conn.execute("SELECT COUNT(*) FROM training_log WHERE kind = 'line'").fetchone()[0] == 6


def test_an_endgame_solved_on_retry_still_unlocks_the_next_level(conn):
    srs.record_key(conn, "endgame", "kp_front", "failed", DAY)
    srs.record_key(conn, "endgame", "kp_front", "solved", DAY)
    row = conn.execute("SELECT * FROM cards WHERE kind = 'endgame'").fetchone()
    assert row["box"] == 0 and convert._status(row) == "solved"


def test_migration_puts_back_cards_promoted_too_fast(conn):
    """B3 evidence: the cards for 1.e4 and 1.e4 e5 were mastered the day they were created."""
    conn.executemany("INSERT INTO cards (kind, key, box, introduced_on, last_on, mastered_on, due_on) VALUES (?, ?, ?, ?, ?, ?, ?)", [
        ("line", "white:start", 6, "2026-10-16", None, "2026-10-16", None),        # mastered on day 0
        ("line", "white:e4e5", 3, "2026-10-16", "2026-10-19", None, "2026-10-26"),  # box 3 needs 4 days
        ("line", "white:ok", 2, "2026-10-16", "2026-10-19", None, "2026-10-22"),    # possible: unchanged
    ])
    conn.execute("DROP TABLE training_log")
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    db.migrate(conn)
    rows = {r["key"]: r for r in conn.execute("SELECT * FROM cards WHERE kind = 'line'")}
    assert (rows["white:start"]["box"], rows["white:start"]["mastered_on"], rows["white:start"]["due_on"]) == (1, None, "2026-10-17")
    assert (rows["white:e4e5"]["box"], rows["white:e4e5"]["due_on"]) == (2, "2026-10-22")
    assert (rows["white:ok"]["box"], rows["white:ok"]["due_on"]) == (2, "2026-10-22")


# ------------------------------------------------------------ F4: the time budget

def blocks(plan):
    return {b["id"]: b for b in plan["blocks"]}


def planned_minutes(plan) -> float:
    return simulate._planned_minutes(plan)


@pytest.mark.parametrize("minutes", [10, 20, 30])
def test_the_plan_fits_the_budget(conn, cfg, minutes):
    coach.set_budget(conn, minutes)
    plan = coach.today_plan(conn, cfg, DAY)
    assert plan["budget"] == minutes and plan["pace"] == 60
    assert plan["total"] == minutes          # 60 s a puzzle, nothing due yet: one puzzle a minute
    assert planned_minutes(plan) <= minutes and plan["minutes_left"] == minutes


def test_a_budget_outside_the_choices_is_refused(conn):
    with pytest.raises(ValueError):
        coach.set_budget(conn, 15)


def test_reviews_fill_the_day_when_more_is_due_than_it_holds(conn, cfg):
    srs.ensure_cards(conn, cfg)
    conn.execute("UPDATE cards SET box = 1, due_on = ?, introduced_on = '2026-10-01' WHERE game_id <= 3",
                 (DAY.isoformat(),))
    plan = coach.today_plan(conn, cfg, DAY)
    b = blocks(plan)
    assert len(b["reviews"]["items"]) == 10 and set(b) == {"reviews"}     # 18 due, 10 places: no new cards
    assert "8 more are waiting" in b["reviews"]["subtitle"]


def test_new_cards_wait_until_the_reviews_leave_room(conn, cfg):
    srs.ensure_cards(conn, cfg)
    conn.execute("UPDATE cards SET box = 1, due_on = ?, introduced_on = '2026-10-01' WHERE game_id = 1 AND ply <= 12",
                 (DAY.isoformat(),))       # 6 due: room for 7 - 6 = 1 new card (new share 0.7 of 10)
    plan = coach.today_plan(conn, cfg, DAY)
    tags = [i[2] for b in plan["blocks"] for i in b["items"]]
    assert tags.count("due") == 6 and tags.count("new") == 1
    conn.execute("UPDATE cards SET box = 1, due_on = ? WHERE game_id = 2 AND ply = 2", (DAY.isoformat(),))
    tags = [i[2] for b in coach.today_plan(conn, cfg, DAY, refresh=True)["blocks"] for i in b["items"]]
    assert tags.count("due") == 7 and tags.count("new") == 0                    # 7 due: no room left
    focus = blocks(coach.today_plan(conn, cfg, DAY)).get("focus", {"items": []})["items"]
    assert all(card(conn, g, p)["due_on"] for g, p, _ in focus if g != "lib")  # no unseen ones there either


def test_reviews_come_most_overdue_first_relative_to_their_interval(conn, cfg):
    srs.ensure_cards(conn, cfg)
    conn.execute("UPDATE cards SET box = 5, due_on = ? WHERE game_id = 1 AND ply = 2", ((DAY - timedelta(days=3)).isoformat(),))
    conn.execute("UPDATE cards SET box = 1, due_on = ? WHERE game_id = 2 AND ply = 2", ((DAY - timedelta(days=1)).isoformat(),))
    conn.execute("UPDATE cards SET box = 6, due_on = ? WHERE game_id = 3 AND ply = 2", ((DAY - timedelta(days=70)).isoformat(),))
    # Days late (+1) over the interval: 2/1 for the box-1 card, 71/60 for box 6, 4/30 for box 5.
    assert srs.due_cards(conn, cfg, DAY) == [(2, 2), (3, 2), (1, 2)]


def test_pace_is_my_time_per_puzzle_with_retries(conn, cfg):
    assert coach.pace(conn, cfg) == 60                                       # no history: the setting
    start = datetime(2026, 10, 20, 12)
    for i in range(40):                                                      # 40 attempts 30 s apart,
        key = (1 + i // 2, 2)                                                # each puzzle tried twice
        conn.execute("INSERT INTO review_results (game_id, ply, result, at) VALUES (?, ?, 'solved', ?)",
                     (*key, (start + timedelta(seconds=30 * i)).strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    assert coach.attempt_seconds(conn, cfg) == 30 and coach.pace(conn, cfg) == 60


def test_a_new_budget_rebuilds_the_plan_and_keeps_what_was_done(conn, cfg):
    plan = coach.today_plan(conn, cfg, srs.today())
    done = [i for b in plan["blocks"] for i in b["items"]][:4]
    for g, p, _ in done:
        conn.execute("INSERT INTO review_results (game_id, ply, result) VALUES (?, ?, 'solved')", (g, p))
    conn.commit()
    coach.set_budget(conn, 30)
    bigger = coach.today_plan(conn, cfg, srs.today(), refresh=True)
    assert bigger["total"] == 30 and bigger["finished"] == 4
    coach.set_budget(conn, 10)
    smaller = coach.today_plan(conn, cfg, srs.today(), refresh=True)
    assert smaller["total"] == 10 and smaller["finished"] == 4


def test_ninety_days_stay_inside_the_budget(conn, cfg):
    """F4 "done when": a simulated 90-day run keeps the daily load inside the chosen budget. With
    the old fixed sizes (15 reviews + 6 real-game + 5 focus a day) the reviews piled up for ever."""
    result = simulate.run(conn, cfg, days=90, budget=10, skip=0.15, seed=3, start=DAY,
                          my_rates=simulate.DEFAULT_RATES)
    played = [d for d in result.days if not d.skipped]
    assert len(played) > 60
    assert all(d.planned_min <= 10 for d in played)
    assert sum(d.spent_min for d in played) / len(played) <= 10.5       # retries included, on average
    late = result.days[30:]
    assert max(d.backlog for d in late) <= 30                          # at most three days of reviews wait
    assert result.days[-1].backlog <= 10
    assert sum(d.new for d in late) >= 30                              # and learning goes on


def test_repeated_solves_cannot_master_a_card_early(conn, cfg):
    """M2 "done when": repeated solves can't master a card. Solving everything, retries included,
    no card is mastered before it has passed every interval (1+3+7+14+30+60 = 115 days)."""
    perfect = {k: (1.0, 0.0) for k in simulate.DEFAULT_RATES}
    simulate.run(conn, cfg, days=130, budget=10, skip=0, start=DAY, my_rates=perfect)
    rows = conn.execute("SELECT introduced_on, mastered_on FROM cards WHERE mastered_on IS NOT NULL").fetchall()
    assert rows
    assert all((date.fromisoformat(r["mastered_on"]) - date.fromisoformat(r["introduced_on"])).days >= 115 for r in rows)


# ------------------------------------------------------------ the opening block

def test_the_opening_block_is_done_when_its_lines_are_done(conn, cfg, monkeypatch):
    keys = [f"white:{i}" for i in range(8)]
    for k in keys:
        conn.execute("INSERT INTO cards (kind, key, box, due_on) VALUES ('line', ?, 1, ?)", (k, DAY.isoformat()))
    conn.commit()
    monkeypatch.setattr(coach, "_due_line_keys", lambda c, on: keys)
    plan = coach.today_plan(conn, cfg, DAY)
    opening = blocks(plan)["opening"]
    assert opening["lines_due"] == 6 and opening["lines_left"] == 6          # 15% of 10 minutes
    assert plan["total"] - plan["finished"] == 8 + 1                         # 90 s of lines leave 8 puzzles
    srs.record_line(conn, opening["keys"][0], "solved", DAY)
    assert not blocks(coach.today_plan(conn, cfg, DAY))["opening"]["done"]   # one drilled isn't enough
    for k in opening["keys"][1:]:
        srs.record_line(conn, k, "failed", DAY)                             # failed counts as drilled
    opening = blocks(coach.today_plan(conn, cfg, DAY))["opening"]
    assert opening["done"] and opening["lines_left"] == 0


# ------------------------------------------------------------ B9: the streak counts all training

def local_noon(day: date) -> str:
    return datetime.combine(day, datetime.min.time().replace(hour=12)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def test_the_streak_counts_every_kind_of_training(conn):
    """B9 repro: a day of library puzzles, opening drills or Convert only reset the streak."""
    conn.execute("INSERT INTO puzzle_attempts (puzzle_id, result, at) VALUES ('p1', 'solved', ?)", (local_noon(DAY - timedelta(days=3)),))
    conn.execute("INSERT INTO training_log (kind, key, result, at) VALUES ('line', 'white:x', 'solved', ?)", (local_noon(DAY - timedelta(days=2)),))
    conn.execute("INSERT INTO training_log (kind, key, result, at) VALUES ('convert', '1:30', 'failed', ?)", (local_noon(DAY - timedelta(days=1)),))
    conn.execute("INSERT INTO self_reviews (game_id, picked_ply, actual_ply, hit, at) VALUES (1, 2, 2, 1, ?)", (local_noon(DAY),))
    conn.commit()
    assert srs.streak(conn, DAY) == 4
    weeks = progress.training_weeks(conn)
    assert sum(w["days"] for w in weeks) == 4 and sum(w["attempts"] for w in weeks) == 0


# ------------------------------------------------------------ B13: the training day starts at 04:00

def test_the_training_day_runs_from_four_to_four(conn, cfg):
    assert srs.today(datetime(2026, 10, 21, 23, 55)) == DAY
    assert srs.today(datetime(2026, 10, 22, 0, 30)) == DAY            # still the evening's workout
    assert srs.today(datetime(2026, 10, 22, 4, 0)) == DAY + timedelta(days=1)
    late = datetime(2026, 10, 22, 0, 30).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("INSERT INTO review_results (game_id, ply, result, at) VALUES (1, 2, 'solved', ?)", (late,))
    conn.commit()
    assert (1, 2) in coach.attempted_today(conn, DAY)                  # counts for the plan it belongs to
    assert srs.streak(conn, DAY) == 1


def test_a_card_solved_before_midnight_is_due_next_training_day(conn, cfg):
    """B13 repro: a card solved at 23:55 was due again at 00:00, and the plan turned into a new day."""
    srs.ensure_cards(conn, cfg)
    srs.record(conn, 1, 2, "solved", srs.today(datetime(2026, 10, 21, 23, 55)))
    after_midnight = srs.today(datetime(2026, 10, 22, 0, 10))
    assert (1, 2) not in srs.due_cards(conn, cfg, after_midnight)
    assert (1, 2) in srs.due_cards(conn, cfg, srs.today(datetime(2026, 10, 22, 4, 10)))


# ------------------------------------------------------------ B5: sessions never block Today

RECENT_PGN = """[Event "Live Chess"]
[Site "Chess.com"]
[Date "{date}"]
[White "sampleplayer"]
[Black "opponent9"]
[Result "1-0"]

1. e4 e5 2. Nf3 Nc6 3. Bc4 Bc5 4. c3 Nf6 5. d4 exd4 6. cxd4 Bb4+ 1-0
"""


def test_the_first_import_and_an_import_of_old_games_are_history(tmp_path, cfg):
    """B5 repro: uploading my history as PGN became an 831-puzzle session that starved Today."""
    c = db.connect(tmp_path / "h.db")
    import_paths(c, [SAMPLE], cfg)                                    # the first import
    first = c.execute("SELECT * FROM imports").fetchone()
    assert first["session_done_at"] and first["label"].endswith("(history)")
    c.execute("DELETE FROM games")
    import_paths(c, [SAMPLE], cfg)                                    # games from March 2025
    assert not session.status(c, cfg)["open"]
    batch = db.create_batch(c, "upload", "Upload of 1 file(s)")       # a game played today
    import_pgn_text(c, RECENT_PGN.format(date=date.today().strftime("%Y.%m.%d")), cfg, batch_id=batch)
    session.finish_import(c, cfg, batch)
    assert session.status(c, cfg)["open"]
    c.close()


def test_an_unfinished_session_joins_daily_training_after_three_days(tmp_path, cfg):
    c = db.connect(tmp_path / "s.db")
    fill(c, games=2, done=False)
    assert session.status(c, cfg)["open"]
    assert srs.new_cards(c, cfg, 5) == []                             # its mistakes wait for it...
    c.execute("UPDATE imports SET at = datetime('now', '-4 days')")
    c.commit()
    assert not session.status(c, cfg)["open"]
    assert len(srs.new_cards(c, cfg, 5)) == 5                         # ...for three days at most
    c.close()


def test_calm_positions_come_from_open_session_games_too(tmp_path, cfg):
    c = db.connect(tmp_path / "c.db")
    fill(c, games=2, done=False)
    assert session.status(c, cfg)["open"]
    assert len(srs.calm_positions(c, cfg, 4, "seed")) == 4
    c.close()
