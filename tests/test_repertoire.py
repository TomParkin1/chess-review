"""Repertoire tree, problem detection, drill lines and opening-line SRS cards."""

from datetime import date

import chess
import pytest

from chessreview import db, repertoire, srs

E4_E5 = "e2e4 e7e5 g1f3 b8c6 f1c4"  # Italian: my usual Bc4
E4_C5 = "e2e4 c7c5 g1f3 d7d6 b1c3"  # Sicilian vs me: my usual Nc3 (judged bad below)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "r.db")
    games = [
        (E4_E5, "win"),
        (E4_E5, "win"),
        (E4_E5, "loss"),
        (E4_C5, "loss"),
        (E4_C5, "loss"),
        ("d2d4 d7d5 c2c4", "draw"),
    ]
    for i, (moves, result) in enumerate(games, start=1):
        c.execute(
            """INSERT INTO games (id, dedupe_key, white, black, my_color, my_result, date, moves_uci,
                         num_plies, pgn, opening_family) VALUES (?, ?, 'me', 'o', 'white', ?, '2026-01-01', ?, ?, '',
                         ?)""",
            (i, f"k{i}", result, moves, len(moves.split()), "Sicilian Defense" if moves == E4_C5 else "Other"),
        )
        board = chess.Board()
        for ply, uci in enumerate(moves.split()):
            # Engine: best move is always the move played, except in the Sicilian at ply 4 (Nc3 -> d4).
            best = "d2d4" if (moves == E4_C5 and ply == 4) else uci
            c.execute(
                "INSERT INTO positions (game_id, ply, fen, move_uci, best_uci) VALUES (?, ?, ?, ?, ?)",
                (i, ply, board.fen(), uci, best),
            )
            judgement = "mistake" if (moves == E4_C5 and ply == 4) else "good"
            c.execute(
                """INSERT INTO mistakes (game_id, ply, judgement, win_drop, is_mine)
                         VALUES (?, ?, ?, ?, ?)""",
                (i, ply, judgement, 0.2 if judgement != "good" else 0.0, int(ply % 2 == 0)),
            )
            board.push_uci(uci)
    c.commit()
    yield c
    c.close()


def test_tree_counts_and_repertoire(conn):
    tree = repertoire.build(conn, "white")
    root = repertoire.node_json(tree, repertoire.root_epd())
    assert root["games"] == 6 and root["my_turn"]
    assert [(m["san"], m["games"]) for m in root["moves"]] == [("e4", 5), ("d4", 1)]
    assert root["learn_san"] == "e4" and not root["problem"]


def test_bad_usual_move_is_a_problem_with_engine_fix(conn):
    tree = repertoire.build(conn, "white")
    probs = repertoire.problems(tree)
    assert len(probs) == 1
    p = probs[0]
    assert p["usual_san"] == "Nc3" and p["learn_san"] == "d4" and p["games"] == 2
    assert p["line"].startswith("1. e4 c5 2. Nf3 d6")


def test_family_filter(conn):
    tree = repertoire.build(conn, "white", "Sicilian Defense")
    root = repertoire.node_json(tree, repertoire.root_epd())
    assert root["games"] == 2


def test_drill_lines_follow_repertoire(conn):
    tree = repertoire.build(conn, "white")
    lines = repertoire.drill_lines(tree, "white", count=5, seed=3)
    assert lines
    for line in lines:
        assert line[0] == {**line[0], "uci": "e2e4", "mine": True}
        mine = [s for s in line if s["mine"]]
        assert all(s["key"].startswith("white:") for s in mine)
        if line[1]["uci"] == "c7c5":
            assert line[-1]["uci"] == "d2d4" and line[-1]["fix"]  # the improvement, not Nc3


def test_line_cards_schedule(conn):
    key = "white:" + repertoire.root_epd()
    srs.record_line(conn, key, "failed", date(2026, 10, 16))
    assert srs.due_lines(conn, "white", date(2026, 10, 17)) == [key]
    assert srs.due_lines(conn, "black", date(2026, 10, 17)) == []
    srs.record_line(conn, key, "solved", date(2026, 10, 17))
    assert srs.due_lines(conn, "white", date(2026, 10, 17)) == []
    assert srs.line_stats(conn, "white", date(2026, 10, 17))["seen"] == 1


def test_due_exercise_replays_the_path(conn):
    tree = repertoire.build(conn, "white")
    problem = repertoire.problems(tree)[0]
    key = repertoire.card_key("white", problem["epd"])
    [steps] = repertoire.due_exercises(tree, "white", [key])
    assert [s["uci"] for s in steps[:-1]] == problem["path"] and all(s["setup"] for s in steps[:-1])
    assert steps[-1]["mine"] and steps[-1]["uci"] == "d2d4"


def test_tree_is_cached_until_the_games_or_verdicts_change(conn):
    """F17: the explorer reused to rebuild the whole tree on every click."""
    tree = repertoire.build(conn, "white")
    assert repertoire.build(conn, "white") is tree  # nothing changed: same tree
    assert repertoire.build(conn, "white", "Sicilian Defense") is not tree
    # A reclassified game gets fresh mistakes rows (classify_game deletes and inserts).
    with conn:
        rows = conn.execute("SELECT * FROM mistakes WHERE game_id = 4").fetchall()
        conn.execute("DELETE FROM mistakes WHERE game_id = 4")
        conn.executemany(
            "INSERT INTO mistakes (game_id, ply, judgement, win_drop, is_mine) VALUES (?, ?, ?, ?, ?)",
            [(r["game_id"], r["ply"], "good", 0.0, r["is_mine"]) for r in rows],
        )
    fresh = repertoire.build(conn, "white")
    assert fresh is not tree
    assert not repertoire.problems(fresh)  # Nc3 is no longer judged bad
    # A new game, and deleting everything, change it too.
    with conn:
        conn.execute("""INSERT INTO games (id, dedupe_key, white, black, my_color, my_result, date, moves_uci,
                            num_plies, pgn) VALUES (7, 'k7', 'me', 'o', 'white', 'win', '2026-01-02', 'g1f3', 1, '')""")
    assert repertoire.build(conn, "white") is not fresh
    db.delete_all_games(conn)
    assert repertoire.node_json(repertoire.build(conn, "white"), repertoire.root_epd()) is None
