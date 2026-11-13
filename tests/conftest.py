"""Shared test setup: the suite's own config, Stockfish when installed, and no Lichess tablebase requests."""

import os
from pathlib import Path

import pytest

from chessreview import tablebase
from chessreview.config import CONFIG_ENV, load_config
from chessreview.engine import StockfishNotFound, find_stockfish

# Tests read tests/config.toml, never the user's config.toml (load_config() looks this up on every call).
os.environ[CONFIG_ENV] = str(Path(__file__).parent / "config.toml")


@pytest.fixture(scope="session")
def stockfish() -> str:
    """The Stockfish executable; a test that asks for it is skipped on a machine without one."""
    try:
        return find_stockfish(load_config().engine.path)
    except StockfishNotFound:
        pytest.skip("Stockfish not installed")


@pytest.fixture(autouse=True)
def no_tablebase_network(monkeypatch):
    def offline(url):
        raise OSError("network disabled in tests")

    monkeypatch.setattr(tablebase, "fetch", offline)
    tablebase.clear_cache()
    yield
    tablebase.clear_cache()
