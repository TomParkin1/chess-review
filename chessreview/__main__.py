"""Command line interface.

    python -m chessreview sync [--since all|3m|1m]
    python -m chessreview import FILE_OR_DIR [...] [--as NAME]
    python -m chessreview summary
    python -m chessreview analyse [--limit N]
    python -m chessreview reclassify
    python -m chessreview mistakes [--limit N]
    python -m chessreview serve [--port 8000]
    python -m chessreview puzzles
    python -m chessreview reset [--yes]
    python -m chessreview backup [--list]
    python -m chessreview restore NAME_OR_FILE
    python -m chessreview simulate [--days 90] [--budget 10|20|30] [--skip 0.15] [--seed 1]
"""

import argparse
import sqlite3
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from . import backup, db
from .classify import ensure_classified, refresh
from .config import ConfigError, effective_workers, load_config
from .engine import AnalysisJob, StockfishNotFound, count_pending, find_stockfish
from .pgn_import import import_paths


def cmd_sync(conn: sqlite3.Connection, cfg, args) -> None:
    from . import chesscom
    user = db.username(conn, cfg)
    if not user:
        sys.exit("Set your chess.com username first (in the web app, or [player] username in config.toml).")
    last = db.get_meta(conn, chesscom.LAST_MONTH_KEY)
    print(f"Syncing chess.com games of {user} "
          f"({'since ' + last if last else 'first sync: ' + args.since})...")
    result = chesscom.sync(conn, cfg, since=args.since,
                           progress=lambda i, n, month: print(f"  {month} ({i + 1}/{n})"))
    s = result.stats
    print(f"Games seen: {s.games_seen}, new: {s.imported}, duplicates: {s.duplicates}")
    for reason, n in sorted(s.skipped.items()):
        print(f"Skipped:    {n} ({reason})")
    if s.imported:
        print("Run 'analyse' (or open the web app) to analyse them.")


def _set_player(conn: sqlite3.Connection, name: str) -> None:
    with conn:
        if name:
            db.set_meta(conn, db.USERNAME_KEY, name)
        else:
            conn.execute("DELETE FROM meta WHERE key = ?", (db.USERNAME_KEY,))


def cmd_import(conn: sqlite3.Connection, cfg, args) -> None:
    before = db.username(conn, cfg)
    switching = bool(args.player) and args.player.lower() != before.lower()
    if switching:
        if before and conn.execute("SELECT 1 FROM games LIMIT 1").fetchone():
            sys.exit(f"The player is already {before!r}; change it in Settings to import {args.player!r}'s games.")
        _set_player(conn, args.player)
    elif not before:
        sys.exit("Whose games are these? Pass --as YOUR_NAME (as it appears in the PGN), "
                 "or set your chess.com username in the web app.")
    stats = import_paths(conn, [Path(p) for p in args.paths], cfg)
    if switching and not (stats.imported or stats.duplicates):
        _set_player(conn, before)      # none of their games here (a typo?): keep the player as it was
        print(f"No games of {args.player!r} found, so the player is unchanged.")
    print(f"Files read:   {stats.files}")
    print(f"Games seen:   {stats.games_seen}")
    print(f"Imported:     {stats.imported}")
    print(f"Duplicates:   {stats.duplicates}")
    for reason, n in sorted(stats.skipped.items()):
        print(f"Skipped:      {n} ({reason})")


def _wdl_table(rows, label: str) -> None:
    print(f"\n{label:<28} {'games':>6} {'win':>5} {'draw':>5} {'loss':>5} {'score':>6}")
    for r in rows:
        n = r["n"]
        score = (r["w"] + 0.5 * r["d"]) / n * 100 if n else 0
        print(f"{str(r['k'])[:28]:<28} {n:>6} {r['w']:>5} {r['d']:>5} {r['l']:>5} {score:>5.0f}%")


