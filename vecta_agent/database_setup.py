"""Local PostgreSQL source and credential preflight for setup."""

from __future__ import annotations

import getpass
import sys
from typing import Any

from vecta_agent import postgresql


def prepare_database_setup(
    job: dict[str, Any], backup_type: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a database job and test its local PostgreSQL credentials."""
    if backup_type != "database":
        if backup_type != "files":
            print("Error: unsupported backup type.", file=sys.stderr)
            raise SystemExit(1)
        return None, None

    try:
        database_config = postgresql.validate_source_config(job.get("source_config"))
    except postgresql.PostgreSQLConfigError as exc:
        print(f"Error: invalid PostgreSQL setup configuration: {exc}", file=sys.stderr)
        raise SystemExit(1)
    if job.get("source") is not None:
        print("Error: database setup must not include a filesystem source.", file=sys.stderr)
        raise SystemExit(1)
    if postgresql.pg_dump_version() is None:
        print(
            "Error: pg_dump 9.0 or newer is required and must be available on PATH.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if not postgresql.probe_postgresql_support():
        print(
            "Error: PostgreSQL backups require Restic 0.17.0 or newer with "
            "--stdin-from-command and --stdin-filename support.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    try:
        existing_pgpass = postgresql.validate_pgpass_file(database_config)
    except postgresql.PostgreSQLConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)

    database_password: str | None = None
    if existing_pgpass is None:
        database_password = getpass.getpass("PostgreSQL database password: ")
        confirm = getpass.getpass("Confirm PostgreSQL database password: ")
        if not database_password or database_password != confirm:
            print("Error: PostgreSQL passwords must be non-empty and match.", file=sys.stderr)
            raise SystemExit(1)
    print(
        "Testing PostgreSQL connection "
        f"(up to {postgresql.PG_CONNECTION_TIMEOUT_SECONDS} seconds)..."
    )
    pg_ok, pg_message = postgresql.test_connection(
        database_config,
        password=database_password,
        password_file=existing_pgpass,
    )
    if not pg_ok:
        print(f"Error: PostgreSQL connection test failed: {pg_message}", file=sys.stderr)
        raise SystemExit(1)
    print("PostgreSQL connection successful.")
    return database_config, database_password
