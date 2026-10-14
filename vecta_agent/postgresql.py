"""PostgreSQL source configuration, credentials, and capability checks."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from vecta_agent import config
from vecta_agent.postgresql_probe import (
    MIN_PGDUMP_VERSION as MIN_PGDUMP_VERSION,
    MIN_RESTIC_VERSION as MIN_RESTIC_VERSION,
    _version_tuple as _version_tuple,
    pg_dump_version as pg_dump_version,
    postgresql_support_issue as postgresql_support_issue,
    probe_postgresql_support as probe_postgresql_support,
)

PG_CONNECTION_TIMEOUT_SECONDS = 30
PG_CONNECT_TIMEOUT_SECONDS = 10
SSL_MODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
PG_ENV_UNSET = ("PGPASSWORD", "PGSERVICE", "PGSERVICEFILE", "PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGUSER", "PGOPTIONS")
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


class PostgreSQLConfigError(ValueError):
    """A malformed or unsupported PostgreSQL job configuration."""


def validate_source_config(value: Any) -> dict[str, Any]:
    """Validate the exact non-secret PostgreSQL source contract."""
    if not isinstance(value, dict):
        raise PostgreSQLConfigError("PostgreSQL source_config must be an object.")
    expected = {"engine", "host", "port", "database", "username", "ssl_mode", "dump_format"}
    if set(value) != expected:
        raise PostgreSQLConfigError("PostgreSQL source_config has missing or unsupported fields.")
    if value.get("engine") != "postgresql" or value.get("dump_format") != "custom":
        raise PostgreSQLConfigError("Only PostgreSQL custom-format backups are supported.")
    source = dict(value)
    for key in ("host", "database", "username"):
        item = value.get(key)
        if not isinstance(item, str):
            raise PostgreSQLConfigError(f"PostgreSQL {key} must be a non-empty string.")
        item = item.strip()
        max_length = 255 if key == "host" else 128
        if not item or len(item) > max_length or any(c in item for c in "\x00\r\n"):
            raise PostgreSQLConfigError(f"PostgreSQL {key} must be a non-empty value of at most {max_length} characters.")
        if item.startswith("-"):
            raise PostgreSQLConfigError(f"PostgreSQL {key} must not begin with '-'.")
        if item == "*":
            raise PostgreSQLConfigError(f"PostgreSQL {key} must not be the libpq pgpass wildcard '*'.")
        if key == "host" and ("://" in item or "@" in item or "/" in item):
            raise PostgreSQLConfigError("PostgreSQL host must be a hostname or address, not a connection URI.")
        source[key] = item
    port = source.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise PostgreSQLConfigError("PostgreSQL port must be an integer between 1 and 65535.")
    if source.get("ssl_mode") not in SSL_MODES:
        raise PostgreSQLConfigError("PostgreSQL ssl_mode is unsupported.")
    return source


def stdin_filename(source_config: dict[str, Any]) -> str:
    """Stable safe path included in the Restic snapshot for this database."""
    database = _SAFE_COMPONENT.sub("_", source_config["database"]).strip("._-") or "database"
    database = database[:64]
    return f"postgresql-{database}.dump"


def build_pg_dump_argv(source_config: dict[str, Any]) -> list[str]:
    source = validate_source_config(source_config)
    return [
        "pg_dump",
        "--no-password",
        f"--host={source['host']}",
        f"--port={source['port']}",
        f"--username={source['username']}",
        f"--dbname={source['database']}",
        "-Fc",
        "-Z",
        "0",
    ]


def connection_fingerprint(source_config: dict[str, Any]) -> str:
    source = validate_source_config(source_config)
    identity = "\0".join(str(source[key]) for key in ("host", "port", "database", "username"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def pgpass_path(source_config: dict[str, Any]) -> Path:
    return config._config_dir() / f"pgpass-{connection_fingerprint(source_config)}.conf"


def _escape_pgpass_field(value: str) -> str:
    return "".join("\\" + char if char in "\\:" else char for char in value)


def _pgpass_line(source_config: dict[str, Any], password: str) -> str:
    source = validate_source_config(source_config)
    if not isinstance(password, str) or not password or any(c in password for c in "\x00\r\n"):
        raise PostgreSQLConfigError("A non-empty PostgreSQL password is required.")
    return ":".join(
        _escape_pgpass_field(str(value))
        for value in (source["host"], str(source["port"]), source["database"], source["username"], password)
    ) + "\n"


def save_pgpass(source_config: dict[str, Any], password: str) -> Path:
    """Write one connection-scoped libpq password file with mode 0600."""
    source = validate_source_config(source_config)
    path = pgpass_path(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = _pgpass_line(source, password)
    fd, temporary = tempfile.mkstemp(prefix=".pgpass-", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(line)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def test_connection(
    source_config: dict[str, Any],
    *,
    password: str | None = None,
    password_file: Path | None = None,
) -> tuple[bool, str]:
    """Check PostgreSQL credentials with a bounded, schema-only pg_dump."""
    source = validate_source_config(source_config)
    temporary: str | None = None
    if password_file is None:
        if password is None:
            raise PostgreSQLConfigError("A PostgreSQL password or password file is required to test the connection.")
        line = _pgpass_line(source, password)
        fd, temporary = tempfile.mkstemp(prefix="vecta-pgpass-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(line)
            os.chmod(temporary, 0o600)
        except Exception:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise
        password_file = Path(temporary)

    env = {
        **os.environ,
        "PGPASSFILE": str(password_file),
        "PGSSLMODE": source["ssl_mode"],
        "PGCONNECT_TIMEOUT": str(PG_CONNECT_TIMEOUT_SECONDS),
    }
    command = [*build_pg_dump_argv(source), "--schema-only"]
    try:
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                timeout=PG_CONNECTION_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            return False, "pg_dump executable not found on PATH."
        except subprocess.TimeoutExpired:
            return False, f"PostgreSQL connection test timed out after {PG_CONNECTION_TIMEOUT_SECONDS}s."
        if result.returncode == 0:
            return True, ""
        return False, (result.stderr or "pg_dump could not connect to PostgreSQL.").strip()
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def validate_pgpass_file(source_config: dict[str, Any]) -> Path | None:
    path = pgpass_path(source_config)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise PostgreSQLConfigError("The local PostgreSQL password file is not a safe regular file.")
    if os.name == "posix":
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise PostgreSQLConfigError("The local PostgreSQL password file must have mode 0600.")
        if hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
            raise PostgreSQLConfigError("The local PostgreSQL password file must be owned by the agent user.")
    return path


def child_environment(source_config: dict[str, Any]) -> dict[str, str]:
    source = validate_source_config(source_config)
    password_file = validate_pgpass_file(source)
    if password_file is None:
        raise PostgreSQLConfigError("PostgreSQL credentials are not configured. Run 'vecta-agent setup <JOB_ID>'.")
    return {
        "PGPASSFILE": str(password_file),
        "PGSSLMODE": source["ssl_mode"],
    }
