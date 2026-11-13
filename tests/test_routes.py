"""The web app's URL map: a route that goes missing (or appears) is a deliberate change."""

from dataclasses import replace
from pathlib import Path

from chessreview.config import load_config
from chessreview.web import create_app

ROUTES = Path(__file__).parent / "data" / "routes.txt"  # "<rule> <METHOD,...>" per line


def test_every_route_is_where_the_pages_expect_it(tmp_path):
    app = create_app(replace(load_config(), db_path=tmp_path / "routes.db"))
    try:
        rules = {
            f"{r.rule} {','.join(sorted(r.methods - {'HEAD', 'OPTIONS'}))}"
            for r in app.url_map.iter_rules()
            if r.endpoint != "static"
        }
    finally:
        app.extensions["live"].close()
    assert rules == set(ROUTES.read_text().split("\n")) - {""}
