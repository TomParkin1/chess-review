import io
import json
import shutil
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from chessreview import backup, chesscom, coach, db
from chessreview.config import PlayerConfig, load_config
from chessreview.pgn_import import import_pgn_text
from chessreview.web import create_app

SAMPLE = Path(__file__).parent / "data" / "sample.pgn"


def make_app(tmp_path, username=None):
    base = load_config()
    if not shutil.which(base.engine.path):
        pytest.skip("Stockfish not installed")
    cfg = replace(base, db_path=tmp_path / "web.db",
                  engine=replace(base.engine, time_per_position=0.05, workers=2, threads=1),
                  review=replace(base.review, live_time=0.1),
                  analysis=replace(base.analysis, max_seconds=1))
    if username is not None:
        cfg = replace(cfg, player=PlayerConfig(username))
    app = create_app(cfg)
    app.testing = True
    return app, cfg


@pytest.fixture
def client(tmp_path):
    app, _cfg = make_app(tmp_path)
    yield app.test_client(), app
    app.extensions["live"].close()


@pytest.fixture
def new_user(tmp_path):
    """A first run: no username in config.toml, empty database."""
    app, _cfg = make_app(tmp_path, username="")
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

    # Misses: only mistakes/blunders marked "miss" (right after the opponent's own error).
    misses = c.get("/api/mistakes?judgement=miss").get_json()["items"]
    assert all(i["mark"] == "miss" for i in misses)

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
    c.post("/api/today/refresh")
    items = c.get("/api/today/real").get_json()["items"]
    assert items and all(i["mode"] == "blind" for i in items)
    first = items[0]
    r = c.post("/api/review/result", json={"game_id": first["game_id"], "ply": first["ply"], "result": "solved",
                                           "mode": "blind", "calm": first["calm"], "threat_result": "correct"})
    assert r.status_code == 200
    assert c.get("/api/train/queue?blind=0").status_code == 404     # B18: Today replaced the old queue


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


# ------------------------------------------------------------ Finish M1

PAGE_APIS = ["/api/today", "/api/today/reviews", "/api/today/real", "/api/today/focus", "/api/train/stats",
             "/api/insights?last=100", "/api/progress/report", "/api/convert", "/api/openings?color=white",
             "/api/session", "/api/session/self-review", "/api/mistakes", "/api/games", "/api/sync",
             "/api/library/status", "/api/account", "/api/backups", "/api/settings/thresholds"]


def test_pages_work_on_an_empty_and_an_unanalysed_database(client, tmp_path):
    """B1: on a new install (and until the first game is analysed) /api/today was a 500, so Today
    stayed on "Loading your plan…" and the badge failed on every page."""
    c, _app = client
    def check():
        for url in PAGE_APIS:
            r = c.get(url)
            assert r.status_code in (200, 404) and (r.status_code == 200 or "/api/today/" in url), url
    check()
    today = c.get("/api/today").get_json()
    assert today["plan"]["total"] == 0 and today["games"] == {"total": 0, "analysed": 0}
    conn = db.connect(tmp_path / "web.db")       # imported but not analysed (no analysis job here)
    import_pgn_text(conn, SAMPLE.read_text(), load_config())
    conn.close()
    check()
    assert c.get("/api/today").get_json()["games"] == {"total": 3, "analysed": 0}


