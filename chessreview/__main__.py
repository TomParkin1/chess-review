"""Command line interface.

    python -m chessreview import FILE_OR_DIR [...]
    python -m chessreview summary
    python -m chessreview analyse [--limit N]
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

from . import db
from .config import load_config
from .engine import AnalysisJob
from .pgn_import import import_paths


def cmd_import(conn: sqlite3.Connection, cfg, args) -> None:
    stats = import_paths(conn, [Path(p) for p in args.paths], cfg)
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
    print(f"Player: {cfg.player.username}")
    print(f"Games:  {total[0]}  ({total[1]} to {total[2]})")
    _wdl_table(_wdl_query(conn, "'all'"), "Overall")
    _wdl_table(_wdl_query(conn, "my_color"), "By colour")
    _wdl_table(_wdl_query(conn, "time_class"), "By time class")
    _wdl_table(_wdl_query(conn, "opening_family", limit=args.openings), "Top openings")


def _format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def cmd_analyse(conn: sqlite3.Connection, cfg, args) -> None:
    pending = conn.execute("SELECT COUNT(*) FROM games WHERE analysed_at IS NULL").fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM games WHERE analysed_at IS NOT NULL").fetchone()[0]
    print(f"{done} games already analysed (cached), {pending} to analyse.")
    if not pending:
        return
    e = cfg.engine
    print(f"Engine: {e.path}, {e.time_per_position}s/position, "
          f"{e.workers} workers x {e.threads} threads. Ctrl+C stops (finished games are kept).")

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


def _print_progress(job: AnalysisJob) -> None:
    p = job.progress
    width = 30
    filled = int(width * p.fraction)
    bar = "#" * filled + "-" * (width - filled)
    sys.stdout.write(f"\r[{bar}] {p.fraction * 100:5.1f}%  games {p.games_done}/{p.games_total}  "
                     f"positions {p.positions_done}/{p.positions_total}  ETA {_format_eta(p.eta_seconds)} ")
    sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(prog="chessreview", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=None, help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import", help="import PGN files or directories of PGN files")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("summary", help="print a summary of imported games")
    p.add_argument("--openings", type=int, default=15, help="number of openings to list")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("analyse", help="analyse un-analysed games with Stockfish")
    p.add_argument("--limit", type=int, default=None, help="only analyse N games (newest first)")
    p.set_defaults(func=cmd_analyse)

    args = parser.parse_args()
    cfg = load_config(args.config) if args.config else load_config()
    conn = db.connect(cfg.db_path)
    try:
        args.func(conn, cfg, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
