"""Load config.toml into simple dataclasses.

Every setting has a default here, so the file is optional and only needs the values you change.
"""

import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

CONFIG_NAME = "config.toml"
CONFIG_ENV = "CHESSREVIEW_CONFIG"      # path of the config file to use
STOCKFISH_ENV = "STOCKFISH_PATH"       # overrides [engine] path


# The [sections] of config.toml ("import" is ImportConfig, "database" only holds the path).
SECTIONS = {"player", "import", "engine", "classification", "phases", "database", "review", "analysis",
            "training", "coach", "backup"}


class ConfigError(ValueError):
    """The config file is missing, isn't valid TOML or has a setting the app doesn't know."""


@dataclass(frozen=True)
class PlayerConfig:
    username: str = ""          # chess.com username; the app's Settings can set it instead (db.username)


@dataclass(frozen=True)
class ImportConfig:
    min_plies: int = 10
    skip_variants: bool = True


@dataclass(frozen=True)
class EngineConfig:
    path: str = ""              # Stockfish executable; empty = find it (engine.find_stockfish)
    time_per_position: float = 0.3
    max_depth: int = 0          # stop a position early at this depth (0 = no depth limit)
    workers: int = 0            # parallel Stockfish processes; 0 = automatic (about 2/3 of CPU cores)
    threads: int = 1            # threads per process (1 is most efficient for many positions)
    hash_mb: int = 128
    pv_length: int = 8


def effective_workers(engine: EngineConfig) -> int:
    """Number of analysis processes: configured, or ~2/3 of the cores (8 on a 12-core Mac)."""
    if engine.workers > 0:
        return engine.workers
    return max(1, min(10, (os.cpu_count() or 3) * 2 // 3))


@dataclass(frozen=True)
class ClassificationConfig:
    mode: str = "winchance"
    inaccuracy: float = 0.1
    mistake: float = 0.2
    blunder: float = 0.3
    cp_inaccuracy: int = 50
    cp_mistake: int = 100
    cp_blunder: int = 300
    adaptive: bool = True        # thresholds follow the mover's rating (Settings page can override)


@dataclass(frozen=True)
class PhaseConfig:
    opening_plies: int = 20
    endgame_material: int = 13


@dataclass(frozen=True)
class ReviewConfig:
    playout_tactical: int = 3   # my moves to play out when I missed a mate/tactic/capture
    playout_other: int = 1      # my moves to play out for defensive/positional mistakes
    live_time: float = 0.35     # seconds per position when judging moves I try (4 threads)


@dataclass(frozen=True)
class AnalysisConfig:
    max_seconds: float = 10     # live analysis stops after this long per position
    multipv: int = 3            # engine lines shown on the analysis board


@dataclass(frozen=True)
class TrainingConfig:
    new_per_day: int = 10            # new mistakes introduced into daily training per day
    include_inaccuracies: bool = False
    blind: bool = True               # Train shows positions "as in a real game" (no badge, no arrow)


@dataclass(frozen=True)
class CoachConfig:
    calm_ratio: float = 0.3          # share of the real-game deck that is positions I handled well
    recent_games: int = 100          # calm positions come from my last N games
    budget_minutes: int = 20         # Today: default time budget (10, 20 or 30; the Today page changes it)
    seconds_per_puzzle: float = 60   # Today: time per puzzle until my own pace can be measured
    review_share: float = 0.8        # Today: due reviews fill at most this share of the puzzles...
    new_share: float = 0.7           # ...and new cards come only while they fill less than this share
    session_days: int = 3            # an unfinished new-games session joins daily training after this
    history_days: int = 14           # an import of games mostly older than this is history, not a session
    progress_window: int = 25        # Progress: compare my last N games with the N before
    self_review_games: int = 5       # New-games session: games to self-review before the engine


@dataclass(frozen=True)
class BackupConfig:
    daily: int = 7                   # daily copies kept in data/backups (backup.py)
    weekly: int = 4                  # weekly copies kept
    other: int = 10                  # copies made by hand or before Delete all, reclassify, migrations...


@dataclass(frozen=True)
class Config:
    player: PlayerConfig
    imports: ImportConfig
    engine: EngineConfig
    classification: ClassificationConfig
    phases: PhaseConfig
    db_path: Path
    review: ReviewConfig = ReviewConfig()
    analysis: AnalysisConfig = AnalysisConfig()
    training: TrainingConfig = TrainingConfig()
    coach: CoachConfig = CoachConfig()
    backup: BackupConfig = BackupConfig()


def find_config(path: Path | str | None = None) -> Path | None:
    """The config file to read: `path`, else $CHESSREVIEW_CONFIG, else ./config.toml if there is one."""
    if path:
        return Path(path)
    if os.environ.get(CONFIG_ENV):
        return Path(os.environ[CONFIG_ENV])
    local = Path.cwd() / CONFIG_NAME
    return local if local.is_file() else None


def load_config(path: Path | str | None = None) -> Config:
    """Read the configuration (see find_config); without a file every setting has its default.

    A relative database path is relative to the config file's folder (the current folder without one).
    $STOCKFISH_PATH, when set, replaces [engine] path.
    """
    file = find_config(path)
    raw: dict = {}
    base = Path.cwd()
    if file is not None:
        try:
            with open(file, "rb") as f:
                raw = tomllib.load(f)
        except FileNotFoundError:
            raise ConfigError(f"Config file not found: {file}") from None
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{file} isn't valid TOML: {exc}") from None
        base = file.resolve().parent

    unknown = set(raw) - SECTIONS
    if unknown:
        raise ConfigError(f"{file}: unknown section [{sorted(unknown)[0]}] (known: {', '.join(sorted(SECTIONS))})")

    def table(name: str) -> dict:
        value = raw.get(name, {})
        if not isinstance(value, dict):
            raise ConfigError(f"{file}: {name} must be a [{name}] section, not a single value")
        return value

    def section(cls, name: str):
        values = table(name)
        unknown_keys = set(values) - set(cls.__dataclass_fields__)
        if unknown_keys:
            raise ConfigError(f"{file}: unknown setting {sorted(unknown_keys)[0]!r} in [{name}]")
        return cls(**values)

    db_value = table("database").get("path", "data/chess.db")
    if not isinstance(db_value, str):
        raise ConfigError(f"{file}: [database] path must be a string")
    db_path = Path(db_value).expanduser()
    if not db_path.is_absolute():
        db_path = base / db_path

    engine = section(EngineConfig, "engine")
    if os.environ.get(STOCKFISH_ENV):
        engine = replace(engine, path=os.environ[STOCKFISH_ENV])

    return Config(
        player=section(PlayerConfig, "player"),
        imports=section(ImportConfig, "import"),
        engine=engine,
        classification=section(ClassificationConfig, "classification"),
        phases=section(PhaseConfig, "phases"),
        db_path=db_path,
        review=section(ReviewConfig, "review"),
        analysis=section(AnalysisConfig, "analysis"),
        training=section(TrainingConfig, "training"),
        coach=section(CoachConfig, "coach"),
        backup=section(BackupConfig, "backup"),
    )
