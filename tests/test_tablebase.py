"""E3: the Lichess tablebase for endgame play (perfect defence, exact verdicts, cached, offline-safe).

No test touches the network (conftest.py): the API is replaced by `FakeTablebase`, which answers
for the rook mate (essential endgame kr_k) the way the API does: categories from the side to move,
each move's category from the opponent's side after it.
"""
import json
import shutil
import urllib.error
from dataclasses import replace

import chess
import pytest

from chessreview import convert, db, tablebase
from chessreview.config import load_config
from chessreview.web import create_app

KEY = "endgame/kr_k"
START = chess.Board(convert.task_for(None, KEY).fen)
BLUNDER = "a1d1"            # in this fake, the one move that lets the win go: a draw


def after(board: chess.Board, *ucis: str) -> chess.Board:
    b = board.copy()
    for u in ucis:
        b.push_uci(u)
    return b


class FakeTablebase:
    def __init__(self):
        self.calls = 0
        self.drawn = after(START, BLUNDER).epd()

    def __call__(self, url: str) -> dict:
        self.calls += 1
        fen = url.split("fen=", 1)[1]
        board = chess.Board(fen.replace("+", " ").replace("%2F", "/").replace("%20", " "))
        moves = []
        for i, m in enumerate(board.legal_moves):
            entry = {"uci": m.uci(), "san": board.san(m), "dtz": None}
            if board.epd() == self.drawn:
                entry |= {"category": "draw", "dtm": 0}
            elif board.turn == chess.WHITE:
                entry |= {"category": "draw", "dtm": 0} if m.uci() == BLUNDER and board.epd() == START.epd() \
                    else {"category": "loss", "dtm": -(20 + i)}
            else:
                entry |= {"category": "win", "dtm": 10 + i}          # the longest defence: the last move
            moves.append(entry)
        category = "draw" if board.epd() == self.drawn else "win" if board.turn == chess.WHITE else "loss"
        return {"category": category, "dtm": None, "dtz": None, "moves": moves}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "tb.db")
    yield c
    c.close()


@pytest.fixture
def fake(monkeypatch):
    f = FakeTablebase()
    monkeypatch.setattr(tablebase, "fetch", f)
    return f


# ------------------------------------------------------------ probing and caching

def test_probe_reads_the_categories_from_the_side_to_move(conn, fake):
    tb = tablebase.probe(conn, START)
    assert tb.wdl == 2 and tb.best.wdl == 2
    assert tb.best.uci != BLUNDER and tb.best.dtm == -20          # the quickest win
    black = tablebase.probe(conn, after(START, "a1a4"))
    assert black.wdl == -2 and black.best.uci == list(after(START, "a1a4").legal_moves)[-1].uci()   # longest defence


def test_answers_are_cached_in_memory_and_in_the_database(conn, fake):
    tablebase.probe(conn, START)
    tablebase.probe(conn, START)
    assert fake.calls == 1
    tablebase.clear_cache()                                       # e.g. a restart: SQLite still has it
    assert tablebase.probe(conn, START).wdl == 2 and fake.calls == 1
    stored = json.loads(conn.execute("SELECT data FROM tablebase_cache").fetchone()["data"])
    assert set(stored) == {"category", "dtm", "dtz", "moves"}     # only what we use


def test_the_cache_key_has_the_halfmove_clock(conn, fake):
    """The API's answer depends on it: the same position is a "cursed" win near the 50-move limit."""
    tablebase.probe(conn, START)
    late = START.copy()
    late.halfmove_clock = 90
    tablebase.probe(conn, late)
    assert fake.calls == 2


