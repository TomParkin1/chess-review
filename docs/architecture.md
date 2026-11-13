# Architecture

How Chess Review is put together, for anyone changing it. The README covers installing and using it.

## The pipeline

```
PGN files / chess.com API
        │  pgn_import.py, chesscom.py      (dedupe, my colour and result, clocks)
        ▼
     games ──► engine.py ──► positions       (Stockfish evals, MultiPV 2, null-move "threats")
                                 │
                                 ▼  classify.py   (win-chance drop → good / inaccuracy / mistake / blunder)
                             mistakes ──► explain.py  (plain-English reason, first matching rule)
                                 │
                                 ▼
        training: srs.py (cards) · coach.py (Today's plan) · convert.py · spar.py · calc.py · library.py
                                 │
                                 ▼
        web/ (Flask JSON API) ──► static/ (ES modules, chessground boards)
```

Engine output is stored once; everything after it can be recomputed from the stored evaluations. Changing a
threshold or an explanation rule never re-runs Stockfish.

## Modules

| Module | Responsibility |
|---|---|
| `config.py` | Loads `config.toml` into frozen dataclasses (one per section); every setting has a default |
| `db.py` | SQLite schema, numbered migrations, `meta` key/value store, the player's username |
| `backup.py` | Backups with SQLite's backup API, rotation, restore with checks |
| `pgn_import.py` | Parsing, skip rules, dedupe keys, clocks |
| `chesscom.py` | Sync from the chess.com public API, one import batch per sync |
| `openings.py` | Opening names from the Lichess openings list, matched by position |
| `engine.py` | Finding Stockfish, the batch analysis job (worker pool) |
| `live.py` | Long-lived Stockfish processes for judging tries, streaming analysis and sparring |
| `classify.py` | Judging moves, adaptive thresholds, background rebuilds |
| `explain.py` | Rule-based explanations |
| `report.py` | chess.com-style move marks, Lichess accuracy, clock rules |
| `threats.py` | "What if I passed?": the threat in a position |
| `phases.py` | Opening / middlegame / endgame |
| `srs.py`, `coach.py`, `session.py` | Spaced repetition, Today's plan, the new-games session |
| `convert.py`, `tablebase.py` | Playing won positions and essential endgames out; Lichess tablebase |
| `library.py`, `spar.py`, `calc.py`, `repertoire.py` | Puzzle library, sparring, calculation trainer, opening repertoire |
| `insights.py`, `progress.py`, `level.py` | Statistics, weaknesses, progress over time, the player's current level |
| `simulate.py` | Runs the real Today planner day by day on a copy of the database (load check) |
| `web/` | Flask app factory and one blueprint per area |

## Data

SQLite in WAL mode, by default `data/chess.db` next to the config file. Main tables:

- `games`: one row per imported game (PGN, moves, my colour and result, clocks, accuracies).
- `positions`: one row per position of an analysed game, with Stockfish's evaluation (White's point of view),
  best move, principal variation, second-best move and the null-move threat.
- `mistakes`: one row per move, both sides (`is_mine`), with judgement, win-chance drop, kind, explanation,
  mark and accuracy. Queries about the player's own moves filter `is_mine = 1`.
- `cards`, `review_results`, `training_log`, `puzzle_attempts`, `self_reviews`, `calc_attempts`,
  `spar_games`: training state and history.
- `imports`: import batches (a new-games session is the set of open batches).
- `meta`: key/value settings (username, threshold mode, classification fingerprint, puzzle rating, ...).

**Migrations** are functions in `db.MIGRATIONS`, applied in order and tracked with `PRAGMA user_version`. Old
migrations are never edited; a change is a new function. New migrations run one `conn.execute` per statement
(not `executescript`) so each stays atomic. A lock serialises the WAL switch, schema creation and migrations
when several requests open a new database at once.

**Backups** use SQLite's backup API (never a file copy, because of WAL) and are written as single files in
`<db folder>/backups/`. `serve` keeps a daily copy (the first of each ISO week is also kept as weekly) and
rotates them. A `before-*` copy is made before any destructive operation: deleting all games, pending
migrations, a full reclassification, a puzzle-library refresh. `restore` checks a file (header, `quick_check`,
expected tables, schema version) and backs up the current data first; the CLI's `restore` opens the database
without migrating, so it can recover from a broken one.

## Import

- **Skips:** games the player isn't in (name matched case-insensitively), variants, games shorter than
  `import.min_plies`, unfinished games and games python-chess can't parse.
