"""Load config.toml into simple dataclasses."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"


@dataclass(frozen=True)
class PlayerConfig:
    username: str


@dataclass(frozen=True)
class ImportConfig:
    min_plies: int = 10
    skip_variants: bool = True


@dataclass(frozen=True)
class EngineConfig:
    path: str = "/opt/homebrew/bin/stockfish"
    time_per_position: float = 0.3
    workers: int = 4
    threads: int = 2
    hash_mb: int = 128
    pv_length: int = 8


@dataclass(frozen=True)
class ClassificationConfig:
    mode: str = "winchance"
    inaccuracy: float = 0.1
    mistake: float = 0.2
    blunder: float = 0.3
    cp_inaccuracy: int = 50
    cp_mistake: int = 100
    cp_blunder: int = 300


@dataclass(frozen=True)
class PhaseConfig:
    opening_plies: int = 20
    endgame_material: int = 13


@dataclass(frozen=True)
class Config:
    player: PlayerConfig
    imports: ImportConfig
    engine: EngineConfig
    classification: ClassificationConfig
    phases: PhaseConfig
    db_path: Path


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    db_path = Path(raw.get("database", {}).get("path", "data/chess.db"))
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path

    return Config(
        player=PlayerConfig(**raw["player"]),
        imports=ImportConfig(**raw.get("import", {})),
        engine=EngineConfig(**raw.get("engine", {})),
        classification=ClassificationConfig(**raw.get("classification", {})),
        phases=PhaseConfig(**raw.get("phases", {})),
        db_path=db_path,
    )
