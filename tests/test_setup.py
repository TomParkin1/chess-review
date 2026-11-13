"""Getting started: finding the config and Stockfish, and importing the sample games from the command line."""

import sys
from pathlib import Path

import pytest

from chessreview import __main__ as cli
from chessreview import db, engine
from chessreview.config import CONFIG_ENV, STOCKFISH_ENV, ConfigError, load_config

SAMPLES = Path(__file__).parent.parent / "samples"


@pytest.fixture
def no_config(tmp_path, monkeypatch):
    """An empty current folder and no $CHESSREVIEW_CONFIG: a new user's first run."""
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    monkeypatch.delenv(STOCKFISH_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ------------------------------------------------------------ config


def test_without_a_config_file_every_setting_has_its_default(no_config):
    cfg = load_config()
    assert cfg.engine.path == "" and cfg.player.username == ""
    assert cfg.db_path == no_config / "data" / "chess.db"


def test_config_toml_in_the_current_folder_is_used(no_config):
    (no_config / "config.toml").write_text('[player]\nusername = "someone"\n[database]\npath = "games.db"\n')
    cfg = load_config()
    assert cfg.player.username == "someone" and cfg.db_path == no_config / "games.db"


def test_a_relative_database_path_is_relative_to_the_config_file(no_config):
    folder = no_config / "elsewhere"
    folder.mkdir()
    (folder / "my.toml").write_text('[database]\npath = "db/chess.db"\n')
    assert load_config(folder / "my.toml").db_path == folder / "db" / "chess.db"


@pytest.mark.parametrize(
    "text, message",
    [
        ("[engine\npath = 1", "isn't valid TOML"),
        ("[engine]\nspeed = 3\n", r"unknown setting 'speed' in \[engine\]"),
        ("[enigne]\npath = 'x'\n", r"unknown section \[enigne\]"),
        ("engine = 3\n", r"engine must be a \[engine\] section"),
        ("[database]\npath = 3\n", r"\[database\] path must be a string"),
    ],
)
def test_a_bad_config_file_names_the_problem(no_config, text, message):
    (no_config / "config.toml").write_text(text)
    with pytest.raises(ConfigError, match=message):
        load_config()


def test_a_missing_config_file_is_a_config_error(no_config):
    with pytest.raises(ConfigError, match="not found"):
        load_config(no_config / "nope.toml")


def test_stockfish_path_variable_overrides_the_config(no_config, monkeypatch):
    (no_config / "config.toml").write_text('[engine]\npath = "/from/config"\n')
    monkeypatch.setenv(STOCKFISH_ENV, "/from/env")
    assert load_config().engine.path == "/from/env"


# ------------------------------------------------------------ Stockfish


def test_a_configured_stockfish_that_is_missing_is_an_error_with_install_help():
    with pytest.raises(engine.StockfishNotFound, match="brew install stockfish"):
        engine.find_stockfish("/no/such/stockfish")


def test_stockfish_is_found_on_path_then_in_the_usual_folders(monkeypatch):
    on_path = {"stockfish": "/bin/stockfish-on-path", "/usr/games/stockfish": "/usr/games/stockfish"}
    monkeypatch.setattr(engine.shutil, "which", on_path.get)
    assert engine.find_stockfish() == "/bin/stockfish-on-path"
    del on_path["stockfish"]
    assert engine.find_stockfish() == "/usr/games/stockfish"  # Debian/Ubuntu's apt, often not on PATH
    on_path.clear()
    with pytest.raises(engine.StockfishNotFound):
        engine.find_stockfish()


# ------------------------------------------------------------ command line


def run(monkeypatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["chessreview", *argv])
    cli.main()


def test_the_samples_import_as_sample_player(no_config, monkeypatch, capsys):
    run(monkeypatch, "import", str(SAMPLES), "--as", "Sample Player")
    assert "Imported:     5" in capsys.readouterr().out
    conn = db.connect(no_config / "data" / "chess.db")
    colours = conn.execute("SELECT my_color, COUNT(*) FROM games GROUP BY my_color ORDER BY my_color").fetchall()
    assert [tuple(r) for r in colours] == [("black", 2), ("white", 3)]
    assert db.username(conn, load_config()) == "Sample Player"
    conn.close()


def test_import_without_a_player_says_how_to_name_one(no_config, monkeypatch):
    with pytest.raises(SystemExit, match="--as"):
        run(monkeypatch, "import", str(SAMPLES))


def test_import_as_someone_else_does_not_change_the_player(no_config, monkeypatch):
    run(monkeypatch, "import", str(SAMPLES), "--as", "Sample Player")
    with pytest.raises(SystemExit, match="already 'Sample Player'"):
        run(monkeypatch, "import", str(SAMPLES), "--as", "Opponent")


def test_analyse_without_stockfish_stops_with_install_help(no_config, monkeypatch):
    with pytest.raises(SystemExit, match="Stockfish not found"):
        run(monkeypatch, "--stockfish", "/no/such/stockfish", "analyse")


def test_a_bad_config_stops_the_cli_with_its_message(no_config, monkeypatch):
    (no_config / "config.toml").write_text("not = [valid")
    with pytest.raises(SystemExit, match="isn't valid TOML"):
        run(monkeypatch, "summary")


def test_import_as_a_name_with_no_games_leaves_the_player_unset(no_config, monkeypatch, capsys):
    """A typo in --as used to stick, and then refused the right name."""
    run(monkeypatch, "import", str(SAMPLES), "--as", "Sample Playr")
    assert "player is unchanged" in capsys.readouterr().out
    run(monkeypatch, "import", str(SAMPLES), "--as", "sample player")  # names match in any case
    assert "Imported:     5" in capsys.readouterr().out


def test_import_reports_paths_it_cannot_read_and_exits_with_an_error(no_config, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exit_:
        run(monkeypatch, "import", str(SAMPLES), str(no_config / "typo.pgn"), "--as", "Sample Player")
    out, err = capsys.readouterr()
    assert exit_.value.code == 1
    assert "Imported:     5" in out and "typo.pgn: no such file or folder" in err


def test_sync_offline_says_so(no_config, monkeypatch):
    import urllib.error

    from chessreview import chesscom

    def offline(request, timeout):
        raise urllib.error.URLError("network is unreachable")

    monkeypatch.setattr(chesscom.urllib.request, "urlopen", offline)
    (no_config / "config.toml").write_text('[player]\nusername = "someone"\n')
    with pytest.raises(SystemExit, match="Couldn't reach chess.com"):
        run(monkeypatch, "sync")


def test_a_failed_puzzle_download_says_so(no_config, monkeypatch):
    from chessreview import library

    def offline(conn, progress):
        raise OSError("network is unreachable")

    monkeypatch.setattr(library, "fetch", offline)
    with pytest.raises(SystemExit, match="Couldn't download the puzzle library"):
        run(monkeypatch, "puzzles")