- **Dedupe:** the `Link` header if present, else a hash of players, date and moves. chess.com API games and
  PGN exports of the same game are recognised as one. Inserts use `ON CONFLICT DO NOTHING`, so a race between
  two imports counts as a duplicate rather than failing.
- **Batches:** every import is a batch. The first import, or one whose games mostly ended more than
  `coach.history_days` ago, is "history" and skips the new-games session.
- **chess.com sync** fetches the monthly archives from the last synced month onwards.
- **Locking:** uploads, syncs, restores and "Delete all" share one lock.

## Analysis

- `engine.find_stockfish()` resolves the executable, in order:
  1. `--stockfish`
  2. `$STOCKFISH_PATH`
  3. `[engine] path`
  4. `stockfish` on PATH
  5. the usual Homebrew, `/usr/local` and Debian/Ubuntu locations
- `AnalysisJob` runs a pool of single-threaded Stockfish processes (about two thirds of the CPU cores by
  default), each taking whole games from a queue. Many single-threaded engines were measured to analyse about
  twice as many positions per second as fewer multi-threaded ones, reaching the same depth on hard positions.
  A depth cap (`engine.max_depth`) stops easy positions early.
- Each position gets MultiPV 2 (the gap to the second-best move measures how sharp the position was).
- On the player's moves there is also a null-move search: the position with the turn passed, to see what the
  opponent threatens.
- Checkmated positions are stored without an engine call.
- `ANALYSIS_VERSION` and `THREAT_VERSION` mark what each game was analysed with; older games are re-analysed,
  or get a threat-only pass.

## Classification

- **Win chance:** each move is judged by the drop in the mover's win chance (Lichess's logistic curve over
  centipawns). Lichess's mate rules (a lost or allowed forced mate) come first.
- **Adaptive thresholds:** the thresholds for inaccuracy, mistake and blunder depend on the mover's rating in
  that game (`classify.ADAPTIVE_BANDS`). A 900-rated player isn't held to a master's standard; Settings can
  switch to fixed thresholds.
- **Always good:** forced moves and the engine's own best move.
- **Rebuilds:** a fingerprint of the thresholds and `explain.VERSION` is stored in `meta`. When it changes,
  everything is re-classified from the stored evaluations. The web app does this in a background thread
  (progress in Settings) while requests keep reading the existing rows; each game is replaced in one
  transaction.
- **Decided and sharpness:** moves in positions already decided are flagged, and the gap to the second-best
  move is kept, so training can prefer instructive positions.

## Explanations

- **Rules:** `explain.RULES` is an ordered list of functions; the first that matches explains the move. Rules
  cover missed or allowed mate, a bigger miss than the loss, forks, discovered attacks, hanging pieces,
  material lost by force, missed wins and forks, exposed king, missed opportunity, and a positional fallback.
- **Checked claims:** the stored lines are short searches, so a material claim is checked against the
  evaluation before it is made. An amount the eval doesn't back is left out rather than stated wrongly. A
  trailing capture is settled with a static exchange.
- **The best move:** `explain_best` says why the engine's move was better, without repeating what the
  explanation already said.
- **Versioning:** changing any wording bumps `explain.VERSION`, which re-explains every stored move.
- **Regression tests:** `tests/test_explain_report.py` holds real positions with their stored engine rows.

## Training

- **Cards:** spaced repetition (Leitner boxes) over cards. A card is one of the player's mistakes, a failed
  calm position, an endgame, a won position, a library puzzle, a calculation question or an opening line.
  - Only the first attempt of a training day moves a card. A training day runs from 04:00 to 04:00
    (`srs.today()`, `srs.day_sql()`); never use the calendar date for it.
  - Due cards are ordered by how overdue they are relative to their box's interval.
- **Today** (`coach.today_plan`) builds a plan for a 10, 20 or 30-minute budget and freezes it in `meta` for
  the day.
  - About 30% of the budget is endgames: one endgame to play plus endgame puzzles.
  - The rest is due reviews, new mistakes, "real-game" positions and the player's top weakness.
  - The pace comes from the player's own recent attempts.
  - A plan is rebuilt when it can no longer be served (data deleted or restored) or when new games arrive
    before anything was tried.
  - `simulate` runs this planner over months to check the daily load stays within budget.
- **Real-game mode:** a threat check first, and calm positions (good moves) mixed in, so the player can't
  assume every position hides a mistake.
- **New-games session:** after an import, the player first guesses where each lost game turned (the move with
  the biggest win-chance drop), then reviews the mistakes.
