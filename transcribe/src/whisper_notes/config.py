"""Config loading (D-08): TOML via stdlib tomllib, defaults in code.

The server host is deliberately absent from the config surface: the app binds
127.0.0.1 unconditionally (FR-30). Making the host configurable is the drift
this module exists to prevent.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("config.toml")


class ConfigError(Exception):
    """Invalid or unreadable configuration. Message is user-facing (FR-38)."""


@dataclass(frozen=True)
class Config:
    port: int = 8420
    model_repo: str = "mlx-community/whisper-large-v3-turbo"
    model_idle_timeout_seconds: int = 600
    timestamps: bool = True
    summary_line: bool = True
    paragraph_pause_seconds: float = 2.0
    paragraph_max_chars: int = 750
    max_file_gb: float = 2.0
    max_duration_hours: float = 6.0
    data_dir: Path = Path("data")

    @property
    def max_file_bytes(self) -> int:
        return int(self.max_file_gb * 1024**3)

    @property
    def max_duration_seconds(self) -> float:
        return self.max_duration_hours * 3600

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def transcripts_dir(self) -> Path:
        return self.data_dir / "transcripts"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobs.db"


# (section, key) in the TOML file -> (Config field, expected type).
# bool is checked before int since bool is a subclass of int in Python.
_SCHEMA: dict[tuple[str, str], tuple[str, type]] = {
    ("server", "port"): ("port", int),
    ("model", "repo"): ("model_repo", str),
    ("model", "idle_timeout_seconds"): ("model_idle_timeout_seconds", int),
    ("output", "timestamps"): ("timestamps", bool),
    ("output", "summary_line"): ("summary_line", bool),
    ("output", "paragraph_pause_seconds"): ("paragraph_pause_seconds", float),
    ("output", "paragraph_max_chars"): ("paragraph_max_chars", int),
    ("limits", "max_file_gb"): ("max_file_gb", float),
    ("limits", "max_duration_hours"): ("max_duration_hours", float),
    ("storage", "data_dir"): ("data_dir", Path),
}


def load_config(path: Path | None = None) -> Config:
    """Read TOML config, falling back to defaults for absent keys.

    A missing file is fine (all defaults). Unknown sections/keys and wrong
    types are hard errors so a typo can't silently revert a setting.
    """
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return Config()

    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from e

    values: dict[str, object] = {}
    for section, table in raw.items():
        if not isinstance(table, dict):
            raise ConfigError(f"{path}: top-level key '{section}' must be a [section]")
        for key, value in table.items():
            field_and_type = _SCHEMA.get((section, key))
            if field_and_type is None:
                raise ConfigError(f"{path}: unknown setting [{section}] {key}")
            field, expected = field_and_type
            if expected is bool:
                if not isinstance(value, bool):
                    raise ConfigError(f"{path}: [{section}] {key} must be true or false")
            elif expected is int:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ConfigError(f"{path}: [{section}] {key} must be an integer")
            elif expected is float:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ConfigError(f"{path}: [{section}] {key} must be a number")
                value = float(value)
            elif expected is str:
                if not isinstance(value, str):
                    raise ConfigError(f"{path}: [{section}] {key} must be a string")
            elif expected is Path:
                if not isinstance(value, str):
                    raise ConfigError(f"{path}: [{section}] {key} must be a path string")
                value = Path(value)
            values[field] = value

    config = Config(**values)  # type: ignore[arg-type]
    _validate(config, path)
    return config


def _validate(config: Config, path: Path) -> None:
    if not (1 <= config.port <= 65535):
        raise ConfigError(f"{path}: [server] port must be between 1 and 65535")
    if config.model_idle_timeout_seconds < 0:
        raise ConfigError(f"{path}: [model] idle_timeout_seconds must be >= 0")
    if config.paragraph_pause_seconds <= 0:
        raise ConfigError(f"{path}: [output] paragraph_pause_seconds must be > 0")
    if config.paragraph_max_chars < 100:
        raise ConfigError(f"{path}: [output] paragraph_max_chars must be >= 100")
    if config.max_file_gb <= 0 or config.max_duration_hours <= 0:
        raise ConfigError(f"{path}: [limits] values must be > 0")
