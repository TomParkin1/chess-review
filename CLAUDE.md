# Chess Review

Local web app that imports my chess.com PGN exports, analyses every move I made
with Stockfish, classifies mistakes Lichess-style, explains them in plain English,
and lets me review them across all games. chess.com username: `sampleplayer`
(set in `config.toml`); every imported game is mine and my colour comes from the headers.

## Running
- Python 3.15 venv in `.venv` (Homebrew Python, not Anaconda): use `.venv/bin/python`.
- Stockfish: `/opt/homebrew/bin/stockfish` (configurable in `config.toml`).
- CLI: `.venv/bin/python -m chessreview sync [--since all|3m|1m]`, `import <files|dirs>`, `summary`,
  `analyse [--limit N]`, `reclassify`, `mistakes [--limit N]`, `serve [--port 8000]`, `reset [--yes]`.
  Port 5000 is taken by macOS AirPlay Receiver, hence 8000.
- Tests: `.venv/bin/pytest`.

## Layout
- `config.toml`: all tunables (engine time/workers, classification thresholds, phase rules, DB path).
- `chessreview/config.py`: loads config into dataclasses.
- `chessreview/db.py`: SQLite schema + numbered `MIGRATIONS` (PRAGMA user_version; add a function, never edit
  old ones). Tables: `games`, `positions`, `mistakes` (every move, both sides; `is_mine`, `mark`, `accuracy`),
  `review_results`, `imports` (batches), `meta`. DB lives in `data/` (gitignored).
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
  `/api/analyse/stream` (SSE)). Upload auto-starts an `AnalysisJob`.
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
    first (`/api/threat/check`); `srs.real_game_deck()` mixes in calm positions (`coach.calm_ratio`; good moves, half with
    a real threat). Failed calm positions become `cards.kind='position'`. `review_results.mode/threat_result` record it.
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
2. [ ] "Today" workout + progress report
3. [ ] Convert won positions + essential endgames
4. [ ] Puzzle library (Lichess DB, every rating, puzzle rating)
5. [ ] Self-review before the engine in new-games sessions
