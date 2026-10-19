# Chess Review

Local web app that imports my chess.com PGN exports, analyses every move I made
with Stockfish, classifies mistakes Lichess-style, explains them in plain English,
and lets me review them across all games. chess.com username: `sampleplayer`
(`config.toml`, or set in the app: `db.username()`); every imported game is mine and my colour comes from the headers.

## Running
- Python 3.15 venv in `.venv` (Homebrew Python, not Anaconda): use `.venv/bin/python`.
- Stockfish: `/opt/homebrew/bin/stockfish` (configurable in `config.toml`).
- CLI: `.venv/bin/python -m chessreview sync [--since all|3m|1m]`, `import <files|dirs>`, `summary`,
  `analyse [--limit N]`, `reclassify`, `mistakes [--limit N]`, `serve [--port 8000]`, `puzzles`, `reset [--yes]`,
  `backup [--list]`, `restore NAME|FILE [--yes]` (opens the DB without migrating first: the way back from a broken DB).
  Port 5000 is taken by macOS AirPlay Receiver, hence 8000.
- Tests: `.venv/bin/pytest`.

## Layout
- `config.toml`: all tunables (engine time/workers, classification thresholds, phase rules, DB path, backup
  rotation). `[player]` is optional (first run asks for the username).
- `chessreview/config.py`: loads config into dataclasses.
- `chessreview/db.py`: SQLite schema + numbered `MIGRATIONS` (PRAGMA user_version; add a function, never edit
  old ones). Tables: `games`, `positions`, `mistakes` (every move, both sides; `is_mine`, `mark`, `accuracy`),
  `review_results`, `imports` (batches), `meta`. DB lives in `data/` (gitignored). `db.username(conn, cfg)` = meta
  `player_username` (set from Home/Settings, survives Delete all) else config. `_schema_lock` serialises WAL switch,
  schema and migrations when requests open a new DB at once. `db.restore()` = `backup.restore()` + `migrate()`.
- `chessreview/backup.py` (Finish M1): copies with SQLite's backup API (never a file copy: WAL), switched to a
  rollback journal so each is one file, in `<db dir>/backups/chess-<kind>-<YYYYMMDD-HHMMSS>.db`. `serve` runs
  `DailyBackups` (startup + hourly; first daily of an ISO week also kept as `weekly`); rotation `[backup]` 7 daily,
  4 weekly, 10 others. `before-*` copies are made inside `db.delete_all_games`, `db.migrate` (pending migrations on
  an existing DB), `classify.ensure_classified` (full rebuild with mistakes present) and `library.fetch` (refresh):
  call `backup.make(conn, "before-<what>")` before any new destructive operation. `restore()` checks the file
  (header, quick_check, games+meta, user_version) and backs up the current data first.
- `chessreview/chesscom.py`: sync via the public API (archives -> months >= last synced month), one batch per sync.
  Dedupe across sources = `link` OR `content_hash` (players|date|moves). API games carry clocks (`games.clocks_ms`).
- `chessreview/srs.py`: Leitner spaced repetition over `cards` (one per my mistake/blunder); every attempt
  (`/api/review/result`) updates the card. Daily queue = due + `training.new_per_day` new (not from open sessions).
- `chessreview/session.py`: "new games" session = open import batches (session_done_at NULL). The first chess.com
  sync is a history import (marked done). Summary: results, accuracy, mistake kinds, clock left at blunders.
- `chessreview/insights.py`: per-game trends, phase accuracy, mistake kinds, clock buckets, openings, colours and
  heuristic `weaknesses()` (each with a Practice filter). `static/charts.js`: SVG line/bar charts with hover
  (dataviz rules: one axis, 2px lines, rounded bar ends, validated colours `--series-you/--series-opp`).
  Practice (`/review`) accepts filters in the URL (incl. `low_clock`), overriding saved filters.
- `chessreview/repertoire.py`: repertoire tree per colour from my games (EPD-keyed, first MAX_PLY plies);
  repertoire move = my most played (>= 2 games) unless usually judged bad -> engine best ("problem").
  Drill lines + due exercises; line cards are `cards.kind='line'`, key `<colour>:<epd>` (srs.record_line).
  UI: `/openings` (`static/openings.js`: Explore tab + Drill tab).
