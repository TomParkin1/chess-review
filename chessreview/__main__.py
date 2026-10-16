"""Command line interface.

    python -m chessreview import FILE_OR_DIR [...]
    python -m chessreview summary
    python -m chessreview analyse [--limit N]
    python -m chessreview reclassify
    python -m chessreview mistakes [--limit N]
    python -m chessreview serve [--port 8000]
    python -m chessreview reset [--yes]
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

from . import db
from .classify import ensure_classified
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
        FROM mistakes m GROUP BY m.phase
        UNION ALL
        SELECT 'all', COUNT(*), SUM(judgement = 'inaccuracy'), SUM(judgement = 'mistake'),
               SUM(judgement = 'blunder'), COUNT(DISTINCT game_id) FROM mistakes
    """).fetchall()
    order = {"opening": 0, "middlegame": 1, "endgame": 2, "all": 3}
    for r in sorted(rows, key=lambda r: order.get(r["k"], 9)):
        per_game = (r["m"] + r["b"]) / analysed
        print(f"{r['k']:<14} {r['n']:>6} {r['i']:>7} {r['m']:>8} {r['b']:>8} {per_game:>9.1f}")
    print("(per game = mistakes + blunders per analysed game)")

    kinds = conn.execute("""SELECT kind, COUNT(*) AS n FROM mistakes
                            WHERE judgement IN ('mistake', 'blunder')
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
        WHERE m.judgement != 'good'
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
    pending = conn.execute("SELECT COUNT(*) FROM games WHERE analysed_at IS NULL").fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM games WHERE analysed_at IS NOT NULL").fetchone()[0]
    print(f"{done} games already analysed (cached), {pending} to analyse.")
    if not pending:
        ensure_classified(conn, cfg)
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
    ensure_classified(conn, cfg)
    print(f"Open http://127.0.0.1:{args.port} in your browser (Ctrl+C to stop).")
    app = create_app(cfg)
    try:
        app.run(host="127.0.0.1", port=args.port, threaded=True)
    finally:
        app.extensions["live"].close()  # stop Stockfish so Python can exit


def cmd_reset(conn: sqlite3.Connection, cfg, args) -> None:
    n = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    if not args.yes:
        answer = input(f"Delete all {n} games and their analysis? PGN files are not touched. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing deleted.")
            return
    print(f"Deleted {db.delete_all_games(conn)} games.")


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

    p = sub.add_parser("reclassify", help="rebuild mistake classification from stored evals")
    p.set_defaults(func=cmd_reclassify)

    p = sub.add_parser("mistakes", help="list my worst moves with explanations")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_mistakes)

    p = sub.add_parser("serve", help="run the web app")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("reset", help="delete all games and analysis to start afresh")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p.set_defaults(func=cmd_reset)

    args = parser.parse_args()
    cfg = load_config(args.config) if args.config else load_config()
    conn = db.connect(cfg.db_path)
    try:
        args.func(conn, cfg, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
