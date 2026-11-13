"""Fetch my games from chess.com's public API (no login needed).

  https://api.chess.com/pub/player/{user}/games/archives  -> list of monthly archive URLs
  https://api.chess.com/pub/player/{user}/games/YYYY/MM   -> {"games": [{pgn, uuid, rules, ...}]}

Only months on or after the last synced month are fetched (the current month is always
re-fetched because it is still filling up). Requests are made one at a time, as the API
asks, with backoff on HTTP 429. Every sync is one import batch.
"""

import io
import json
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import chess.pgn

from . import db, session
from .config import Config
from .pgn_import import ImportStats, import_game

API = "https://api.chess.com/pub/player/{user}/games/archives"
USER_AGENT = "chess-review (local training app; python urllib)"
LAST_MONTH_KEY = "chesscom_last_month"  # 'YYYY/MM' of the newest archive fetched
SINCE_CHOICES = {"all": None, "3m": 3, "1m": 1}


@dataclass
class SyncResult:
    """What a sync did: months fetched, import counts and the batch it created."""

    months: list[str] = field(default_factory=list)
    stats: ImportStats = field(default_factory=ImportStats)
    batch_id: int | None = None
    games_added: int = 0

    def as_dict(self) -> dict:
        s = self.stats
        return {
            "months": self.months,
            "games_seen": s.games_seen,
            "imported": s.imported,
            "duplicates": s.duplicates,
            "skipped": s.skipped,
            "batch_id": self.batch_id,
        }


class Unavailable(OSError):
    """chess.com answered, but not with the JSON we asked for (maintenance page, server error)."""


def fetch_json(url: str, retries: int = 4) -> dict:
    """GET a chess.com API URL as JSON, retrying rate limits and server errors with back-off."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            # Rate limited or a server error: back off and retry.
            if (exc.code == 429 or exc.code >= 500) and attempt < retries - 1:
                time.sleep(2**attempt)
                continue
            if exc.code == 404:
                raise LookupError(f"chess.com user not found ({url})") from exc
            raise
        except json.JSONDecodeError as exc:  # a ValueError, which callers read as "no username"
            raise Unavailable(f"chess.com sent a page that isn't game data ({url}); try again later") from exc
    raise RuntimeError("unreachable")


def months_to_fetch(
    archives: list[str], last_month: str | None, since: str | None, today: date | None = None
) -> list[str]:
    """Pick archive URLs: everything after the last sync, else according to `since`."""

    def month_of(url: str) -> str:
        return "/".join(url.rstrip("/").split("/")[-2:])  # 'YYYY/MM'

    if last_month:
        return [u for u in archives if month_of(u) >= last_month]
    keep = SINCE_CHOICES.get(since or "all")
    if keep is None:
        return list(archives)
    today = today or date.today()
    y, m = today.year, today.month - (keep - 1)
    while m <= 0:
        y, m = y - 1, m + 12
    return [u for u in archives if month_of(u) >= f"{y:04d}/{m:02d}"]


def archives_of(user: str, fetch: Callable[[str], Any] = fetch_json) -> list[str]:
    """URLs of a player's monthly archives, oldest first. LookupError if chess.com has no such player."""
    return fetch(API.format(user=user.strip().lower()))["archives"]


def sync(
    conn: sqlite3.Connection,
    cfg: Config,
    since: str | None = None,
    fetch: Callable[[str], Any] = fetch_json,
    progress: Callable[[int, int, str], None] | None = None,
) -> SyncResult:
    """Import new games from chess.com as one batch. `fetch` is injectable for tests."""
    user = db.username(conn, cfg)
    if not user:
        raise ValueError("Set your chess.com username first.")
    archives = archives_of(user, fetch)
    last_month = db.get_meta(conn, LAST_MONTH_KEY)
    urls = months_to_fetch(archives, last_month, since)

    result = SyncResult()
    result.batch_id = db.create_batch(conn, "chess.com", f"chess.com sync ({len(urls)} month(s))")
    try:
        for i, url in enumerate(urls):
            month = "/".join(url.rstrip("/").split("/")[-2:])
            if progress:
                progress(i, len(urls), month)
            for g in fetch(url).get("games", []):
                if g.get("rules", "chess") != "chess":
                    result.stats.games_seen += 1
                    result.stats.skip(f"variant ({g.get('rules')})")
                    continue
                game = chess.pgn.read_game(io.StringIO(g.get("pgn", "")))
                if game is None:
                    continue
                import_game(
                    conn,
                    game,
                    cfg,
                    source="chess.com",
                    stats=result.stats,
                    batch_id=result.batch_id,
                    origin="chess.com",
                    extra={"uuid": g.get("uuid"), "rated": g.get("rated")},
                )
            conn.commit()
            result.months.append(month)
            with conn:
                db.set_meta(conn, LAST_MONTH_KEY, month)
    finally:
        conn.commit()  # the months imported before a failure are kept; an empty batch is dropped
        result.games_added = session.finish_import(conn, cfg, result.batch_id)
        if result.games_added and last_month is None:
            # The first sync brings in history, not "games I just played": no new-games session;
            # its mistakes go straight to daily training instead. Also when it failed partway
            # (the next sync is no longer the first one).
            with conn:
                conn.execute(
                    """UPDATE imports SET session_done_at = COALESCE(session_done_at, CURRENT_TIMESTAMP),
                                    label = 'chess.com history import' WHERE id = ?""",
                    (result.batch_id,),
                )
    if not result.games_added:
        result.batch_id = None
    return result
