"""Local agent configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib


CONFIG_DIR_ENV = "VECTA_CONFIG_DIR"
CONFIG_FILENAME = "config.toml"


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


@dataclass
class Config:
    agent_id: str
    api_key: str
    name: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


def _config_dir() -> Path:
    if CONFIG_DIR_ENV in os.environ:
        return Path(os.environ[CONFIG_DIR_ENV])
    return Path.home() / ".config" / "vecta"


def config_path() -> Path:
    return _config_dir() / CONFIG_FILENAME


def _escape_toml_value(value: str) -> str:
    # Minimal TOML escaping for short string values.
    return value.replace("\\", "\\\\").replace('"', '\\"')


def load() -> Config:
    path = config_path()
    if not path.exists():
        raise ConfigError(
            f"Config not found at {path}. Run 'vecta-agent register --token <TOKEN>' first."
        )

    with path.open("rb") as f:
        data = tomllib.load(f)

    agent_id = data.get("agent_id")
    api_key = data.get("api_key")
    name = data.get("name")

    if not agent_id or not api_key:
        raise ConfigError(
            f"Config at {path} must contain 'agent_id' and 'api_key'."
        )

    if not isinstance(agent_id, str) or not isinstance(api_key, str):
        raise ConfigError("'agent_id' and 'api_key' must be strings.")

    if name is not None and not isinstance(name, str):
        raise ConfigError("'name' must be a string if present.")

    return Config(agent_id=agent_id, api_key=api_key, name=name)


def save(config: Config) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Vecta agent configuration",
        "# Keep this file secret. chmod 600.",
        f'agent_id = "{_escape_toml_value(config.agent_id)}"',
        f'api_key = "{_escape_toml_value(config.api_key)}"',
    ]
    if config.name:
        lines.append(f'name = "{_escape_toml_value(config.name)}"')
    lines.append("")  # trailing newline

    path.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(path, 0o600)
