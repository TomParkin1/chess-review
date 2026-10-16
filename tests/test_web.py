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
                  engine=replace(base.engine, time_per_position=0.05, workers=2, threads=1))
    app = create_app(cfg)
    app.testing = True
    return app.test_client(), app


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
    r = c.post("/api/evaluate", json={"game_id": m["game_id"], "ply": m["ply"], "uci": m["best_uci"]})
    assert r.get_json()["is_best"] is True

    for page in ("/", "/review", f"/game/{m['game_id']}"):
        assert c.get(page).status_code == 200


def test_evaluate_rejects_illegal_move(client):
    c, _app = client
    with open(SAMPLE, "rb") as f:
        c.post("/api/upload", data={"files": (f, "sample.pgn")}, content_type="multipart/form-data")
    wait_for_analysis(c)
    m = c.get("/api/mistakes?judgement=inaccuracy,mistake,blunder").get_json()["items"][0]
    r = c.post("/api/evaluate", json={"game_id": m["game_id"], "ply": m["ply"], "uci": "a1a8"})
    assert r.status_code == 400