def test_two_uploads_of_the_same_games_at_once(client, tmp_path):
    """B8: the second upload failed with a 500 (UNIQUE constraint failed: games.dedupe_key) and left
    an empty batch behind, although the first did import the games."""
    _c, app = client
    results = []
    def upload():
        with open(SAMPLE, "rb") as f:
            r = app.test_client().post("/api/upload", data={"files": (f, "sample.pgn")},
                                       content_type="multipart/form-data")
        results.append((r.status_code, r.get_json()))
    threads = [threading.Thread(target=upload) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert [code for code, _ in results] == [200, 200]
    assert sorted(body["imported"] for _, body in results) == [0, 3]
    c = app.test_client()
    wait_for_analysis(c)
    assert len(c.get("/api/games").get_json()) == 3
    batches = db.connect(tmp_path / "web.db").execute("SELECT COUNT(*) FROM imports").fetchone()[0]
    assert batches == 1      # the empty second batch was dropped


def test_api_errors_are_json_and_pages_say_what_went_wrong(client, monkeypatch):
    """B11: a failed call left pages loading for ever; an unknown game threw "NOT FOUND"."""
    c, _app = client
    r = c.get("/api/game/999999")
    assert r.status_code == 404 and "Game not found" in r.get_json()["error"]
    assert c.get("/api/no-such-thing").get_json()["error"]
    monkeypatch.setattr(coach, "today_plan", lambda *a, **k: 1 / 0)
    r = c.get("/api/today")
    assert r.status_code == 500 and "ZeroDivisionError" in r.get_json()["error"]
    page = c.get("/no-such-page")
    assert page.status_code == 404 and page.mimetype == "text/html"


def test_housekeeping(client):
    """B18: favicon.ico was a 404 on every page; /api/train/queue was unused."""
    c, _app = client
    icon = c.get("/favicon.ico")
    assert icon.status_code == 200 and icon.mimetype == "image/svg+xml"
    assert c.get("/api/train/queue").status_code == 404


def fake_chesscom(url, retries=4):
    if "/player/nobody/" in url:
        raise LookupError("chess.com user not found")
    if url.endswith("/archives"):
        return {"archives": ["https://api.chess.com/pub/player/newbie/games/2025/01",
                             "https://api.chess.com/pub/player/newbie/games/2026/10"]}
    return {"games": []}


def test_first_run_sets_the_username_in_the_app(new_user, monkeypatch):
    """F6: the username lived only in config.toml; a new user can now set it on Home or in Settings."""
    c, _app = new_user
    monkeypatch.setattr(chesscom, "fetch_json", fake_chesscom)
    assert c.get("/api/account").get_json()["username"] == ""
    with open(SAMPLE, "rb") as f:
        r = c.post("/api/upload", data={"files": (f, "sample.pgn")}, content_type="multipart/form-data")
    assert r.status_code == 400 and "username" in r.get_json()["error"]
    assert c.post("/api/sync", json={"since": "1m"}).status_code == 400
    assert b"Welcome to Chess Review" in c.get("/").data

    assert c.post("/api/account", json={"username": "not a name!"}).status_code == 400
    r = c.post("/api/account", json={"username": "nobody"})
    assert r.status_code == 404 and "nobody" in r.get_json()["error"]
    a = c.post("/api/account", json={"username": "Newbie"}).get_json()
    assert a["username"] == "Newbie" and a["username_source"] == "app"
    assert a["months"] == 2 and a["first_month"] == "2025/01"
    home = c.get("/").data
    assert b"Welcome, Newbie" in home and b"Welcome back" not in home and b"first-sync" in home
    assert c.post("/api/sync", json={"since": "1m"}).status_code == 200
    c.delete("/api/games")                                   # Delete all keeps who I am
    assert c.get("/api/account").get_json()["username"] == "Newbie"


def test_backups_api(client):
    c, _app = client
    assert c.get("/api/backups").get_json()["backups"] == []
    with open(SAMPLE, "rb") as f:
        c.post("/api/upload", data={"files": (f, "sample.pgn")}, content_type="multipart/form-data")
    wait_for_analysis(c)
    name = c.post("/api/backups").get_json()["backup"]
    assert c.get("/api/backups").get_json()["backups"][0]["games"] == 3

    exported = c.get("/api/backups/export")
    assert exported.status_code == 200 and exported.data.startswith(backup.SQLITE_HEADER)
    assert "attachment" in exported.headers["Content-Disposition"]

    c.delete("/api/games")
    assert c.get("/api/games").get_json() == []
    kinds = {b["kind"] for b in c.get("/api/backups").get_json()["backups"]}
    assert {"manual", "before-delete"} <= kinds
    r = c.post("/api/backups/restore", json={"name": name}).get_json()
    assert r["games"] == 3 and r["before"].startswith("chess-before-restore-")
    assert len(c.get("/api/games").get_json()) == 3

    c.delete("/api/games")
    r = c.post("/api/backups/restore", data={"file": (io.BytesIO(exported.data), "x.db")},
               content_type="multipart/form-data")
    assert r.status_code == 200 and r.get_json()["games"] == 3          # an exported copy, restored
    bad = c.post("/api/backups/restore", data={"file": (io.BytesIO(b"nonsense"), "x.db")},
                 content_type="multipart/form-data")
    assert bad.status_code == 400 and "database" in bad.get_json()["error"]
    assert c.post("/api/backups/restore", json={"name": "../web.db"}).status_code == 404
    assert len(c.get("/api/games").get_json()) == 3                     # a refused file changes nothing


def test_today_refresh_api(client):
    c, _app = client
    with open(SAMPLE, "rb") as f:
        c.post("/api/upload", data={"files": (f, "sample.pgn")}, content_type="multipart/form-data")
    wait_for_analysis(c)
    today = c.post("/api/today/refresh").get_json()
    assert today["games"] == {"total": 3, "analysed": 3} and not today["plan"]["stale"]


def test_today_budget_and_todays_opening_lines(client):
    """F4: the Today page sets the time budget; the plan is rebuilt for it."""
    c, _app = client
    r = c.post("/api/today/budget", json={"minutes": 30})
    assert r.status_code == 200
    body = r.get_json()
    assert body["plan"]["budget"] == 30 and body["budgets"] == [10, 20, 30] and "minutes_left" in body["plan"]
    assert c.get("/api/today").get_json()["plan"]["budget"] == 30
    assert c.post("/api/today/budget", json={"minutes": 15}).status_code == 400
    assert c.get("/api/openings/drill?mode=today").get_json() == {"lines": [], "colors": []}
