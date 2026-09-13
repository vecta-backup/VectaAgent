"""Per-destination credentials stored in credentials.toml.

Secrets never leave the machine: entries are keyed by a fingerprint of the
destination (a hash of the destination string), so jobs need no user-named
profile — two jobs to the same destination automatically share credentials.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from vecta_agent import config

CREDENTIALS_FILENAME = "credentials.toml"

# Non-secret annotation stored alongside the secrets so `setup` can tell the
# user which destination a fingerprint belongs to. Excluded from load results.
_DESTINATION_KEY = "destination"


class CredentialsError(Exception):
    """Raised when credentials.toml is missing values it must contain or is malformed."""


def destination_fingerprint(destination: str) -> str:
    """Stable fingerprint of a destination: sha256 of the trimmed string.

    The destination string already carries endpoint + bucket (s3:) or
    user@host + path (sftp:), so hashing it keys all credentials for one
    restic repository under a single section. The SFTP `port` job option is
    deliberately excluded: it changes restic's connection flags, not auth.
    """
    return hashlib.sha256(destination.strip().encode("utf-8")).hexdigest()[:16]


def credentials_path() -> Path:
    return config._config_dir() / CREDENTIALS_FILENAME


def _read_toml() -> dict[str, Any]:
    path = credentials_path()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise CredentialsError(f"Invalid TOML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CredentialsError(f"{path} must contain TOML tables.")
    return data


def _write_toml(data: dict[str, Any]) -> None:
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    if data:
        lines.append("# Vecta agent per-destination credentials. Keep this file secret. chmod 600.")
        for fingerprint, entries in data.items():
            lines.append("")
            lines.append(f"[{fingerprint}]")
            for key, value in entries.items():
                lines.append(f'{key} = "{_escape_toml_value(value)}"')
    path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")
    os.chmod(path, 0o600)


def _escape_toml_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def load_credentials(fingerprint: str) -> dict[str, str] | None:
    """Return the destination's KEY=VALUE env entries, or None if not configured.

    The `destination` annotation is never returned — it is informational only.
    """
    data = _read_toml()
    section = data.get(fingerprint)
    if section is None:
        return None
    if not isinstance(section, dict):
        raise CredentialsError(
            f"Credentials section '{fingerprint}' in {credentials_path()} must be a table."
        )
    result: dict[str, str] = {}
    for key, value in section.items():
        if key == _DESTINATION_KEY:
            continue
        if not isinstance(value, str):
            raise CredentialsError(
                f"Credentials key {key!r} in section '{fingerprint}' must be a string value."
            )
        result[key] = value
    return result


def save_credentials(fingerprint: str, destination: str, entries: dict[str, str]) -> Path:
    """Create or merge a destination section, preserving other destinations."""
    for key, value in entries.items():
        if not isinstance(key, str) or not key:
            raise CredentialsError("Secret keys must be non-empty strings.")
        if not isinstance(value, str):
            raise CredentialsError(f"Secret value for {key!r} must be a string.")

    data = _read_toml()
    section: dict[str, str] = {}
    existing = data.get(fingerprint)
    if isinstance(existing, dict):
        for key, value in existing.items():
            if isinstance(value, str):
                section[key] = value
    section[_DESTINATION_KEY] = destination
    section.update(entries)
    data[fingerprint] = section
    _write_toml(data)
    return credentials_path()