# Chess Review

Local web app that imports my chess.com PGN exports, analyses every move I made
with Stockfish, classifies mistakes Lichess-style, explains them in plain English,
and lets me review them across all games. chess.com username: `sampleplayer`
(set in `config.toml`); every imported game is mine and my colour comes from the headers.

## Running
- Python 3.15 venv in `.venv` (Homebrew Python, not Anaconda): use `.venv/bin/python`.
- Stockfish: `/opt/homebrew/bin/stockfish` (configurable in `config.toml`).
- CLI: `.venv/bin/python -m chessreview import <files|dirs>`, `summary`
  `analyse [--limit N]` (later stages add `reclassify`, `serve`).
- Tests: `.venv/bin/pytest`.

## Layout
- `config.toml`: all tunables (engine time/workers, classification thresholds, phase rules, DB path).
- `chessreview/config.py`: loads config into dataclasses.
- `chessreview/db.py`: SQLite schema (`games`, `positions`, `mistakes`, `meta`) and helpers. DB lives in `data/` (gitignored).
- `chessreview/pgn_import.py`: PGN parsing, my colour/result, time class, dedupe (Link header, else sha1 of players+date+moves).
- `chessreview/openings.py` + `chessreview/data/openings.tsv`: names openings from the moves using the Lichess chess-openings DB (CC0), matched by position. My chess.com exports have no ECO/ECOUrl/Link headers.
- `chessreview/engine.py`: Stockfish analysis, worker pool, progress, caching. Checkmated positions store score_cp = ±10000 (MATED_CP), no engine call.
- `chessreview/classify.py`, `explain.py`, `phases.py`: mistake judgement, rule-based explanations, game phases. (Stage 3)
- `chessreview/web.py` + `templates/` + `static/`: Flask app, chessground + chess.js from jsdelivr, no build step. (Stage 4)

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
- [ ] Stage 3: classification and explanations
- [ ] Stage 4: web UI (upload, game view, mistake review)
