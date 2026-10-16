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

    for page in ("/", "/review", "/games", "/settings", "/insights", "/train", "/session", "/progress", "/convert", "/puzzles", f"/game/{m["game_id"]}"):
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


def test_insights_api(client):
    c, _app = client
    with open(SAMPLE, "rb") as f:
        c.post("/api/upload", data={"files": (f, "sample.pgn")}, content_type="multipart/form-data")
    wait_for_analysis(c)
    d = c.get("/api/insights").get_json()
    assert d["games"] == 3 and len(d["trend"]) == 3
    assert {p["phase"] for p in d["phases"]} == {"opening", "middlegame", "endgame"}
    assert sum(o["games"] for o in d["openings"]) == 3
    assert isinstance(d["weaknesses"], list)
    assert c.get("/api/insights?last=1").get_json()["games"] == 1


def test_openings_api(client):
    c, _app = client
    with open(SAMPLE, "rb") as f:
        c.post("/api/upload", data={"files": (f, "sample.pgn")}, content_type="multipart/form-data")
    wait_for_analysis(c)
    for color in ("white", "black"):
        assert c.get(f"/api/openings?color={color}").status_code == 200
        root = c.get(f"/api/openings/node?color={color}").get_json()
        assert root["games"] >= 1
        assert c.get(f"/api/openings/drill?color={color}").status_code == 200
    assert c.get("/api/openings/node?color=white&fen=8/8/8/8/8/8/8/K6k w - - 0 1").status_code == 404
    assert c.post("/api/openings/result", json={"key": "white:x", "result": "solved"}).get_json()["ok"]
    assert c.get("/openings").status_code == 200


def test_threshold_mode_switch_reclassifies(client):
    c, _app = client
    with open(SAMPLE, "rb") as f:
        c.post("/api/upload", data={"files": (f, "sample.pgn")}, content_type="multipart/form-data")
    wait_for_analysis(c)
    info = c.get("/api/settings/thresholds").get_json()
    assert "adaptive" in info and len(info["standard_thresholds"]) == 3
    std = c.post("/api/settings/thresholds", json={"adaptive": False}).get_json()
    assert std["adaptive"] is False and std["games"] == 3          # every game reclassified
    assert c.get("/api/settings/thresholds").get_json()["adaptive"] is False
    ada = c.post("/api/settings/thresholds", json={"adaptive": True}).get_json()
    # Sample players are rated ~1200 (or unrated): adaptive can only be as strict or more lenient.
    assert ada["mistakes"] + ada["blunders"] <= std["mistakes"] + std["blunders"]


def test_threat_check_and_real_game_queue(client):
    c, _app = client
    with open(SAMPLE, "rb") as f:
        c.post("/api/upload", data={"files": (f, "sample.pgn")}, content_type="multipart/form-data")
    wait_for_analysis(c)
    items = c.get("/api/mistakes?judgement=inaccuracy,mistake,blunder&status=").get_json()["items"]
    with_threat = [m for m in items if m["threat"]]
    assert with_threat, "analysis stores the null-move threat of my positions"
    m = with_threat[0]
    t = m["threat"]
    none = c.post("/api/threat/check", json={"game_id": m["game_id"], "ply": m["ply"], "uci": None}).get_json()
    assert none["correct"] is (not t["real"])
    if t["uci"]:
        exact = c.post("/api/threat/check", json={"game_id": m["game_id"], "ply": m["ply"], "uci": t["uci"]}).get_json()
        assert exact["correct"] is t["real"] and exact["san"] == t["san"]

    st = c.get("/api/session").get_json()          # new games wait for their session first
    c.post("/api/session/done", json={"batch_ids": st["batch_ids"]})
    q = c.get("/api/train/queue?blind=1").get_json()
    assert q["blind"] and q["items"] and all(i["mode"] == "blind" for i in q["items"])
    first = q["items"][0]
    r = c.post("/api/review/result", json={"game_id": first["game_id"], "ply": first["ply"], "result": "solved",
                                           "mode": "blind", "calm": first["calm"], "threat_result": "correct"})
    assert r.status_code == 200
    assert c.get("/api/train/queue?blind=0").get_json()["blind"] is False


def test_today_and_progress_api(client):
    c, _app = client
    with open(SAMPLE, "rb") as f:
        c.post("/api/upload", data={"files": (f, "sample.pgn")}, content_type="multipart/form-data")
    wait_for_analysis(c)
    st = c.get("/api/session").get_json()
    c.post("/api/session/done", json={"batch_ids": st["batch_ids"]})
    today = c.get("/api/today").get_json()
    plan = today["plan"]
    assert plan["total"] > 0 and all("items" not in b for b in plan["blocks"])
    for b in plan["blocks"]:
        if b["id"] in ("opening", "convert"):    # played on their own pages
            continue
        items = c.get(f"/api/today/{b['id']}").get_json()["items"]
        assert len(items) == b["count"]
        assert all(i["mode"] == "blind" for i in items) if b["id"] == "real" else True
    assert c.get("/api/today/nonsense").status_code == 404
    report = c.get("/api/progress/report").get_json()
    assert report["games"] == 3 and report["summary"] and len(report["headline"]) == 6


def test_endgame_playout_api(client):
    c, _app = client
    overview = c.get("/api/convert").get_json()
    assert overview["endgames"] and overview["positions"] == []
    key = "endgame/kq_k"
    task = c.get(f"/api/convert/task?key={key}").get_json()
    assert task["goal"] == "win" and task["color"] == "white"
    r = c.post("/api/play/move", json={"key": key, "moves": [], "uci": "h1h5", "streak": 0}).get_json()
    assert r["san"] == "Qh5+" and r["reply_uci"] and r["status"] == "playing"
    bad = c.post("/api/play/move", json={"key": key, "moves": ["h1h5"], "uci": "h1h5"})
    assert bad.status_code == 400
    hint = c.post("/api/play/hint", json={"key": key, "moves": ["h1h5", r["reply_uci"]]}).get_json()
    assert hint["uci"]
    assert c.post("/api/convert/result", json={"key": key, "result": "solved"}).status_code == 200
    statuses = {e["key"]: e["status"] for e in c.get("/api/convert").get_json()["endgames"]}
    assert statuses[key] == "solved"


def test_library_api_without_a_library(client):
    c, _app = client
    s = c.get("/api/library/status").get_json()
    assert s["count"] == 0 and s["progress"]["state"] == "idle" and s["rating"] == 1500   # no games: default
    assert c.get("/api/library/next").get_json()["items"] == []
    assert c.post("/api/library/result", json={"puzzle_id": "nope", "result": "solved"}).status_code == 404
