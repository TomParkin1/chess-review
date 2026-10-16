"""SQLite schema and small query helpers.

Tables:
  games      one row per imported game (headers, my colour/result, opening, moves)
  positions  engine analysis of every position in a game (filled by engine.py)
  mistakes   classification + explanation of each of my moves (filled by classify.py)
  meta       key/value store (e.g. hash of the classification settings)
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id              INTEGER PRIMARY KEY,
    dedupe_key      TEXT UNIQUE NOT NULL,
    link            TEXT,
    white           TEXT,
    black           TEXT,
    white_elo       INTEGER,
    black_elo       INTEGER,
    my_color        TEXT NOT NULL,          -- 'white' | 'black'
    my_result       TEXT NOT NULL,          -- 'win' | 'loss' | 'draw'
    result          TEXT,                   -- '1-0' | '0-1' | '1/2-1/2'
    termination     TEXT,
    date            TEXT,                   -- 'YYYY-MM-DD'
    time_control    TEXT,
    time_class      TEXT,                   -- bullet | blitz | rapid | daily
    eco             TEXT,
    opening         TEXT,
    opening_family  TEXT,
    start_fen       TEXT,                   -- NULL for the standard start position
    moves_uci       TEXT NOT NULL,          -- space-separated mainline moves
    num_plies       INTEGER NOT NULL,
    pgn             TEXT NOT NULL,
    source_file     TEXT,
    imported_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    analysed_at     TEXT                    -- set when engine analysis is complete
);

CREATE TABLE IF NOT EXISTS positions (
    game_id     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    ply         INTEGER NOT NULL,           -- 0 = start position
    fen         TEXT NOT NULL,
    move_uci    TEXT,                       -- move played from this position (NULL at the end)
    move_san    TEXT,
    score_cp    INTEGER,                    -- White's point of view; NULL when mate score
    score_mate  INTEGER,                    -- moves to mate, White's POV (+ white mates)
    best_uci    TEXT,
    pv_uci      TEXT,                       -- space-separated principal variation
    depth       INTEGER,
    PRIMARY KEY (game_id, ply)
);

CREATE TABLE IF NOT EXISTS mistakes (
    game_id      INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    ply          INTEGER NOT NULL,          -- ply of the position before my move
    judgement    TEXT NOT NULL,             -- good | inaccuracy | mistake | blunder
    win_before   REAL,                      -- my win chance (-1..1) before the move
    win_after    REAL,
    win_drop     REAL,
    cp_loss      INTEGER,
    phase        TEXT,                      -- opening | middlegame | endgame
    kind         TEXT,                      -- explanation category, e.g. 'hanging_piece'
    explanation  TEXT,
    PRIMARY KEY (game_id, ply)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_games_analysed ON games(analysed_at);
CREATE INDEX IF NOT EXISTS idx_mistakes_judgement ON mistakes(judgement);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open the database (creating it if needed) with sensible settings."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def delete_all_games(conn: sqlite3.Connection) -> int:
    """Delete every game with its analysis and classification. Returns games deleted."""
    with conn:
        n = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        conn.execute("DELETE FROM mistakes")
        conn.execute("DELETE FROM positions")
        conn.execute("DELETE FROM games")
    return n
