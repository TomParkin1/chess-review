# Chess Review

A local web app that turns your chess.com games into training. It imports your games, analyses every
move you played with Stockfish, labels your mistakes the way Lichess does, explains them in plain English,
and builds a daily workout from them.

Everything runs on your own machine. Your games and progress live in a SQLite database in `data/`.

## Features

- **Import**: sync straight from chess.com's public API, or upload PGN exports. Duplicate games are skipped.
- **Analysis**: Stockfish checks every position in parallel. Results are cached, so each game is analysed once.
- **Game Report**: accuracy, chess.com-style move marks, key moments, an eval graph and a time chart.
- **Mistakes, explained**: inaccuracies, mistakes and blunders judged by drop in win chance. The thresholds
  adapt to your rating. Each one comes with a plain-English reason (hanging piece, missed fork, missed mate, ...).
- **Practice**: replay your own mistakes as puzzles, with hints, a "threat check first" mode and an analysis
  board. Filter by opening, phase, result, colour, time control, kind of mistake or time trouble.
- **Today**: a daily plan sized to 10, 20 or 30 minutes. About 30% of it is endgames: one endgame to play
  against Stockfish (an essential endgame, or a won position from your games) and endgame puzzles (your own
  endgame mistakes plus Lichess rook, pawn, queen and minor-piece endgames near your puzzle rating). The rest
  mixes spaced-repetition reviews, new mistakes, calm "real game" positions and your current #1 weakness.
  Opening lines aren't part of Today (practise them on the Openings page).
- **New-games session**: after an import, first guess where each lost game turned, then review it.
- **Openings**: your repertoire tree for each colour, built from your games, with drills.
- **Convert**: play won positions from your games out to the win against Stockfish, plus 15 essential endgames.
  With 7 pieces or fewer the free [Lichess tablebase](https://tablebase.lichess.ovh) takes over: perfect defence,
  perfect hints, and an exact verdict the moment a move throws a win away. Answers are kept, so endgames you've
  played work offline; Settings can switch it off.
- **Spar**: play positions from your own games (or the start, or any FEN) out against Stockfish at about your
  rating, adjustable from -200 to +300, then review your moves at full strength.
- **Calculate**: read a line of moves without seeing them played, then answer about the position at the end
  (the best move, whether a piece can be won, where a piece went). Lines get longer as you get them right.
- **Puzzle library**: about 19k Lichess puzzles (optional download), with your own puzzle rating.
- **Insights and Progress**: trends, accuracy by phase, clock use, openings, endgames (results by type of endgame,
  how often you convert winning endgames and save losing ones, your endgame training) and your top weaknesses
  (each with "Train this", including time trouble and moving too fast), comparing your last games with the
  ones before.
- **Keyboard friendly**: press Tab to reach the board's move box and type a move (`Nf3`, `O-O`, `e2e4`),
  then Enter. Lists and rows are reachable with Tab and open with Enter.
- **Backups**: automatic daily and weekly copies, plus one before any destructive operation, restorable from Settings.

## Requirements

- Python 3.14+ (the puzzle library uses the stdlib `compression.zstd`; developed on 3.15)
- [Stockfish](https://stockfishchess.org/) (`brew install stockfish` on macOS)

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Check `config.toml`. If Stockfish isn't at `/opt/homebrew/bin/stockfish`, set `[engine] path`.
The chess.com username under `[player]` is optional: the app asks for it on first run.

## Running

```sh
.venv/bin/python -m chessreview serve --port 8000
```

Then open http://localhost:8000. On first run, the app asks for your chess.com username and how much
history to sync. (Port 5000 is taken by AirPlay Receiver on macOS, hence 8000.)

### Command line

```sh
.venv/bin/python -m chessreview sync [--since all|3m|1m]   # fetch games from chess.com
.venv/bin/python -m chessreview import <files|dirs>        # import PGN files
.venv/bin/python -m chessreview analyse [--limit N]        # run Stockfish on new games
.venv/bin/python -m chessreview summary                    # games in the database
.venv/bin/python -m chessreview mistakes [--limit N]       # list your mistakes
.venv/bin/python -m chessreview reclassify                 # re-judge stored evals after changing thresholds
.venv/bin/python -m chessreview puzzles                    # download the Lichess puzzle library
.venv/bin/python -m chessreview backup [--list]            # make or list backups
.venv/bin/python -m chessreview restore NAME|FILE [--yes]  # restore a backup
.venv/bin/python -m chessreview reset [--yes]              # delete all data
```

## Configuration

`config.toml` holds every tunable setting: engine time and workers, classification thresholds, game
phases, training budgets, backup rotation and the database path. Changing the thresholds doesn't
re-run the engine. Stored evaluations are re-classified automatically.

## Tests

```sh
.venv/bin/pytest
```

The endgame acceptance tests play real games against Stockfish. They take a few minutes and are opt-in:

```sh
CHESSREVIEW_PLAYOUTS=1 .venv/bin/pytest tests/test_endgame_playouts.py
```

## Project layout

```
chessreview/
  engine.py, live.py        Stockfish analysis (batch worker pool, live judging)
  classify.py, explain.py   mistake judgement and plain-English explanations
  srs.py, coach.py          spaced repetition and the daily plan
  repertoire.py, convert.py openings and endgame training
  library.py                Lichess puzzle library
  tablebase.py              Lichess endgame tablebase (<= 7 pieces), cached
  insights.py, progress.py  statistics
  web.py                    Flask app and JSON API
  static/, templates/       front end (plain ES modules, no build step)
tests/
config.toml
```

## Credits

- [Stockfish](https://stockfishchess.org/) for analysis
- [python-chess](https://python-chess.readthedocs.io/), [chessground](https://github.com/lichess-org/chessground)
  and [chess.js](https://github.com/jhlywa/chess.js)
- Opening names from [lichess-org/chess-openings](https://github.com/lichess-org/chess-openings) (CC0).
  Puzzles from the [Lichess puzzle database](https://database.lichess.org/#puzzles) (CC0).
  Endgame tablebase answers from the [Lichess tablebase API](https://github.com/lichess-org/lila-tablebase) (Syzygy).
- Mistake thresholds and accuracy formula follow Lichess.