- **Convert and essential endgames:**
  - won positions from the player's games and 15 checked endgames are played out against full-strength
    Stockfish
  - with 7 pieces or fewer, the Lichess tablebase defends perfectly and judges each move exactly
  - tablebase answers are cached in the database, so played endgames work offline
  - a win ends at a clear milestone (mate, a safe promotion, a bare king) and a draw goal at 15 moves held
- **Stateless playouts:** the client sends the moves so far and the server replays them; nothing about a
  game in progress is kept on the server.
- **Spar:** play from the player's positions against Stockfish limited to about their rating. Below
  Stockfish's own floor (`UCI_Elo` 1320), a share of its moves is swapped for a random top-5 or random legal
  move, at shares measured in engine matches.
- **Calculate:** read a line without seeing it played, then answer about the final position. The line gets
  longer with correct answers.
- **Puzzle library:**
  - a sample of the Lichess puzzle database, streamed and sampled per rating bucket and theme, about 19k
    puzzles
  - the player's puzzle rating is an Elo with a large K-factor at first
- **Design rule:** nothing is tuned to one rating. Values come from the engine, the current rating's band, or
  the player's own data.

## Insights and clocks

- **Insights** (`insights.compute`) and **Progress** (`progress.report`) compute trends, phase accuracy,
  mistake kinds, openings, endgame conversion and the top weaknesses, each with a "Train this" filter.
- **Clock rules** are shares of the estimated game duration (base + 40 × increment). Time trouble is under
  10%, and a very fast move takes under 1/120 (5 s at 10+0). `insights.in_time_trouble` is the one check used
  everywhere.

## Web app

- **App factory:** `web.create_app(cfg)` registers the blueprints:
  - `pages`: HTML shells
  - `games`: account, import, analysis, games list
  - `practice`: mistakes as puzzles, judging, live analysis
  - `training`: Today, sessions, Insights, Progress, Openings
  - `play`: Convert, Spar, Calculate
  - `settings`: thresholds, tablebase, puzzle library, backups
- **Shared state:** `web/common.py` holds `AppState`, with the config, the live engines, the analysis job and
  locks, in `app.extensions`. Request helpers parse input so that bad input is a 400 or 404, never a 500:
  `json_body`, `int_field`, `color_field`, `board_from_fen`, `moves_field`.
- **Errors:** `/api/*` errors are always JSON `{error}`. A missing or crashed Stockfish is a 503 that says
  so.
- **Live engines:** `LiveEngine` keeps separate processes for judging, streaming and sparring. The live
  analysis stream runs on a worker thread feeding a queue, so a slow reader never holds the engine. `serve`
  closes the processes on shutdown; otherwise Python can't exit.
- **Binding:** the server binds to 127.0.0.1 only.

## Front end

- **Modules:** plain ES modules with no build step. Each page's template is a thin shell, and a module in
  `static/` fetches its data from the API.
- **Libraries:** chessground (boards) and chess.js come from jsdelivr through an import map in `base.html`.
- **Shared helpers:**
  - `common.js`: `api()`, error and empty states, `guard()` for first loads
  - `board.js`: board creation, settings, promotion picker, sounds, keyboard moves
  - `puzzle.js`: the puzzle component used by Practice, Today and sessions
- **Chessground quirks:**
  - a board that takes moves must be created with `viewOnly: false`
  - `events.after` handlers are wrapped as `(o, d) => fn(o, d)`
  - `redrawAll()` is never called, because it wipes the badge layer
  - the board's cached size is re-measured on pointer events, since a board measured while hidden has no size
- **Accessibility:** everything clickable is reachable by keyboard, and boards have a hidden move box (SAN or
  UCI).

## Tests

- **Running:** `pytest`, configured in `pyproject.toml`. Tests read `tests/config.toml` (through
  `$CHESSREVIEW_CONFIG`), never the user's config.
- **The `stockfish` fixture** gives real Stockfish when installed, else `tests/fake_uci.py`, a deterministic
  one-move-lookahead UCI engine that is enough for the app's plumbing. Tests that need real strength use
  `real_stockfish`. `CHESSREVIEW_FAKE_ENGINE=1` forces the fake.
- **No network:** every test runs with outside connections and lookups refused (loopback is allowed). The
  tablebase and chess.com fetches are stubbed per test.
- **Opt-in suites with real Stockfish:**
  - `CHESSREVIEW_PLAYOUTS=1`: every endgame won or held by strong play
  - `CHESSREVIEW_CALIBRATION=1`: sparring strength measured in engine matches
- **Pinned routes:** `tests/test_routes.py` pins the URL map.