- `static/puzzle.js` + `templates/_puzzle.html`: the shared puzzle used by Practice (`/review`), Train, Session.
- `chessreview/report.py`: chess.com-style move marks (documented approximations), Lichess accuracy, time spent.
- `chessreview/pgn_import.py`: PGN parsing, my colour/result, time class, dedupe (Link header, else sha1 of players+date+moves).
- `chessreview/openings.py` + `chessreview/data/openings.tsv`: names openings from the moves using the Lichess chess-openings DB (CC0), matched by position. My chess.com exports have no ECO/ECOUrl/Link headers.
- `chessreview/engine.py`: Stockfish analysis (MultiPV 2, `ANALYSIS_VERSION`; older games re-analysed), worker pool, progress, caching. Checkmated positions store score_cp = ±10000 (MATED_CP), no engine call.
- `chessreview/classify.py`: Lichess win-chance judgement per move (both sides) -> `mistakes` table, plus marks and game accuracy. My-mistake queries must filter `is_mine = 1`. `ensure_classified()` rebuilds
  automatically when config thresholds or `explain.VERSION` change (fingerprint in `meta`). Forced moves and the engine's
  own best move are always "good".
- `chessreview/explain.py`: ordered list of rule functions (`RULES`), first match wins. Bump `VERSION` when changing output.
- `chessreview/phases.py`: opening/middlegame/endgame.
- `chessreview/web.py`: Flask app factory `create_app(cfg)`; pages (`/`, `/game/<id>`, `/review`) are thin shells,
  data comes from the JSON API (`/api/upload`, `/api/progress`, `/api/analysis/start|stop`, `/api/games`,
  `/api/game/<id>`, `/api/mistakes?filters` incl. `status` & `last`, `/api/judge`, `/api/review/result`,
  `/api/analyse/stream` (SSE), `/api/account`, `/api/backups` (+ `/restore`, `/export`), `/api/today/refresh`).
  Upload auto-starts an `AnalysisJob`. Uploads, syncs and restores share one `import_lock` (B8); `import_game`
  inserts with `ON CONFLICT(dedupe_key) DO NOTHING` (a race counts as a duplicate) and batches finish in `finally`.
- `chessreview/live.py`: `LiveEngine` = two long-lived Stockfish processes (judging tries, live analysis stream).
  The stream runs on a worker thread feeding a queue (a paused SSE generator must never hold the engine lock).
  `serve` closes it in a finally block, tests close it in fixtures, otherwise Python cannot exit.
- Adaptive thresholds (default; `classification.adaptive`, overridable from Settings via meta `thresholds_mode`):
  `classify.thresholds_for(rating)` bands by the mover's rating in that game; `/api/judge` uses my rating in the game.
  Switching mode changes the classification fingerprint, so `ensure_classified` rebuilds everything (seconds).
- `explain.explain_best()`: why the engine's move is better (shown after a puzzle as `best_explanation`).
- Analysis speed: `effective_workers()` (~2/3 of cores) single-thread engines + `max_depth` cap (benchmarked 2x
  faster than 4x2 threads, same depth on hard positions). Live judging uses 4 threads.
- v3 real-game training (Milestone 1):
  - `engine.py` also runs a null-move search on my positions ("what if I passed?") -> `positions.threat_*`
    (cp/mate/uci/pv, White's POV), `games.threat_version` / `THREAT_VERSION`; older games get a threat-only pass.
  - `threats.py`: `threat_of()` = win-chance drop if I passed; "real" when >= the *mistake* threshold at my current
    level AND the line mates / wins material / starts with a forcing move (otherwise it is my lost chance, e.g. they castle
    out of my tactic). `training_threat()` skips positions where a recapture is pending.
  - `level.py`: `current_rating()` / `current_thresholds()`. `/api/judge`, threats and calm positions use my level *now*.
  - `mistakes.sharpness` (best vs second-best gap) and `decided` (band-scaled, `Thresholds.decided`); new cards are
    ordered by `srs.PUZZLE_VALUE`, decided ones skipped. Missed mates are never "decided".
  - Real-game mode (`item.mode = 'blind'`, Train default `training.blind`, Practice checkbox): no badge/arrow; threat step
    first (`/api/threat/check`); Today's real-game block mixes in `srs.calm_positions()` (`coach.calm_ratio`; good moves,
    half with a real threat). Failed calm positions become `cards.kind='position'`. `review_results.mode/threat_result` record it.
