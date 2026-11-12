"""Shared test setup: the suite's own config, and no test reaches the Lichess tablebase unless it says so."""
import os
from pathlib import Path

import pytest

# Tests read tests/config.toml, never the user's config.toml (load_config() looks this up on every call).
os.environ["CHESSREVIEW_CONFIG"] = str(Path(__file__).parent / "config.toml")

from chessreview import tablebase


@pytest.fixture(autouse=True)
def no_tablebase_network(monkeypatch):
    def offline(url):
        raise OSError("network disabled in tests")
    monkeypatch.setattr(tablebase, "fetch", offline)
    tablebase.clear_cache()
    yield
    tablebase.clear_cache()
