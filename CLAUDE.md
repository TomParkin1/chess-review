# Chess Review

Local Flask + CLI app: imports chess.com games (API or PGN), analyses them with Stockfish, explains the player's
mistakes and trains on them. How it fits together: [docs/architecture.md](docs/architecture.md).

## Commands
- Install: `python -m venv .venv && .venv/bin/pip install -e ".[dev]"` (Python 3.14+, Stockfish on PATH or `STOCKFISH_PATH`)
- Run: `.venv/bin/chessreview serve` (port 8000), CLI `.venv/bin/chessreview --help`
- Try it: `chessreview import samples --as "Sample Player" && chessreview analyse`
- Test: `.venv/bin/pytest` (about 45 s with Stockfish); `CHESSREVIEW_FAKE_ENGINE=1 .venv/bin/pytest` (about 13 s)
- Opt-in, real Stockfish, minutes: `CHESSREVIEW_PLAYOUTS=1 .venv/bin/pytest tests/test_endgame_playouts.py`,
  `CHESSREVIEW_CALIBRATION=1 .venv/bin/pytest tests/test_spar_calibration.py`
- Lint and types (CI runs all of these): `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`, `.venv/bin/mypy`

## Conventions
- Scores are stored from White's point of view; convert to the player's view when classifying.
- Engine analysis, classification and explanation stay separate: classification is re-derived from stored
  evals, so never re-run the engine to change thresholds. Bump `explain.VERSION` when explanation output changes
  and `classify.CLASSIFY_VERSION` when judging logic changes.
- Schema changes are new functions in `db.MIGRATIONS` (never edit old ones; one `conn.execute` per statement).
  Call `backup.make(conn, "before-<what>")` before any new destructive operation.
- Training days run 04:00 to 04:00: use `srs.today()` / `srs.day_sql()`, never `date.today()`.
- Queries about the player's own moves filter `mistakes.is_mine = 1`.
- Nothing is tuned to one rating: derive values from the engine, the rating band or the player's own data.
- Web: bad input is a 400/404, never a 500 (parse with `web.common` helpers); `/api/*` errors are JSON `{error}`.
- Front end: plain ES modules, no build step. Chessground boards that take moves need `viewOnly: false`;
  wrap `events.after` handlers as `(o, d) => fn(o, d)`; never call `redrawAll()`.
- Tests never touch the network or the real database (`tests/config.toml`, `no_network` fixture).
- Python, type hints everywhere (mypy `disallow_untyped_defs`), ruff at 120 columns, British English.
- Git: Conventional Commits, one focused PR per change, merge commits (not squash). Never commit `data/`.