- v3 Today + Progress (Milestone 2):
  - `coach.py`: `today_plan()` builds the day's blocks (reviews capped at `coach.due_cap`, real-game = new cards + calm
    positions, focus = my #1 weakness from `insights.weaknesses(last=100)`, opening lines) and freezes it in meta
    `today_plan`; an item is done once it has a review result today (`attempted_today`), lines via `cards.last_on`.
    API `/api/today`, `/api/today/<block>`; the Train page (`/train`, sidebar "Today") is the block checklist + runner.
    Same-day rebuilds (Finish M1, B6): plan `basis` = analysed-game count; rebuilt on `refresh` (POST
    `/api/today/refresh`), when an item can't be served (`_servable`: Delete all, restore, missing puzzle) or when the
    basis changed and nothing was tried yet; otherwise `stale`/`new_games` -> "Refresh plan" prompt. `_rebuild` keeps
    today's tried items (they count towards each block's usual size) and done opening/convert blocks.
  - `progress.py`: last N games vs the N before (`coach.progress_window`, game windows not weeks), metrics at my current
    level (`level.winning_chance` bands for conversion), rolling series, kinds per game, weekly training. `/progress`,
    `/api/progress/report`, summary sentence on Home. (Route fn for `/api/progress` is `analysis_progress`.)
- v3 Convert (Milestone 3): `convert.py` + `data/endgames.toml` (15 Stockfish-checked endgames, 3 levels; a level
  unlocks when the one before is solved). Won positions = first position per game at/above `level.winning_cp` (by
  rating band) held on my next move; lost games first. `state()` decides won/failed per move (checkmate; Stockfish
  resigns at mate <= 5 or +7 for 3 of my moves; any draw fails a win goal; convert fails below 40% of the bar; draw
  goals hold for 15 moves). Playouts are stateless: the client sends all moves (`/api/play/move`, `/api/play/hint`),
  Stockfish = `LiveEngine.analyse` at full strength. Cards: `srs.record_key(kind 'convert'|'endgame', key)`.
  Page `/convert` (`static/convert.js`, eval hidden until the end); Today gets a "Convert" block.