def test_only_small_positions_without_castling_rights_are_asked(conn, fake):
    assert tablebase.probe(conn, chess.Board()) is None
    assert tablebase.probe(conn, chess.Board("r3k3/8/8/8/8/8/8/4K3 b q - 0 1")) is None   # castling rights
    assert tablebase.probe(conn, chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")) is None   # stalemate: game over
    assert fake.calls == 0


def test_switched_off_it_asks_nothing(conn, fake):
    tablebase.set_enabled(conn, False)
    assert tablebase.probe(conn, START) is None and fake.calls == 0
    tablebase.set_enabled(conn, True)
    assert tablebase.probe(conn, START) is not None


def test_failures_fall_back_and_back_off(conn, monkeypatch):
    calls = []

    def limited(url):
        calls.append(url)
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
    monkeypatch.setattr(tablebase, "fetch", limited)
    assert tablebase.probe(conn, START) is None
    assert tablebase.probe(conn, after(START, "a1a4")) is None      # backing off: not asked again
    assert len(calls) == 1
    clock = [0.0]
    monkeypatch.setattr(tablebase.time, "monotonic", lambda: clock[0])
    tablebase.clear_cache()
    assert tablebase.probe(conn, START) is None and len(calls) == 2
    clock[0] = tablebase.RATE_LIMIT_BACKOFF - 1
    assert tablebase.probe(conn, START) is None and len(calls) == 2  # still within the minute
    clock[0] = tablebase.RATE_LIMIT_BACKOFF + 1
    monkeypatch.setattr(tablebase, "fetch", FakeTablebase())
    assert tablebase.probe(conn, START).wdl == 2                     # asked again after it


def test_offline_and_unknown_answers_give_none(conn, monkeypatch):
    def unknown(url):
        return {"category": "unknown", "moves": []}
    monkeypatch.setattr(tablebase, "fetch", unknown)
    assert tablebase.probe(conn, START) is None
    assert conn.execute("SELECT COUNT(*) FROM tablebase_cache").fetchone()[0] == 0   # nothing cached
    tablebase.clear_cache()
    assert tablebase.probe(conn, START) is None                      # conftest: "offline"


def test_in_a_draw_the_defender_keeps_the_engines_move():
    data = {"category": "draw", "moves": [{"uci": "a1a2", "san": "Ra2", "category": "draw"},
                                          {"uci": "a1a3", "san": "Ra3", "category": "draw"},
                                          {"uci": "a1a4", "san": "Ra4", "category": "win"}]}
    tb = tablebase._parse(data)
    assert tablebase.choose(tb, chess.Move.from_uci("a1a3")).uci == "a1a3"    # also a draw: the engine's
    assert tablebase.choose(tb, chess.Move.from_uci("a1a4")).uci == "a1a2"    # it loses: perfect play
    losing = tablebase._parse({"category": "loss", "moves": [{"uci": "a1a2", "san": "Ra2", "category": "win", "dtm": 3},
                                                             {"uci": "a1a3", "san": "Ra3", "category": "win", "dtm": 9}]})
    assert tablebase.choose(losing, chess.Move.from_uci("a1a2")).uci == "a1a3"  # lost: the longest defence


def test_mate_in_one_is_the_quickest_win():
    """Playout finding: the API gives the mating move no DTM (null, checkmate: true), which ranked
    it below every quiet move, and K+R v K went round in circles to a threefold repetition."""
    data = {"category": "win", "dtm": 1, "dtz": 1, "moves": [
        {"uci": "b3a3", "san": "Ra3#", "category": "loss", "dtm": None, "dtz": -1, "checkmate": True},
        {"uci": "b3b4", "san": "Rb4", "category": "loss", "dtm": -2, "dtz": -2}]}
    assert tablebase._parse(data).best.san == "Ra3#"
    assert tablebase._parse(json.loads(json.dumps(tablebase._compact(data)))).best.san == "Ra3#"   # cached too
    no_dtm = {"category": "win", "moves": [{"uci": "a1a2", "san": "Ra2", "category": "loss", "dtz": -9},
                                          {"uci": "a1a3", "san": "Ra3", "category": "loss", "dtz": -3}]}
    assert tablebase._parse(no_dtm).best.san == "Ra3"                 # 6-7 pieces: DTZ decides


def test_cursed_wins_count_as_almost_draws():
    data = {"category": "cursed-win", "moves": [{"uci": "a1a2", "san": "Ra2", "category": "blessed-loss"},
                                               {"uci": "a1a3", "san": "Ra3", "category": "draw"}]}
    tb = tablebase._parse(data)
    assert tb.wdl == 1 and tb.best.uci == "a1a2" and tb.best.wdl == 1


# ------------------------------------------------------------ exact verdicts (convert.state)

def task(key=KEY):
    return convert.task_for(None, key)


def test_a_win_thrown_away_fails_with_the_exact_reason():
    from chessreview.classify import Eval
    t = task()
    board = after(START, BLUNDER, "e5e4")
    st = convert.state(t, board, Eval(900, None), 0, 1000, tb=(2, 2, 0))
    assert st.status == "failed" and "throws away the win" in st.reason and "draw" in st.reason
    assert "50-move rule" in convert.state(t, board, Eval(900, None), 0, 1000, tb=(2, 2, 1)).reason
    assert "lost" in convert.state(t, board, Eval(900, None), 0, 1000, tb=(2, 2, -2)).reason
    assert convert.state(t, board, Eval(900, None), 0, 1000, tb=(2, 2, 2)).status == "playing"


def test_a_move_is_blamed_only_when_the_position_before_it_is_known():
    """A won position from a game (8+ pieces) entering the tablebase already drawn: the win went
    earlier (or the start was never a tablebase win), so this move isn't "the one"."""
    from chessreview.classify import Eval
    convert_task = replace(task(), kind="convert", finish="resign", resign_cp=900)
    board = after(START, "a1a4", "e5d5")
    st = convert.state(convert_task, board, Eval(900, None), 0, 1000, tb=(None, None, 0))
    assert st.status == "failed" and st.reason.startswith("The win is gone") and "throws" not in st.reason
    assert "throws away" in convert.state(convert_task, board, Eval(900, None), 0, 1000, tb=(None, 2, 0)).reason


def test_the_tablebase_replaces_the_eval_thresholds():
    """A convert position the tablebase still calls won doesn't fail on a low Stockfish eval, and a
    draw goal fails only when the tablebase says lost."""
    from chessreview.classify import Eval
    convert_task = replace(task(), kind="convert", finish="resign", goal="win", resign_cp=900)
    board = after(START, "a1a4", "e5d5")
    assert convert.state(convert_task, board, Eval(50, None), 0, 1000).status == "failed"        # eval rule
    assert convert.state(convert_task, board, Eval(50, None), 0, 1000, tb=(2, 2, 2)).status == "playing"
    hold = replace(task(), goal="draw", finish="hold")
    assert convert.state(hold, board, Eval(-900, None), 0, 1000).status == "failed"
    assert convert.state(hold, board, Eval(-900, None), 0, 1000, tb=(0, 0, 0)).status == "playing"
    assert "lost now" in convert.state(hold, board, Eval(-900, None), 0, 1000, tb=(0, 0, -2)).reason


def test_a_start_the_tablebase_calls_drawn_is_judged_by_the_eval():
    """A won position from a game that is really a tablebase draw: reaching a draw isn't "throwing
    the win away" (it was never there); the eval rules decide."""
    from chessreview.classify import Eval
    board = after(START, "a1a4", "e5d5")
    assert convert.state(task(), board, Eval(900, None), 0, 1000, tb=(0, 0, 0)).status == "playing"
    assert convert.state(task(), board, Eval(50, None), 0, 1000, tb=(0, 0, 0)).status == "playing"   # endgame: no slip rule
    conv = replace(task(), kind="convert", finish="resign", resign_cp=900)
    assert "slipped" in convert.state(conv, board, Eval(50, None), 0, 1000, tb=(0, 0, 0)).reason      # the eval rule


# ------------------------------------------------------------ the play API

@pytest.fixture
def client(tmp_path):
    base = load_config()
    if not shutil.which(base.engine.path):
        pytest.skip("Stockfish not installed")
    app = create_app(replace(base, db_path=tmp_path / "web.db", review=replace(base.review, live_time=0.05)))
    app.testing = True
    yield app.test_client()
    app.extensions["live"].close()


def test_stockfish_defends_with_the_tablebase_move(client, fake):
    r = client.post("/api/play/move", json={"key": KEY, "moves": [], "uci": "a1a4", "streak": 0}).get_json()
    longest = list(after(START, "a1a4").legal_moves)[-1].uci()
    assert r["reply_source"] == "tablebase" and r["reply_uci"] == longest
    assert r["status"] == "playing"
    assert r["tablebase"] == {"verdict": "win", "wdl": 2, "before_verdict": "win", "before_wdl": 2}


def test_the_move_that_throws_the_win_away_fails_at_once(client, fake):
    r = client.post("/api/play/move", json={"key": KEY, "moves": [], "uci": BLUNDER, "streak": 0}).get_json()
    assert r["status"] == "failed" and "throws away the win" in r["reason"] and "(tablebase)" in r["reason"]


def test_hints_are_perfect_play(client, fake):
    h = client.post("/api/play/hint", json={"key": KEY, "moves": []}).get_json()
    quickest = next(m.uci() for m in START.legal_moves if m.uci() != BLUNDER)   # the fake's shortest mate
    assert h["source"] == "tablebase" and h["uci"] == quickest


def test_offline_falls_back_to_stockfish(client):
    r = client.post("/api/play/move", json={"key": KEY, "moves": [], "uci": "a1a4", "streak": 0}).get_json()
    assert r["reply_source"] == "stockfish" and "tablebase" not in r and r["status"] == "playing"
    h = client.post("/api/play/hint", json={"key": KEY, "moves": []}).get_json()
    assert h["source"] == "stockfish" and h["uci"]


def test_the_setting(client, fake):
    assert client.get("/api/settings/tablebase").get_json() == {"enabled": True, "cached": 0, "max_pieces": 7}
    assert client.post("/api/settings/tablebase", json={"enabled": "no"}).status_code == 400
    assert client.post("/api/settings/tablebase", json={"enabled": False}).get_json()["enabled"] is False
    r = client.post("/api/play/move", json={"key": KEY, "moves": [], "uci": "a1a4", "streak": 0}).get_json()
    assert r["reply_source"] == "stockfish" and fake.calls == 0     # off: nothing asked
