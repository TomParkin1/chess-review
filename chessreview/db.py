"""SQLite schema and small query helpers.

Tables:
  games      one row per imported game (headers, my colour/result, opening, moves, clocks)
  positions  engine analysis of every position in a game (filled by engine.py)
  mistakes   judgement, report mark and explanation of every move, both sides
             (filled by classify.py; `is_mine` = 1 for my moves). Named for its first use.
  review_results  my attempts at each mistake in review mode (history; latest = status)
  imports    one row per upload / chess.com sync ("batch"); games.batch_id points here
  cards      spaced-repetition state per position to learn (srs.py): mistakes, calm positions
             I failed in real-game mode, opening lines
  meta       key/value store (e.g. hash of the classification settings)

SCHEMA is the original (version 0) layout; MIGRATIONS upgrade any database to the
current version, tracked with PRAGMA user_version.
"""

import hashlib
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

CREATE TABLE IF NOT EXISTS review_results (
    id          INTEGER PRIMARY KEY,
    game_id     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    ply         INTEGER NOT NULL,           -- the mistake (ply of the position before my move)
    result      TEXT NOT NULL,              -- solved | solved_with_help | failed | skipped
    attempts    INTEGER NOT NULL DEFAULT 0, -- wrong tries before solving / giving up
    hints_used  INTEGER NOT NULL DEFAULT 0,
    at          TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_games_analysed ON games(analysed_at);
CREATE INDEX IF NOT EXISTS idx_mistakes_judgement ON mistakes(judgement);
CREATE INDEX IF NOT EXISTS idx_review_results ON review_results(game_id, ply, id);
"""


def content_hash(white: str, black: str, date: str, moves_uci: str) -> str:
    """Identity of a game independent of where it came from (upload or chess.com API)."""
    basis = "|".join([white or "", black or "", date or "", moves_uci])
    return hashlib.sha1(basis.encode()).hexdigest()


def _migrate_1(conn: sqlite3.Connection) -> None:
    """v2 data: sources/batches, clocks, multi-PV analysis, both-side marks, accuracy."""
    for column in ("content_hash TEXT", "end_time TEXT", "uuid TEXT", "rated INTEGER",
                   "source TEXT", "batch_id INTEGER", "clocks_ms TEXT",
                   "analysis_version INTEGER", "accuracy_white REAL", "accuracy_black REAL"):
        conn.execute(f"ALTER TABLE games ADD COLUMN {column}")
    for column in ("second_cp INTEGER", "second_mate INTEGER", "second_uci TEXT"):
        conn.execute(f"ALTER TABLE positions ADD COLUMN {column}")
    for column in ("is_mine INTEGER NOT NULL DEFAULT 1", "mark TEXT", "accuracy REAL"):
        conn.execute(f"ALTER TABLE mistakes ADD COLUMN {column}")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS imports (
            id              INTEGER PRIMARY KEY,
            at              TEXT DEFAULT CURRENT_TIMESTAMP,
            source          TEXT NOT NULL,      -- 'upload' | 'chess.com'
            label           TEXT,
            games_added     INTEGER NOT NULL DEFAULT 0,
            session_done_at TEXT                -- set when the "new games" session is finished
        );
        CREATE INDEX IF NOT EXISTS idx_games_content_hash ON games(content_hash);
        CREATE INDEX IF NOT EXISTS idx_games_link ON games(link);
        CREATE INDEX IF NOT EXISTS idx_games_end_time ON games(end_time);
        CREATE INDEX IF NOT EXISTS idx_mistakes_mine ON mistakes(is_mine, judgement);
    """)
    # Backfill existing games: identity hash, sort time, source; mark old analysis as v1.
    rows = conn.execute("SELECT id, white, black, date, moves_uci, analysed_at FROM games").fetchall()
    for r in rows:
        conn.execute(
            """UPDATE games SET content_hash = ?, end_time = ?, source = 'upload',
                   analysis_version = CASE WHEN analysed_at IS NULL THEN NULL ELSE 1 END
               WHERE id = ?""",
            (content_hash(r["white"], r["black"], r["date"], r["moves_uci"]),
             f"{r['date']} 00:00:00", r["id"]))
    # Every existing game becomes part of one "earlier games" batch, already reviewed.
    if rows:
        cur = conn.execute("""INSERT INTO imports (source, label, games_added, session_done_at)
                              VALUES ('upload', 'Earlier games', ?, CURRENT_TIMESTAMP)""", (len(rows),))
        conn.execute("UPDATE games SET batch_id = ?", (cur.lastrowid,))


