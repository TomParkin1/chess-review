"""Shared test setup: no test reaches the Lichess tablebase (E3) unless it says so."""
import pytest

from chessreview import tablebase


@pytest.fixture(autouse=True)
def no_tablebase_network(monkeypatch):
    def offline(url):
        raise OSError("network disabled in tests")
    monkeypatch.setattr(tablebase, "fetch", offline)
    tablebase.clear_cache()
    yield
    tablebase.clear_cache()