- v3 puzzle library (Milestone 4): `library.py` streams the Lichess puzzle DB (`lichess_db_puzzle.csv.zst`, ~300 MB,
  stdlib `compression.zstd`) and keeps a reservoir sample per (100-point bucket, theme in `THEMES`), 400-3100, well-tested
  only (~19k puzzles) in `puzzles`; attempts in `puzzle_attempts`. Puzzle rating (meta) starts at my chess.com rating,
  Elo update with K 40 -> 16, first try only; `pick()` chooses unseen puzzles around it. Failed puzzles become
  `cards.kind='library'` (due ones join Today's Review). `KIND_THEMES` maps my mistake kinds to themes; Today's Focus
  = half my mistakes, half themed library puzzles. puzzle.js `mode: 'library'` judges locally against the solution
  (any mate counts). UI: `/puzzles` (Practice switch), Settings card, CLI `puzzles`. Block items for the library are
  `["lib", puzzle_id, tag]` in the Today plan.
- v3 self-review (Milestone 5): the new-games session starts with "where did this game turn?" for games that went wrong
  (lost, drawn, or with a blunder of mine; `coach.self_review_games`, losses first). Turning point = my move with the
  biggest win-chance drop (`session.turning_point`); a pick within 2 plies counts. Table `self_reviews`; API
  `/api/session/self-review` (GET games without evals/marks, POST pick); hit rate in the session summary and Progress.
  The self-review board markup sits after `_puzzle.html` (the puzzle attaches its eval bar to the first `.board-wrap`).
- v3 feedback round 1:
  - Threats: a quiet first move (castling, development) is never "the threat" just because the line wins material
    later; it must mate, check, promote, capture-and-win, or newly attack a piece of mine that is undefended or worth
    more than its cheapest attacker (`threats._threatened`).
  - `explain_best` only adds what the explanation of my move doesn't already say (no repeated line or gain).
  - Misses (report mark `miss`) are shown as "miss" in puzzles; Practice severity filter `judgement=miss`.
  - Sidebar badge (`shell.js` `showTodayBadge`) = items left in today's plan, gone when complete.
  - Today continues into Convert/Openings (`?from=today` -> "Back to Today" there).
  - `analysis.start(fen, {history})`: moves that led to the position (greyed, ◀ steps back through them).
  - Convert after a failure: biggest win-chance drop of my moves + Stockfish's move there, "Try again from there"
    (counts as solved_with_help at best), "Watch Stockfish play it" (hint + play/move loop, nothing recorded).
- Design rule: nothing is tuned to one rating. Derive from the engine, the current rating's band, or my rolling data.
- Chessground `events.after` passes (orig, dest, metadata): always wrap handlers as `(o, d) => fn(o, d)`.
- `classify.judge` applies Lichess' mate rules (lost/allowed forced mate) before win chances; bump
  `CLASSIFY_VERSION` when judge logic changes.
- Review page = Lichess-style "learn from your mistakes" (`static/review.js` state machine: solving → checking →
  wrong (snap back + spoiler-free explanation via `explain(..., reveal_best=False)`) / right (auto-play reply, continue
  for `playout_moves`) → done (explanation, best-line stepper)). Hints: text from `explain.HINTS`, then piece highlight.
  Chessground boards that take moves must be created with `viewOnly: false` (listeners bind only at creation).
- UI preferences: review is always interactive (puzzle first); board sized from window height for desktop; chess.com
  "Neo" pieces (hotlinked from images.chesscomfiles.com) on a brown/cream board (CSS in `style.css`).
- `static/board.js`: makeBoard + settings (localStorage, applied via `html[data-board|data-pieces]`), square badges,
  promotion picker, WebAudio sounds. `static/icons.js`: our own SVG mark badges (`MARKS`, `markIcon`).
- `static/game.js` = Game Report (coach, accuracy, mark counts, key moments, eval graph + time chart with hover).
- `static/evalbar.js` (vertical eval bar) and `static/analysis.js` (free analysis board: variation + SSE live lines,
  closes its EventSource on `done` so it doesn't auto-reconnect) are shared by game view and review.
- `chessreview/templates/` + `chessreview/static/`: plain ES modules, no build step. chessground 9.2.1 and
  chess.js 1.4.0 via an import map from jsdelivr (see `base.html`). `common.js` has shared helpers.
- Error and empty states (Finish M1, B11/F6): `/api/*` errors are always JSON `{error}` (web.py error handlers,
  500s logged). `common.js`: `api()` throws `ApiError` (`status`, 0 = server unreachable), `errorState()`,
  `emptyState()`, `guard(where, load, what)` (shows why + Try again), `need(where, url, what)` (data a page can't
  work without; on failure it never resolves, so the module stops quietly). `shell.js` shows a banner for any
  uncaught error. Every page's first load is guarded; empty DB states point to Home. First run: Home asks for the
  username (`/api/account`, checked against chess.com), then a first-sync choice; "Welcome back" only if the first
  import was before today; Today polls while the first games are analysed.

## Conventions
- Minimal dependencies: `chess`, `Flask` (+ `pytest` for dev). Stdlib otherwise (`sqlite3`, `tomllib`).
- Engine analysis, classification and explanation stay in separate modules; classification
  is re-derivable from stored evals, so never re-run the engine just to change thresholds.
- Scores are stored from White's point of view; convert to "my" view at classification time.
- Keep code readable over clever; small functions; type hints.
- Built in stages, with a git commit after each working stage.

## Status
- [x] Stage 1: import, dedupe, CLI summary
- [x] Stage 2: Stockfish analysis with caching and progress (`analyse` CLI, AnalysisJob worker pool)
- [x] Stage 3: classification and explanations
- [x] Stage 4: web UI (upload, game view, mistake review)
- [x] Interactive review: live judging + hints + play-out, eval bar, analysis board

## v2 roadmap (plan approved; build milestone by milestone, stop after each for testing)
1. [x] Data foundation: migrations, chess.com sync, batches, clocks, MultiPV 2, both-side marks, accuracy
2. [x] New shell (chess.com-dark sidebar, icons, sounds, promotion, settings) + Game Report page
3. [x] Training: puzzle.js component, SRS cards + daily Train, New-games session + summary, Practice
4. [x] Home dashboard + Insights (trends, clock analysis, openings, top-3 weaknesses -> Train this)
5. [x] Openings: repertoire explorer + line drills (SRS)
Browser tests: scratchpad `drive.mjs` (CDP) against a copy of the DB via `--config <copy config>`; never the real DB.

## v3 roadmap (plan approved; milestone by milestone, stop after each for testing)
1. [x] Real-game training: threat check, blind positions + calm mix, puzzle quality, current-level judging
2. [x] "Today" workout + progress report
3. [x] Convert won positions + essential endgames
4. [x] Puzzle library (Lichess DB, every rating, puzzle rating)
5. [x] Self-review before the engine in new-games sessions

## Finish roadmap (audit of 19 Oct 2026: bugs B1–B18, features F1–F17; one milestone at a time)
1. [x] M1 Safe & solid: backups (F2), B1 B2 B4 B6 B7 B8 B11 B18, first run (F6). Regression test per bug.
2. [ ] M2 Training integrity: one promotion per card per day (B3), load budget (F4), streak (B9), 04:00 day (B13),
   double-click guards (B12), sessions never block Today (B5)
3. [ ] M3 Correct lessons: explanation rules P1–P10, P13; lines through the gain; deeper re-check of card positions
4. [ ] M4 Endgames that teach: finish rules (P11), Vancura tip (P12), Convert lesson board (B14)
5. [ ] M5 Polish: layout 1024–1440 (B10), Practice sessions (B16), scoring (B15), answer panel, clock vs time control
