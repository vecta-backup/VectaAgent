"""Local credential profiles stored in secrets.toml.

Secrets never leave the machine: the backend references a profile by name
only, and all secret values live in this file on the agent machine.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from vecta_agent import config

SECRETS_FILENAME = "secrets.toml"

# Must match the backend's JobCreateRequest validation (schemas.py).
PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class SecretsError(Exception):
    """Raised when secrets.toml is missing, malformed, or contains bad values."""


class ProfileNotFoundError(SecretsError):
    """Raised when a job references a profile that is not configured locally."""


def secrets_path() -> Path:
    return config._config_dir() / SECRETS_FILENAME


def _validate_name(name: str) -> str:
    if not PROFILE_NAME_RE.fullmatch(name):
        raise SecretsError(
            "Profile name must be 1-64 chars of lowercase letters, digits, '_' "
            f"or '-' (got {name!r})."
        )
    return name


def _read_toml() -> dict[str, Any]:
    path = secrets_path()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise SecretsError(f"Invalid TOML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SecretsError(f"{path} must contain TOML tables.")
    return data


def _write_toml(data: dict[str, Any]) -> None:
    path = secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    if data:
        lines.append("# Vecta agent credential profiles. Keep this file secret. chmod 600.")
        for name, entries in data.items():
            lines.append("")
            lines.append(f"[{name}]")
            for key, value in entries.items():
                lines.append(f'{key} = "{_escape_toml_value(value)}"')
    path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")
    os.chmod(path, 0o600)


def _escape_toml_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def load_profile(name: str) -> dict[str, str] | None:
    """Return the profile's KEY=VALUE env entries, or None if not configured."""
    _validate_name(name)
    data = _read_toml()
    section = data.get(name)
    if section is None:
        return None
    if not isinstance(section, dict):
        raise SecretsError(f"Profile {name!r} in {secrets_path()} must be a table.")
    result: dict[str, str] = {}
    for key, value in section.items():
        if not isinstance(value, str):
            raise SecretsError(
                f"Profile {name!r} key {key!r} must be a string value."
            )
        result[key] = value
    return result


def save_profile(name: str, entries: dict[str, str]) -> Path:
    """Create or merge a profile section, preserving other profiles."""
    _validate_name(name)
    for key, value in entries.items():
        if not isinstance(key, str) or not key:
            raise SecretsError("Secret keys must be non-empty strings.")
        if not isinstance(value, str):
            raise SecretsError(f"Secret value for {key!r} must be a string.")

    data = _read_toml()
    existing = data.get(name)
    merged: dict[str, str] = {}
    if isinstance(existing, dict):
        for key, value in existing.items():
            if isinstance(value, str):
                merged[key] = value
    merged.update(entries)
    data[name] = merged
    _write_toml(data)
    return secrets_path()


def remove_profile(name: str) -> bool:
    """Delete a profile section. Returns True when it existed."""
    _validate_name(name)
    data = _read_toml()
    if name not in data:
        return False
    del data[name]
    _write_toml(data)
    return True


def list_profiles() -> dict[str, list[str]]:
    """Return {profile_name: [key, ...]} — names and key names only, never values."""
    data = _read_toml()
    result: dict[str, list[str]] = {}
    for name, section in data.items():
        if isinstance(section, dict):
            result[name] = sorted(str(key) for key in section)
        else:
            result[name] = []
    return result