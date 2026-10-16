import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from chessreview.config import load_config
from chessreview.web import create_app

SAMPLE = Path(__file__).parent / "data" / "sample.pgn"


@pytest.fixture
def client(tmp_path):
    base = load_config()
    if not shutil.which(base.engine.path):
        pytest.skip("Stockfish not installed")
    cfg = replace(base, db_path=tmp_path / "web.db",
                  engine=replace(base.engine, time_per_position=0.05, workers=2, threads=1),
                  review=replace(base.review, live_time=0.1),
                  analysis=replace(base.analysis, max_seconds=1))
    app = create_app(cfg)
    app.testing = True
    yield app.test_client(), app
    app.extensions["live"].close()


def wait_for_analysis(client):
    import time
    for _ in range(200):
        p = client.get("/api/progress").get_json()
        if p["state"] in ("done", "error") and p["pending"] == 0:
            return p
        time.sleep(0.1)
    raise AssertionError("analysis did not finish")


def test_upload_analyse_and_review(client):
    c, _app = client
    with open(SAMPLE, "rb") as f:
        r = c.post("/api/upload", data={"files": (f, "sample.pgn")}, content_type="multipart/form-data")
    body = r.get_json()
    assert r.status_code == 200 and body["imported"] == 3 and body["duplicates"] == 1

    assert wait_for_analysis(c)["errors"] == []
    games = c.get("/api/games").get_json()
    assert len(games) == 3 and all(g["analysed"] for g in games)

    game = c.get(f"/api/game/{games[0]['id']}").get_json()
    assert len(game["positions"]) == game["game"]["num_plies"] + 1
    assert all(p["eval"] for p in game["positions"])

    mistakes = c.get("/api/mistakes?judgement=inaccuracy,mistake,blunder").get_json()
    assert mistakes["items"], "the sample games contain obvious blunders"
    m = mistakes["items"][0]
    r = c.post("/api/judge", json={"game_id": m["game_id"], "ply": m["ply"], "fen": m["fen"],
                                   "uci": m["best_uci"], "step": 0})
    assert r.get_json()["is_best"] is True
    r = c.post("/api/judge", json={"game_id": m["game_id"], "ply": m["ply"], "fen": m["fen"],
                                   "uci": m["move_uci"], "step": 0})
    body = r.get_json()
    assert body["judgement"] == m["judgement"] and body["explanation"]  # same verdict as the game

    for page in ("/", "/review", f"/game/{m['game_id']}"):
        assert c.get(page).status_code == 200


def test_judge_rejects_illegal_move(client):
    c, _app = client
    with open(SAMPLE, "rb") as f:
        c.post("/api/upload", data={"files": (f, "sample.pgn")}, content_type="multipart/form-data")
    wait_for_analysis(c)
    m = c.get("/api/mistakes?judgement=inaccuracy,mistake,blunder").get_json()["items"][0]
    r = c.post("/api/judge", json={"fen": m["fen"], "uci": "a1a8"})
    assert r.status_code == 400


def test_delete_all_games(client):
    c, _app = client
    with open(SAMPLE, "rb") as f:
        c.post("/api/upload", data={"files": (f, "sample.pgn")}, content_type="multipart/form-data")
    r = c.delete("/api/games")  # also stops the analysis that the upload started
    assert r.get_json()["deleted"] == 3
    assert c.get("/api/games").get_json() == []
    assert c.get("/api/mistakes").get_json()["items"] == []
    with open(SAMPLE, "rb") as f:  # the same games can be uploaded again afterwards
        r = c.post("/api/upload", data={"files": (f, "sample.pgn")}, content_type="multipart/form-data")
    assert r.get_json()["imported"] == 3
    wait_for_analysis(c)


def test_review_last_n_games(client):
    c, _app = client
    with open(SAMPLE, "rb") as f:
        c.post("/api/upload", data={"files": (f, "sample.pgn")}, content_type="multipart/form-data")
    wait_for_analysis(c)
    newest = c.get("/api/games").get_json()[0]["id"]
    every = c.get("/api/mistakes?judgement=inaccuracy,mistake,blunder").get_json()["items"]
    last1 = c.get("/api/mistakes?judgement=inaccuracy,mistake,blunder&last=1").get_json()["items"]
    assert {m["game_id"] for m in last1} <= {newest}
    assert len(last1) <= len(every)


def test_review_results_and_status_filter(client):
    c, _app = client
    with open(SAMPLE, "rb") as f:
        c.post("/api/upload", data={"files": (f, "sample.pgn")}, content_type="multipart/form-data")
    wait_for_analysis(c)
    q = "/api/mistakes?judgement=inaccuracy,mistake,blunder"
    items = c.get(q).get_json()["items"]
    assert all(i["status"] is None and i["hint"] and i["playout_moves"] >= 1 for i in items)
    first, second = items[0], items[1]
    c.post("/api/review/result", json={"game_id": first["game_id"], "ply": first["ply"], "result": "solved"})
    c.post("/api/review/result", json={"game_id": second["game_id"], "ply": second["ply"], "result": "failed"})

    solved = c.get(q + "&status=solved").get_json()["items"]
    failed = c.get(q + "&status=failed").get_json()["items"]
    unsolved = c.get(q + "&status=unsolved").get_json()["items"]
    assert [(i["game_id"], i["ply"]) for i in solved] == [(first["game_id"], first["ply"])]
    assert [(i["game_id"], i["ply"]) for i in failed] == [(second["game_id"], second["ply"])]
    assert len(unsolved) == len(items) - 1
    assert c.post("/api/review/result", json={"game_id": 1, "ply": 0, "result": "bogus"}).status_code == 400


def test_analysis_stream(client):
    c, _app = client
    fen = "6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1"  # Ra8#
    r = c.get(f"/api/analyse/stream?fen={fen}")
    assert r.mimetype == "text/event-stream"
    snapshots = [json.loads(line[6:]) for line in r.get_data(as_text=True).splitlines()
                 if line.startswith("data: ")]
    assert snapshots and snapshots[-1]["done"]
    best = snapshots[-1]["lines"][0]
    assert best["uci"] == "a1a8" and best["eval"]["mate"] == 1 and best["san_line"].startswith("1. Ra8#")
    assert c.get("/api/analyse/stream?fen=nonsense").status_code == 400
