"""Local Restic and pg_dump capability probes for PostgreSQL backups."""

from __future__ import annotations

import re
import subprocess

MIN_PGDUMP_VERSION = (9, 0)
MIN_RESTIC_VERSION = (0, 17, 0)


def _version_tuple(text: str) -> tuple[int, ...] | None:
    match = re.search(
        r"(?:restic\s+(?:version\s+)?|pg_dump\s+\(PostgreSQL\)\s+)(?:v)?(\d+(?:\.\d+){1,2})",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def postgresql_support_issue() -> str | None:
    """Return a safe diagnostic reason when local PostgreSQL support is unavailable."""
    try:
        restic_version = subprocess.run(["restic", "version"], capture_output=True, text=True, timeout=5)
    except FileNotFoundError:
        return "restic_missing"
    except (OSError, subprocess.TimeoutExpired):
        return "probe_failed"
    if restic_version.returncode:
        return "restic_check_failed"

    restic_v = _version_tuple(restic_version.stdout + restic_version.stderr)
    if restic_v is None:
        return "restic_version_unrecognized"
    if restic_v < MIN_RESTIC_VERSION:
        return "restic_version_unsupported"

    try:
        pgdump_version = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True, timeout=5)
    except FileNotFoundError:
        return "pg_dump_missing"
    except (OSError, subprocess.TimeoutExpired):
        return "probe_failed"
    if pgdump_version.returncode:
        return "pg_dump_check_failed"

    pgdump_v = _version_tuple(pgdump_version.stdout + pgdump_version.stderr)
    if pgdump_v is None:
        return "pg_dump_version_unrecognized"
    if pgdump_v < MIN_PGDUMP_VERSION:
        return "pg_dump_version_unsupported"

    try:
        backup_help = subprocess.run(["restic", "backup", "--help"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return "probe_failed"
    if backup_help.returncode:
        return "restic_help_check_failed"

    help_text = backup_help.stdout + backup_help.stderr
    if "--stdin-from-command" not in help_text or "--stdin-filename" not in help_text:
        return "restic_stdin_options_missing"
    return None


def probe_postgresql_support() -> bool:
    """Return whether local Restic and pg_dump satisfy backup requirements."""
    return postgresql_support_issue() is None


def pg_dump_version() -> tuple[int, ...] | None:
    """Return the installed pg_dump version, or None when unavailable/old."""
    try:
        result = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    version = _version_tuple(result.stdout + result.stderr)
    return version if version and version >= MIN_PGDUMP_VERSION else None
