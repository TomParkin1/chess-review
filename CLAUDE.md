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
  `backup [--list]`, `restore NAME|FILE [--yes]` (opens the DB without migrating first: the way back from a broken DB),
  `simulate [--days 90] [--budget 10|20|30] [--skip 0.15]` (Today's daily load, on a temp copy of the DB).
  Port 5000 is taken by macOS AirPlay Receiver, hence 8000.
- Tests: `.venv/bin/pytest`. Endgame acceptance (real Stockfish, minutes, opt-in):
  `CHESSREVIEW_PLAYOUTS=1 .venv/bin/pytest tests/test_endgame_playouts.py`.

## Layout
- `config.toml`: all tunables (engine time/workers, classification thresholds, phase rules, DB path, backup
  rotation). `[player]` is optional (first run asks for the username).
- `chessreview/config.py`: loads config into dataclasses.
- `chessreview/db.py`: SQLite schema + numbered `MIGRATIONS` (PRAGMA user_version; add a function, never edit
  old ones). Tables: `games`, `positions`, `mistakes` (every move, both sides; `is_mine`, `mark`, `accuracy`),
  `review_results`, `imports` (batches), `cards`, `training_log` (line/Convert/endgame attempts), `meta`. DB lives in `data/` (gitignored). `db.username(conn, cfg)` = meta
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
- `chessreview/srs.py`: Leitner spaced repetition over `cards` (one per my mistake/blunder). Every attempt goes
  through `_apply`/`next_state` (Finish M2, B3): only the first attempt of a training day moves a card (Retry, "Again",
  repeated drill lines don't), and before a card is due only a failure counts. `due_cards()` = most overdue relative
  to the box interval first (library cards included); `new_cards()` (not from open sessions).
  Training day = 04:00 to 04:00 (B13): `srs.today()` and, in SQL, `srs.day_sql("at")`; never `date.today()`,
  `date(at, 'localtime')` or `at[:10]` for training days (`coach.pace` groups retries by it, E1). `introduced_on()`
  counts only `mistake`/`position` cards (what `training.new_per_day` limits). Progress weeks are ISO weeks
  (`progress._iso_week`). Streak = `training_days()` over review_results, puzzle_attempts,
  self_reviews and training_log (B9; Progress weeks too).
- `chessreview/session.py`: "new games" session = open import batches (session_done_at NULL). Every import ends in
  `session.finish_import` (B5): the first import, or one whose games mostly ended > `coach.history_days` ago, is
  history (marked done, label "(history)"); the first chess.com sync is always history. `session.expire` closes
  sessions after `coach.session_days`. Only a session's mistakes wait for it (calm positions don't).
  Summary: results, accuracy, mistake kinds, share of the clock left at blunders (+ `time_trouble_blunders`).
  Self-review wording follows the result (session.js `SR_COPY`): losses "where did it turn?", draws "where did it
  slip?", wins "where was your worst moment?" (same turning point underneath).
- `chessreview/insights.py`: per-game trends, phase accuracy, mistake kinds, clock buckets, openings, colours and
  heuristic `weaknesses()` (each with a Practice filter; ratios via `_ratio()`, base rate smoothed, capped at
  `RATIO_CAP`; "Moving too fast" trains `fast=1`, `insights.fast_move()`, in Practice and Today's focus). `static/charts.js`: SVG line/bar charts with hover
  (dataviz rules: one axis, 2px lines, rounded bar ends, validated colours `--series-you/--series-opp`).
  Practice (`/review`) accepts filters in the URL (incl. `low_clock`), overriding saved filters; the summary line
  names them ("· in time trouble", "· from one game").
- Endgame insights (E4): `insights.endgames()` (in `compute`, so filtered like the rest): games reaching an endgame
  (my first `phase='endgame'` move), by `endgame_type()` of the material there (pawn, rook, minor, rook_minor, queen,
  mixed = queen + others), `endgame_conversion` (entered at `win_before >= level.winning_chance`: won/drawn/lost),
  `saves` (entered at <= -bar), `conversion` (winning at any point). `drill_progress()` (essential endgames, won
  positions, first tries of endgame-tagged library puzzles, endgame mistake cards) only for Insights
  (`/api/insights/endgame-training`). Weakness "Endgames" (`_endgame_weakness`): endgame conversion missed >= 25% of
  >= 5 games (severity 60 x missed) or endgame moves >= 3 points below the middlegame (2.5 x gap); it replaces "The
  endgame" phase card, keeping the higher severity; trains `phase=endgame` (Today's focus: endgame mistakes + puzzles).
  Insights' Endgames card (5 tiles: reached, conversion at any point with W/D/L, endgame conversion, saves, accuracy); Puzzles
  takes `?theme=`. Progress already compares conversion between game windows.
- Sparring (E5, F9): `chessreview/spar.py` + `/spar` (`static/spar.js`, sidebar "Spar"). Start positions per game: the
  first equal-or-better middlegame short of Convert's "clearly winning" and the first equal-or-better endgame
  position on my move, newest first (`positions`); or the start (either colour) or any legal FEN. Strength =
  `level.current_rating` + offset (`OFFSETS` -200..+300, meta `spar_offset`): from `ELO_FLOOR` 1320
  `UCI_LimitStrength`/`UCI_Elo`; below it UCI_Elo 1320 with a share of moves swapped (`Strength.swap`): a random one
  of the top 5 (`TOP5_CALIBRATION`, 1320 -> ~860) then a random legal move (`RANDOM_CALIBRATION`, down to `MIN_ELO`
  ~680). Shares measured in engine matches against UCI_Elo 1320 (100 games a level, the user's equal middlegames);
  Skill Level 0 + a depth cap measured ~1240 at every depth (useless). Opt-in re-check:
  `CHESSREVIEW_CALIBRATION=1 .venv/bin/pytest tests/test_spar_calibration.py`. `LiveEngine.play` = its own 1-thread
  process (options set every move; never the judging engine). Stateless loop `/api/spar/move` ({fen, color, moves,
  uci|null: Stockfish moves first}); draws by rule end at once (`spar.outcome`). `/api/spar/finish` stores
  `spar_games` (migration 11; result from the board, else a resignation; nothing before my first move: "Leave") ->
  streak (`srs.TRAINING_SOURCES`); Delete all clears it. `/api/spar/review` {id, from}: my moves judged at full
  strength (`live.analyse` + `classify.judge`, current thresholds), `REVIEW_CHUNK` plies per request; click ->
  analysis board.
- Calculation trainer (E6, F10): `chessreview/calc.py` + `/calculate` (`static/calculate.js`, sidebar "Calculate").
  A position, a line in SAN (not played on the board: `makeBoard(..., keyboard: false)`), a question about the end:
  `best` (Lichess puzzles only, solver to move: odd depths from the stored FEN, even ones after the opponent's first
  move; typed SAN/UCI judged on the final position, any mate counts), `capture` (an attacked piece, yes/no by
  `explain.exchange`, not in check; subject picked for ~50/50), `where` (a N/B/R whose twin also moved: SAN alone
  doesn't say which went where). Sources: puzzles near my puzzle rating and the moves actually played in my games
  (never an engine line from a card position: it would spoil the card). Depth `LEVELS` 3-6 plies (meta `calc_level`,
  `calc_run`): two new questions right at the current depth -> deeper, a miss -> shallower; reviews don't move it.
  `calc_attempts` (migration 12) keeps every answer (streak, Delete all clears it); puzzles used there are left out
  of `library.pick` and kept by `library.fetch`; misses become cards kind 'calc' (key `source~plies~kind~subject`
  rebuilds the question; `due_question` skips ones whose puzzle is gone) and come back first. Today: focus row
  `calc` flag (weakness kind in `coach.CALC_KINDS`, `PLAN_VERSION` 3) -> link "/calculate?from=today", not counted.
- Clock rules (Finish M5, F7) live in `report.py`: shares of the estimated game duration `game_budget_ms` = base +
  40 × increment (none for daily/unknown). Time trouble < 10% (`is_low_clock`; 60 s at 10+0, 18 s at 3+0, 130 s at
  15+10), a very fast move < 1/120 (5 s at 10+0), buckets `CLOCK_BUCKETS` 30/10/3%. `insights.in_time_trouble(clocks,
  tc, ply)` is the one check (Insights, weaknesses, session summary, Practice's `low_clock`, Today's focus).
- `chessreview/repertoire.py`: repertoire tree per colour from my games (EPD-keyed, first MAX_PLY plies);
  repertoire move = my most played (>= 2 games) unless usually judged bad -> engine best ("problem").
  Drill lines + due exercises; line cards are `cards.kind='line'`, key `<colour>:<epd>` (srs.record_line).
  UI: `/openings` (`static/openings.js`: Explore tab + Drill tab). `build()` is memoised (F17): last `CACHE_SIZE`
  trees keyed on (database file, colour, family, `_stamp`: games count, max id, max analysed_at, max mistakes rowid);
  callers must not modify a tree; restore calls `clear_cache()`. `/api/openings/node` answers `{reached: false}`
  (200) for a position not in my games.
- `static/puzzle.js` + `templates/_puzzle.html`: the shared puzzle used by Practice (`/review`), Train, Session.
  Lean answer panel (Finish M5): result line, then Next/Retry/Analyse/Open game, then one `#explanation` block (what
  happened in the game: verdict, evals unless the text already quotes them, why; library: themes + rating change),
  `#best-explanation`, and the best line with the arrow legend in its header. Feedback and status chip hide at the end.
  `common.js`: `isSolved()` (solved_with_help counts as solved everywhere, B15), `itemKey()` (`lib:<id>` or
  `<game>:<ply>`, Today's "Again"), `threatCheckPref()`/`setThreatCheckPref()`. Session tallies (Today, Puzzles,
  Session) count each item once (a Retry doesn't count again; Today's "Again" is its own attempt); the server still
  records every attempt (`coach.pace` needs them). Every async step checks `puzzle !== p` after an await, and
  `show()` dismisses an open promotion picker (`board.dismissPromotion`, its promise resolves null).
- Accessibility (E1): `common.clickable(node)` (tabindex, role, Enter/Space) is applied by `el()` to anything with an
  `onclick` that isn't a native control; feedback/prompt boxes are `aria-live`. Boards created with `viewOnly: false`
  get a keyboard move box (`board.js addKeyboardMoves`, `.kbd-move`, hidden until focused): SAN or UCI, played
  through the board's own `movable.events.after` only when the board allows that move.
- `chessreview/report.py`: chess.com-style move marks (documented approximations), Lichess accuracy, time spent,
  clock rules relative to the time control.
- `chessreview/pgn_import.py`: PGN parsing, my colour/result, time class, dedupe (Link header, else sha1 of players+date+moves).
- `chessreview/openings.py` + `chessreview/data/openings.tsv`: names openings from the moves using the Lichess chess-openings DB (CC0), matched by position. My chess.com exports have no ECO/ECOUrl/Link headers.
- `chessreview/engine.py`: Stockfish analysis (MultiPV 2, `ANALYSIS_VERSION`; older games re-analysed), worker pool, progress, caching. Checkmated positions store score_cp = ±10000 (MATED_CP), no engine call.
- `chessreview/classify.py`: Lichess win-chance judgement per move (both sides) -> `mistakes` table, plus marks and game accuracy. My-mistake queries must filter `is_mine = 1`. `ensure_classified()` rebuilds
  automatically when config thresholds or `explain.VERSION` change (fingerprint in `meta`). Forced moves and the engine's
  own best move are always "good". `ensure_classified()` is blocking (CLI, tests); web requests and `serve` call
  `refresh()` (F17): new games are classified on the spot (under `_lock`), but a full rebuild of existing rows runs in a
  background thread (`start_rebuild`, own connection, progress in `REBUILD`, shown in Settings) while requests keep
  reading the stored rows (each game is replaced in one transaction). The very first classification stays inline.
- `chessreview/explain.py`: ordered list of rule functions (`RULES`), first match wins. Bump `VERSION` when changing output.
  v7 (Finish M3, P1–P10): stored lines are 0.3 s PVs, so material claims are checked against the eval
  (`_gain_ok`/`_loss_ok`, `SLACK` 150 cp; `_understated`: eval far above the line's material -> no amount, or "It keeps
  you at +x"). `play_line` settles a trailing capture with a static exchange (`exchange()`), or drops it when it loses on
  the spot (PV-tail noise). `MoveContext.prev_capture` (opponent's last capture square) + `recapture` (also: best captures
  on the same square) count losses from after my capture. The bigger half leads: `bigger_miss` (best wins >= what I lose)
  before the loss rules, `_also_missed` adds the other half; `_you_lose` adds the eval drop when material explains
  < half of it. Lines run through the capture that completes the claim (`LineResult.plies`, max 12). `missed_fork`
  (my first two moves, forker safe), `exposed_king` only when my king must move or is checked again, `_why_loose`
  (moved piece was shielding/guarding). `MoveContext.min_loss` = `classify.Thresholds.min_loss` (1 pawn at 2000+, else 2).
  Regression tests per audit position: `tests/test_explain_report.py` (stored rows embedded).
- `chessreview/phases.py`: opening/middlegame/endgame.
- `chessreview/web.py`: Flask app factory `create_app(cfg)`; pages (`/`, `/game/<id>`, `/review`) are thin shells,
  data comes from the JSON API (`/api/upload`, `/api/progress`, `/api/analysis/start|stop`, `/api/games`,
  `/api/game/<id>`, `/api/mistakes?filters` incl. `status` & `last`, `/api/judge`, `/api/review/result`,
  `/api/analyse/stream` (SSE), `/api/account`, `/api/backups` (+ `/restore`, `/export`), `/api/today/refresh`).
  `/api/mistakes?...&limit=N` (Finish M5, B16/F17): `keys` of every match in order ("<game>:<ply>", one light query),
  `total`, the first N `items`, and `options` = each facet (`FACETS`: opening, phase, result, colour, time, type) with
  the values that have matches under the *other* filters, with counts (a chosen value with none stays, count 0);
  without `limit` all items as before. `/api/mistakes/items?keys=` (<= 50) builds the next Practice session.
  Practice (review.js) = sessions of `SESSION_SIZE` 10 from the key snapshot, a summary card after each, "Next 10",
  "That's all N" + Start over at the end (no wrap). `/game/<id>` knows an unknown id itself (`GAME_FOUND`).
  Upload auto-starts an `AnalysisJob`. Uploads, syncs and restores share one `import_lock` (B8); `import_game`
  inserts with `ON CONFLICT(dedupe_key) DO NOTHING` (a race counts as a duplicate) and batches finish in `finally`.
- `chessreview/live.py`: `LiveEngine` = two long-lived Stockfish processes (judging tries, live analysis stream).
  The stream runs on a worker thread feeding a queue (a paused SSE generator must never hold the engine lock).
  `serve` closes it in a finally block, tests close it in fixtures, otherwise Python cannot exit.
- Adaptive thresholds (default; `classification.adaptive`, overridable from Settings via meta `thresholds_mode`):
  `classify.thresholds_for(rating)` bands by the mover's rating in that game; `/api/judge` uses my rating in the game.
  Switching mode changes the classification fingerprint, so everything is rebuilt in the background (`refresh`).
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
  - Real-game mode (`item.mode = 'blind'`; the toggle is "Threat check first" in Practice and Today's reviews, one
    localStorage preference `threat-check`; Today's default `training.blind`): no badge/arrow; threat step
    first (`/api/threat/check`); Today's real-game block mixes in `srs.calm_positions()` (`coach.calm_ratio`; good moves,
    half with a real threat). Failed calm positions become `cards.kind='position'`. `review_results.mode/threat_result` record it.
- v3 Today + Progress (Milestone 2):
  - `coach.py`: `today_plan()` builds the day's blocks (reviews, convert = the endgame to play, endgame puzzles,
    real-game = new cards + calm positions, focus = my #1 weakness from `insights.weaknesses(last=100)`; no opening
    lines since E2) and freezes it in meta `today_plan`; an item is done once it has a review result today
    (`attempted_today`); the convert block once its planned `key`'s card was played today (`_block_done`).
    API `/api/today`, `/api/today/<block>`; the Train page (`/train`, sidebar "Today") is the block checklist + runner.
    Same-day rebuilds (Finish M1, B6): plan `basis` = analysed-game count; rebuilt on `refresh` (POST
    `/api/today/refresh`, or a new budget), when the plan has no `budget` yet or another `version` (`PLAN_VERSION`,
    2 = E2), when an item can't be served
    (`_servable`: Delete all, restore, missing puzzle) or when the basis changed and nothing was tried yet; otherwise
    `stale`/`new_games` -> "Refresh plan" prompt. `_rebuild` keeps today's tried items (their time counts against the
    budget, endgame items against the endgame share) and a played convert block; unknown blocks (opening) go.
  - `progress.py`: last N games vs the N before (`coach.progress_window`, game windows not weeks), metrics at my current
    level (`level.winning_chance` bands for conversion), rolling series, kinds per game, weekly training. `/progress`,
    `/api/progress/report`, summary sentence on Home. (Route fn for `/api/progress` is `analysis_progress`.)
- v3 Convert (Milestone 3): `convert.py` + `data/endgames.toml` (15 Stockfish-checked endgames, 3 levels; a level
  unlocks when the one before is solved). Won positions = first position per game at/above `level.winning_cp` (by
  rating band) held on my next move; lost games first. `state()` decides won/failed per move. A win ends at its
  milestone, `Task.finish` (Finish M4, P11; a high eval alone never does): `mate`, `promote` (I promoted and am still
  at +7 or mating after the reply), `bare_king` (Stockfish had more at the start, now only its king; I keep a Q/R); all
  but `mate` also end at mate <= 5. Converts (`resign`): those, or `Task.resign_cp` = max(+7, raw start cp + 3 pawns;
  None for a mate start: `as_cp` caps at +10) on 3 of my moves in a row. Any draw fails a win goal; convert fails
  below 40% of the bar; draw goals (`hold`) hold for 15 moves. `finish_text()` is shown under the prompt.
  `tests/test_endgame_playouts.py`: strong moves win/hold all 15, three merely safe moves finish none. Playouts are stateless: the client sends all moves (`/api/play/move`, `/api/play/hint`),
  Stockfish = `LiveEngine.analyse` at full strength. Cards: `srs.record_key(kind 'convert'|'endgame', key)`.
  Page `/convert` (`static/convert.js`, eval hidden until the end); Today gets a "Convert" block.
- Tablebase (E3): `chessreview/tablebase.py` asks the Lichess API (`tablebase.lichess.ovh/standard`) for positions with
  <= 7 pieces and no castling rights (`eligible`); `probe(conn, board)` -> `TbResult` (WDL from the side to move: 2 win,
  1 cursed win, 0 draw, -1 blessed loss, -2 loss; cursed/blessed count as almost draws since playouts apply the 50-move
  rule) or None (ineligible, game over, Settings off = meta `tablebase` 'off', or any failure -> Stockfish). Cache: LRU
  in memory + `tablebase_cache` (migration 10, EPD + halfmove clock -> compact JSON: the API's answer depends on the
  clock, a win turns "cursed" near the 50-move limit), so played endgames work offline. 3 s timeout;
  after a failure no requests for `BACKOFF` 30 s (`RATE_LIMIT_BACKOFF` 60 s after 429). `TbResult.best` = quickest win
  (a mating move first: the API gives it no DTM; then DTM, else DTZ) / longest defence; `choose()` keeps the engine's
  move among equal draws (defender reply and hints). `/api/play/move`: defender = `choose`, `reply_source`;
  `convert.state(..., tb=(start, before my move, after it))` (WDL from my side; start/before None when not in the
  tablebase): the result after my move, when known, replaces the 40%-slip and -60% losing eval rules; below the goal it
  fails at once: "That move throws away the win" only when the position before my move is known to be won, else "The
  win is gone" (a game position entering the tablebase already drawn); a start the tablebase calls no better than the
  goal is judged by the eval. Response `tablebase` {wdl, before_wdl, verdicts}; the Convert lesson blames the first
  move with a known drop, else the biggest eval drop. `/api/play/hint` = `choose` (`source`), so "Watch Stockfish play
  it" is perfect play too. Convert page `#play-source` says so. Settings
  card toggle (`/api/settings/tablebase`). Tests never touch the network: `tests/conftest.py` replaces
  `tablebase.fetch`; the opt-in playouts run every endgame twice (Stockfish alone, real tablebase: rate limits waited out, every hint
  and reply asserted to come from the tablebase).
- v3 puzzle library (Milestone 4): `library.py` streams the Lichess puzzle DB (`lichess_db_puzzle.csv.zst`, ~300 MB,
  stdlib `compression.zstd`) and keeps a reservoir sample per (100-point bucket, theme in `THEMES`), 400-3100, well-tested
  only (~19k puzzles) in `puzzles`; attempts in `puzzle_attempts`. Puzzle rating (meta) starts at my chess.com rating,
  Elo update, first try only: score 1 clean, 1/2 solved with help (hint or a wrong try), 0 otherwise (`SCORES`);
  K = max(40 -> 16 over 100 puzzles, 120 × 0.8^n) so the first puzzles correct a bad seed (provisional rating); `pick()` chooses unseen puzzles around it. Failed puzzles become
  `cards.kind='library'` (due ones join Today's Review). `THEMES` gained queen/bishop/knight/queenRook endgames (E2;
  meta `puzzle_library_themes` stamps the sampled set, `outdated()` -> Settings suggests an update). `KIND_THEMES` maps my mistake kinds to themes; Today's Focus
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
  - Today continues into Convert (`?from=today&key=` -> "Back to Today" there; Openings no longer, E2).
  - `analysis.start(fen, {history})`: moves that led to the position (greyed, ◀ steps back through them).
  - Convert after a failure: biggest win-chance drop of my moves + Stockfish's move there, drawn on the position
    before that move (B14; "Final position" toggles), "Try again from there" (counts as solved_with_help at best;
    the prompt gives the eval there), "Watch Stockfish play it" (hint + play/move loop, nothing recorded).
- Today's time budget (Finish M2, F4; `build_plan`): 10/20/30 min (meta `today_budget`, POST `/api/today/budget`,
  default `coach.budget_minutes`). Endgames first (E2): `ENDGAME_SHARE` 30% of the budget = the endgame to play
  (`convert.next_endgame`: due essential endgame > due won position > next essential in unlock order > untried won
  position; 2 min essential / 3 min convert, `drill_seconds`; none before the first analysed game) + the endgame
  puzzle block (`_endgame_items`: due endgame cards (endgame-phase mistakes, endgame-tagged library cards; they leave
  Review; all of them may fill the block: capped at half, they piled up, 30-min backlog 55 -> 104 in `simulate`), new
  endgame mistakes from all games (part of the new-card allowance) up to half, `library.pick_endgames` for the rest:
  `ENDGAME_ROTATION` over `ENDGAME_THEMES`, rook most; tag "endgame"). Places it can't fill go back to the other
  blocks. `simulate` reports the endgame share and the days with Lichess endgame puzzles. The rest = puzzle slots at `coach.pace()` (my time per attempt x
  attempts per puzzle, last 200). Reviews <= `review_share` of the slots, all of them when more is due than fits; new cards only while due
  < `new_share` of the slots (focus mistakes never studied count too); rest half real-game, half focus; what focus
  can't fill goes to waiting reviews, then calm positions. A rebuild counts tried items' time as spent.
  `chessreview/simulate.py` runs the real planner day by day on a copy (`simulate` CLI; test in
  `tests/test_training_integrity.py`). Double clicks (B12): puzzle.js `advance()` (once per puzzle, 400 ms settle) and
  `common.js` `busy()` for start buttons.
- Design rule: nothing is tuned to one rating. Derive from the engine, the current rating's band, or my rolling data.
- Chessground `events.after` passes (orig, dest, metadata): always wrap handlers as `(o, d) => fn(o, d)`.
- `classify.judge` applies Lichess' mate rules (lost/allowed forced mate) before win chances; bump
  `CLASSIFY_VERSION` when judge logic changes. A lost mate longer than `LONG_MATE` (5) while still >= +7 is an inaccuracy
  (P8: the 0.3 s search after the move rarely sees the slower mate); `live.judge` counts a try that keeps +10 in a long-mate
  position as good (`keeps_long_mate`).
- Review page = Lichess-style "learn from your mistakes" (`static/review.js` state machine: solving → checking →
  wrong (snap back + spoiler-free explanation via `explain(..., reveal_best=False)`) / right (auto-play reply, continue
  for `playout_moves`) → done (explanation, best-line stepper)). Hints: text from `explain.HINTS`, then piece highlight.
  Chessground boards that take moves must be created with `viewOnly: false` (listeners bind only at creation).
- Layout (Finish M5, B10): `.layout` = `minmax(0, board + eval bar 32px) minmax(var(--side-min), 1fr)`: the board fills
  the window height (100vh - `--board-above` - `--board-below`: main's padding, plus Practice/Puzzles' filter bar above
  or the `.controls` row below; max 1100 px; no bar space without an `.evalbar`, i.e. Openings) but shrinks before the
  side panel goes under `--side-min` (400 px, 360 under 1100 px); the sidebar
  is icons-only at <= 1100 px. Grids holding names use `minmax(0, 1fr)` so text ellipsizes instead of being clipped.
  Browser check: scratchpad `layout.mjs` (no sideways scroll, no clipped box, 1024/1280/1440/1920).
- UI preferences: review is always interactive (puzzle first); board sized from window height for desktop; chess.com
  Lichess "cburnett" pieces (from chessground) on a brown/cream board (CSS in `style.css`).
- `static/board.js`: makeBoard + settings (localStorage, applied via `html[data-board|data-pieces]`), square badges,
  promotion picker, WebAudio sounds. `static/icons.js`: our own SVG mark badges (`MARKS`, `markIcon`).
  `keepRealBounds` (Finish M5, B17): chessground caches the board's rect; measured while hidden (0×0) every arrow is
  NaN, and its position goes stale when content above moves (Practice's filter form). So a 0×0 measure falls back to
  the last real one, the rect is re-measured on every mousedown/touchstart, and the guard is re-applied whenever
  chessground rebuilds `state.dom` (it does on every flip). Never call `redrawAll()` (it wipes the badge layer).
  `evalbar.js` labels are short ("1.2", "M3") to fit the 24 px bar.
- `static/game.js` = Game Report (coach, accuracy, mark counts, key moments, eval graph + time chart with hover).
- `static/evalbar.js` (vertical eval bar) and `static/analysis.js` (free analysis board: variation + SSE live lines,
  closes its EventSource on `done` so it doesn't auto-reconnect) are shared by game view and review.
- `chessreview/templates/` + `chessreview/static/`: plain ES modules, no build step. chessground 9.2.1 and
  chess.js 1.4.0 via an import map from jsdelivr (see `base.html`). `common.js` has shared helpers.
- Error and empty states (Finish M1, B11/F6): `/api/*` errors are always JSON `{error}` (web.py error handlers,
  500s logged). Bad input is a 400/404, never a 500 (E1): request bodies through `web._body()` (a JSON object or 400),
  numbers through `_int(data, key, default)`, colours `_color()`, FENs `_board()`, move lists `_moves()`;
  `convert.task_for` returns None for a malformed key or a position where it isn't my move; `/api/review/result`
  404s unless the position is my analysed move. `chesscom.Unavailable` (an OSError) = a non-JSON reply (502). `common.js`: `api()` throws `ApiError` (`status`, 0 = server unreachable), `errorState()`,
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
2. [x] M2 Training integrity: one promotion per card per day (B3), load budget (F4), streak (B9), 04:00 day (B13),
   double-click guards (B12), sessions never block Today (B5)
3. [x] M3 Correct lessons: explanation rules P1–P10, P13 MIN_LOSS by band; lines through the gain. Deep check (scratchpad,
   Stockfish 1.5 s/4 threads/MultiPV 3): wrong reasons 13/40 -> 0, held-out 2/20 -> 0. Deeper re-check of card positions
   deferred (user's choice): it would fix best moves the 0.3 s search gets wrong (e.g. game 203 ply 29).
4. [x] M4 Endgames that teach: finish rules (P11: promote / bare_king / mate / hold, converts resign 3 pawns above
   the start), real Vancura position + tip (P12), Convert lesson on the slip position + resumed prompt (B14).
   Migration 9 reset the endgame cards "solved" under the old 3-move rule.
5. [x] M5 Polish & finish: layout 1024–1920 (B10), Practice sessions + facets (B16), scoring + puzzle rating rules
   (B15), lean answer panel, self-review copy by result, "Threat check first", chessground bounds (B17, plus stale
   position after layout shifts), clock vs time control (F7), performance (F17: 6.1 s / 11.6 MB -> 0.08 s / 113 KB for
   Practice at 1,928 games). Found by the regression: session self-review race, drill Stop race, 404 console noise.
   Browser regression (scratchpad `regress.mjs` + `run-width.sh`, fresh copies per width): every flow of the audit's
   Method section at 1280, 1440 and 1920, zero console errors / failed requests / layout problems.
The Finish roadmap is complete.

## E roadmap (audit of 10 Nov 2026; plan approved; one milestone at a time, stop after each for testing)
Openings are practised on Chessly: Today drops opening lines (the /openings page stays) and leans on endgames.
1. [x] E1 Bug squash: bad input -> 400/404 (`_body`/`_int`/...), races (job start lock, Delete all/restore vs
   imports and rebuilds, live cache lock, card insert, puzzle rating), time-trouble/fast weakness ratios + `fast`
   filter, pace by training day, new_per_day scope, ISO weeks, partial first sync = history, chess.com 5xx/non-JSON;
   frontend: Openings drill Stop + stale replies, promotion picker per puzzle, stale judge replies, once-per-item
   tallies, analysis/convert/Today/Insights/Puzzles races, Today badge + games table at 1024 px, keyboard moves,
   clickable/aria-live. Browser regression: scratchpad `regress/run.sh` (fresh DB copy, port 8765, `drive.mjs`:
   every page at 1024/1280/1440/1920 + drill Stop, rapid Openings clicks, a typed move in Practice).
2. [x] E2 Today without openings (old plans rebuilt via `PLAN_VERSION`; Openings' Today mode gone); endgames first
   (~30%: the endgame to play + endgame puzzle block), convert block done only for its planned key; library endgame
   types. `simulate --days 90`: 0 days over budget at 10/20/30, backlog as before E2 (max 55 = the starting
   backlog), endgames 27-29% of planned time, Lichess endgame puzzles on ~2/3 of days at 10 min, all at 20/30.
3. [x] E3 Lichess tablebase API (<= 7 pieces) for endgame play: perfect defence, exact verdicts, cached
4. [x] E4 Endgame insights (conversion rate, results by endgame type, saves, drill progress, "Endgames" weakness)
5. [x] E5 Sparring vs a rating-limited Stockfish from my positions (F9)
6. [x] E6 Calculation trainer (F10)
The E roadmap is complete.
Still deferred: the deeper re-check of card positions (M3); idempotent old migrations (rule: never edit old ones;
new migrations use `conn.execute` per statement, not `executescript`, so they stay atomic).