def _wdl_query(conn: sqlite3.Connection, group_by: str, limit: int | None = None):
    sql = f"""
        SELECT {group_by} AS k, COUNT(*) AS n,
               SUM(my_result = 'win') AS w, SUM(my_result = 'draw') AS d,
               SUM(my_result = 'loss') AS l
        FROM games GROUP BY {group_by} ORDER BY n DESC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def cmd_summary(conn: sqlite3.Connection, cfg, args) -> None:
    total = conn.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM games").fetchone()
    if not total[0]:
        print("No games imported yet. Run: python -m chessreview import <files>")
        return
    print(f"Player: {db.username(conn, cfg)}")
    print(f"Games:  {total[0]}  ({total[1]} to {total[2]})")
    _wdl_table(_wdl_query(conn, "'all'"), "Overall")
    _wdl_table(_wdl_query(conn, "my_color"), "By colour")
    _wdl_table(_wdl_query(conn, "time_class"), "By time class")
    _wdl_table(_wdl_query(conn, "opening_family", limit=args.openings), "Top openings")
    _mistake_summary(conn, cfg)


def _mistake_summary(conn: sqlite3.Connection, cfg) -> None:
    ensure_classified(conn, cfg)
    analysed = conn.execute("SELECT COUNT(*) FROM games WHERE analysed_at IS NOT NULL").fetchone()[0]
    if not analysed:
        print("\nNo games analysed yet. Run: python -m chessreview analyse")
        return
    print(f"\nMy moves in {analysed} analysed games")
    print(f"{'':<14} {'moves':>6} {'inacc.':>7} {'mistake':>8} {'blunder':>8} {'per game':>9}")
    rows = conn.execute("""
        SELECT m.phase AS k, COUNT(*) AS n,
               SUM(judgement = 'inaccuracy') AS i, SUM(judgement = 'mistake') AS m,
               SUM(judgement = 'blunder') AS b, COUNT(DISTINCT game_id) AS g
        FROM mistakes m WHERE is_mine = 1 GROUP BY m.phase
        UNION ALL
        SELECT 'all', COUNT(*), SUM(judgement = 'inaccuracy'), SUM(judgement = 'mistake'),
               SUM(judgement = 'blunder'), COUNT(DISTINCT game_id) FROM mistakes WHERE is_mine = 1
    """).fetchall()
    order = {"opening": 0, "middlegame": 1, "endgame": 2, "all": 3}
    for r in sorted(rows, key=lambda r: order.get(r["k"], 9)):
        per_game = (r["m"] + r["b"]) / analysed
        print(f"{r['k']:<14} {r['n']:>6} {r['i']:>7} {r['m']:>8} {r['b']:>8} {per_game:>9.1f}")
    print("(per game = mistakes + blunders per analysed game)")

    kinds = conn.execute("""SELECT kind, COUNT(*) AS n FROM mistakes
                            WHERE is_mine = 1 AND judgement IN ('mistake', 'blunder')
                            GROUP BY kind ORDER BY n DESC""").fetchall()
    print("\nWhat went wrong (mistakes + blunders)")
    for r in kinds:
        print(f"  {r['kind']:<16} {r['n']:>4}")
    print("\nWorst 10 moves (python -m chessreview mistakes --limit N for more):")
    _print_mistakes(conn, 10)


def _print_mistakes(conn: sqlite3.Connection, limit: int) -> None:
    rows = conn.execute("""
        SELECT m.*, p.move_san, g.white, g.black, g.date, g.my_color, g.opening_family
        FROM mistakes m
        JOIN positions p ON p.game_id = m.game_id AND p.ply = m.ply
        JOIN games g ON g.id = m.game_id
        WHERE m.is_mine = 1 AND m.judgement != 'good'
        ORDER BY m.win_drop DESC LIMIT ?""", (limit,)).fetchall()
    for r in rows:
        opponent = r["black"] if r["my_color"] == "white" else r["white"]
        move_no = r["ply"] // 2 + 1
        dots = "." if r["ply"] % 2 == 0 else "..."
        print(f"\n  {r['judgement'].upper():<10} {move_no}{dots}{r['move_san']}  "
              f"vs {opponent}, {r['date']} ({r['opening_family']}, {r['phase']}, game {r['game_id']})")
        print(f"             {r['explanation']}")


def _format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def cmd_analyse(conn: sqlite3.Connection, cfg, args) -> None:
    pending = count_pending(conn)
    total = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    print(f"{total - pending} games analysed and up to date (cached), {pending} to analyse.")
    if not pending:
        ensure_classified(conn, cfg)
        return
    e = cfg.engine
    print(f"Engine: {e.path}, up to {e.time_per_position}s or depth {e.max_depth or '∞'} per position, "
          f"{effective_workers(e)} workers x {e.threads} thread(s). Ctrl+C stops (finished games are kept).")

    job = AnalysisJob(cfg, limit=args.limit)
    job.start()
    try:
        while job.is_running():
            _print_progress(job)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping after current positions...")
        job.stop()
        job.wait()
    _print_progress(job)
    p = job.progress
    print(f"\nAnalysed {p.games_done} of {p.games_total} games.")
    for err in p.errors:
        print(f"Error: {err}")
    n = ensure_classified(conn, cfg)
    print(f"Classified moves in {n} games. Run 'summary' or 'mistakes' to see results.")


def cmd_reclassify(conn: sqlite3.Connection, cfg, args) -> None:
    n = ensure_classified(conn, cfg, force=True)
    print(f"Re-classified {n} games with the current thresholds.")


def cmd_mistakes(conn: sqlite3.Connection, cfg, args) -> None:
    ensure_classified(conn, cfg)
    _print_mistakes(conn, args.limit)


def _print_progress(job: AnalysisJob) -> None:
    p = job.progress
    width = 30
    filled = int(width * p.fraction)
    bar = "#" * filled + "-" * (width - filled)
    sys.stdout.write(f"\r[{bar}] {p.fraction * 100:5.1f}%  games {p.games_done}/{p.games_total}  "
                     f"positions {p.positions_done}/{p.positions_total}  ETA {_format_eta(p.eta_seconds)} ")
    sys.stdout.flush()


def cmd_serve(conn: sqlite3.Connection, cfg, args) -> None:
    from .web import create_app  # Flask is only needed for the web app
    refresh(conn, cfg)    # new games now; a full rebuild (new thresholds or version) in the background
    print(f"Open http://127.0.0.1:{args.port} in your browser (Ctrl+C to stop).")
    app = create_app(cfg)
    daily = backup.DailyBackups(cfg.db_path, cfg.backup).start()   # today's copy, then hourly checks
    try:
        app.run(host="127.0.0.1", port=args.port, threaded=True)
    finally:
        daily.stop()
        app.extensions["live"].close()  # stop Stockfish so Python can exit


def cmd_reset(conn: sqlite3.Connection, cfg, args) -> None:
    n = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    if not args.yes:
        answer = input(f"Delete all {n} games and their analysis? PGN files are not touched. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing deleted.")
            return
    print(f"Deleted {db.delete_all_games(conn)} games.")


def cmd_backup(conn: sqlite3.Connection, cfg, args) -> None:
    folder = backup.folder_for(cfg.db_path)
    if not args.list:
        print(f"Backed up to {backup.make(conn, 'manual', folder)}")
        backup.rotate(folder, cfg.backup)
    for b in backup.files(folder):
        info = b.as_dict()
        print(f"  {b.path.name:<44} {b.label:<34} {info['games'] or 0:>5} games  {info['size'] / 1e6:5.1f} MB")


def cmd_restore(conn: sqlite3.Connection, cfg, args) -> None:
    """Restore a copy from the backups folder (by name) or any file. The current data is backed up first."""
    source = Path(args.source)
    if not source.is_file():
        source = backup.folder_for(cfg.db_path) / args.source
    if not source.is_file():
        sys.exit(f"No such backup: {args.source} (see: python -m chessreview backup --list)")
    if not args.yes:
        answer = input(f"Replace all your data with {source.name}? The current data is backed up first. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing restored.")
            return
    try:
        before = db.restore(conn, source)
    except ValueError as exc:
        sys.exit(str(exc))
    games = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    print(f"Restored {source.name} ({games} games). Your previous data is in {before}.")


def cmd_puzzles(conn: sqlite3.Connection, cfg, args) -> None:
    """Download (or refresh) the puzzle library from the Lichess puzzle database."""
    from . import library
    progress = library.FetchProgress()
    job = threading.Thread(target=library.fetch, args=(conn, progress), daemon=True)
    print(f"Downloading {library.URL} (about 300 MB, streamed; only a sample is kept)...")
    job.start()
    while job.is_alive():
        job.join(1)
        if progress.bytes_total:
            pct = 100 * progress.bytes_read / progress.bytes_total
            print(f"\r{pct:5.1f}%  {progress.rows:,} puzzles read", end="", flush=True)
    print(f"\nKept {library.count(conn):,} puzzles. Your puzzle rating: {library.rating(conn)}.")


def cmd_simulate(conn: sqlite3.Connection, cfg, args) -> None:
    from . import simulate
    print(f"Simulating {args.days} days of Today at {args.budget} minutes on a copy of the database "
          "(your data is not changed)...")
    result = simulate.simulate_copy(conn, cfg, days=args.days, budget=args.budget, skip=args.skip, seed=args.seed)
    print(simulate.table(result))


def main() -> None:
    parser = argparse.ArgumentParser(prog="chessreview", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=None,
                        help="config file (default: $CHESSREVIEW_CONFIG, else ./config.toml if present)")
    parser.add_argument("--stockfish", default=None, metavar="PATH",
                        help="Stockfish executable (default: [engine] path, $STOCKFISH_PATH, PATH, usual folders)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync", help="fetch new games from chess.com")
    p.add_argument("--since", choices=["all", "3m", "1m"], default="all",
                   help="how far back the first sync goes (later syncs continue from the last one)")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("import", help="import PGN files or directories of PGN files")
    p.add_argument("paths", nargs="+")
    p.add_argument("--as", dest="player", metavar="NAME",
                   help="your name in these games (sets the player when none is set yet)")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("summary", help="print a summary of imported games")
    p.add_argument("--openings", type=int, default=15, help="number of openings to list")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("analyse", help="analyse un-analysed games with Stockfish")
    p.add_argument("--limit", type=int, default=None, help="only analyse N games (newest first)")
    p.set_defaults(func=cmd_analyse)

    p = sub.add_parser("reclassify", help="rebuild mistake classification from stored evals")
    p.set_defaults(func=cmd_reclassify)

    p = sub.add_parser("mistakes", help="list my worst moves with explanations")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_mistakes)

    p = sub.add_parser("serve", help="run the web app")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("puzzles", help="download the puzzle library (Lichess puzzle database sample)")
    p.set_defaults(func=cmd_puzzles)

    p = sub.add_parser("reset", help="delete all games and analysis to start afresh")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("backup", help="back up the database to data/backups (and list the copies)")
    p.add_argument("--list", action="store_true", help="only list the copies")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore", help="replace all data with a backup (the current data is backed up first)")
    p.add_argument("source", help="a file name from 'backup --list', or the path of an exported copy")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("simulate", help="simulate daily training on a copy of the database (load check)")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--budget", type=int, default=20, choices=[10, 20, 30], help="minutes a day")
    p.add_argument("--skip", type=float, default=0.15, help="share of days without training")
    p.add_argument("--seed", type=int, default=1)
    p.set_defaults(func=cmd_simulate)

    args = parser.parse_args()
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        sys.exit(str(exc))
    if args.stockfish:
        cfg = replace(cfg, engine=replace(cfg.engine, path=args.stockfish))
    if args.func in (cmd_analyse, cmd_serve):      # both need the engine: say so now, not mid-analysis
        try:
            cfg = replace(cfg, engine=replace(cfg.engine, path=find_stockfish(cfg.engine.path)))
        except StockfishNotFound as exc:
            sys.exit(str(exc))
    if args.func is cmd_restore:    # no schema work first: the database may be what is broken
        Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(cfg.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
    else:
        conn = db.connect(cfg.db_path)
    try:
        args.func(conn, cfg, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