def _migrate_2(conn: sqlite3.Connection) -> None:
    """Spaced-repetition cards: one per mistake I should learn (later also opening lines)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cards (
            id            INTEGER PRIMARY KEY,
            kind          TEXT NOT NULL DEFAULT 'mistake',   -- 'mistake' | 'line'
            game_id       INTEGER REFERENCES games(id) ON DELETE CASCADE,
            ply           INTEGER,
            key           TEXT,                  -- for opening-line cards (position key)
            box           INTEGER NOT NULL DEFAULT 0,   -- Leitner box: 0 = new / relearning
            due_on        TEXT,                  -- 'YYYY-MM-DD'; NULL = never studied (new)
            introduced_on TEXT,                  -- day it was first studied
            reps          INTEGER NOT NULL DEFAULT 0,
            lapses        INTEGER NOT NULL DEFAULT 0,
            last_result   TEXT,
            mastered_on   TEXT,                  -- retired after passing the longest interval
            UNIQUE (game_id, ply),
            UNIQUE (kind, key)
        );
        CREATE INDEX IF NOT EXISTS idx_cards_due ON cards(mastered_on, due_on);
    """)


def _migrate_3(conn: sqlite3.Connection) -> None:
    """A first chess.com sync is a history import, not a "new games" session."""
    first = conn.execute("""SELECT id FROM imports WHERE source = 'chess.com'
                            ORDER BY id LIMIT 1""").fetchone()
    if first:
        conn.execute("""UPDATE imports SET session_done_at = COALESCE(session_done_at, CURRENT_TIMESTAMP),
                            label = 'chess.com history import' WHERE id = ?""", (first["id"],))


def _migrate_4(conn: sqlite3.Connection) -> None:
    """v3 real-game training: threats (null-move search), puzzle quality, attempt modes."""
    # positions.threat_*: what the opponent would play if I passed (my positions only), White's POV.
    for column in ("threat_cp INTEGER", "threat_mate INTEGER", "threat_uci TEXT", "threat_pv TEXT"):
        conn.execute(f"ALTER TABLE positions ADD COLUMN {column}")
    conn.execute("ALTER TABLE games ADD COLUMN threat_version INTEGER")
    for column in ("sharpness REAL", "decided INTEGER NOT NULL DEFAULT 0"):
        conn.execute(f"ALTER TABLE mistakes ADD COLUMN {column}")
    for column in ("mode TEXT", "threat_result TEXT"):
        conn.execute(f"ALTER TABLE review_results ADD COLUMN {column}")


MIGRATIONS = [_migrate_1, _migrate_2, _migrate_3, _migrate_4]   # index i upgrades user_version i -> i + 1


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, step in enumerate(MIGRATIONS[version:], start=version):
        with conn:
            step(conn)
            conn.execute(f"PRAGMA user_version = {i + 1}")


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open the database (creating it if needed), upgraded to the current schema."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def create_batch(conn: sqlite3.Connection, source: str, label: str) -> int:
    with conn:
        return conn.execute("INSERT INTO imports (source, label) VALUES (?, ?)",
                            (source, label)).lastrowid


def finish_batch(conn: sqlite3.Connection, batch_id: int) -> int:
    """Record how many games a batch added; drop it if it added none."""
    n = conn.execute("SELECT COUNT(*) FROM games WHERE batch_id = ?", (batch_id,)).fetchone()[0]
    with conn:
        if n:
            conn.execute("UPDATE imports SET games_added = ? WHERE id = ?", (n, batch_id))
        else:
            conn.execute("DELETE FROM imports WHERE id = ?", (batch_id,))
    return n


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
        conn.execute("DELETE FROM cards")
        conn.execute("DELETE FROM review_results")
        conn.execute("DELETE FROM mistakes")
        conn.execute("DELETE FROM positions")
        conn.execute("DELETE FROM games")
        conn.execute("DELETE FROM imports")
        conn.execute("DELETE FROM meta WHERE key LIKE 'chesscom_%'")  # next sync starts afresh
    return n
