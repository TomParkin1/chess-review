"""Shared test setup: the suite's own config, Stockfish when installed, and no Lichess tablebase requests."""

import functools
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from chessreview import tablebase
from chessreview.config import CONFIG_ENV, STOCKFISH_ENV, load_config
from chessreview.engine import StockfishNotFound, find_stockfish

# Tests read tests/config.toml, never the user's config.toml (load_config() looks this up on every call).
os.environ[CONFIG_ENV] = str(Path(__file__).parent / "config.toml")
FAKE_ENV = "CHESSREVIEW_FAKE_ENGINE"  # use tests/fake_uci.py even where Stockfish is installed


@functools.cache  # looked up once, before the `stockfish` fixture points $STOCKFISH_PATH at the fake
def _real_stockfish() -> str | None:
    if os.environ.get(FAKE_ENV):
        return None
    try:
        return find_stockfish(load_config().engine.path)
    except StockfishNotFound:
        return None


def _fake_engine(folder: Path) -> str:
    """An executable that runs tests/fake_uci.py with this Python (a .cmd on Windows, a script elsewhere)."""
    script = Path(__file__).parent / "fake_uci.py"
    if sys.platform == "win32":
        launcher = folder / "fakefish.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\n')
    else:
        launcher = folder / "fakefish"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        launcher.chmod(0o755)
    return str(launcher)


@pytest.fixture(scope="session")
def stockfish(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A UCI engine for tests that need one: Stockfish when installed, else tests/fake_uci.py.

    It also becomes $STOCKFISH_PATH for the session, so configs loaded by the app under test use it.
    Set CHESSREVIEW_FAKE_ENGINE=1 to use the fake engine even where Stockfish is installed.
    """
    path = _real_stockfish() or _fake_engine(tmp_path_factory.mktemp("engine"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(STOCKFISH_ENV, path)
        yield path


@pytest.fixture(scope="session")
def real_stockfish() -> str:
    """Stockfish itself, for tests that depend on its strength; skipped without it."""
    path = _real_stockfish()
    if path is None:
        pytest.skip("needs Stockfish (not installed, or CHESSREVIEW_FAKE_ENGINE is set)")
    return path


@pytest.fixture(autouse=True)
def no_tablebase_network(monkeypatch):
    def offline(url):
        raise OSError("network disabled in tests")

    monkeypatch.setattr(tablebase, "fetch", offline)
    tablebase.clear_cache()
    yield
    tablebase.clear_cache()
