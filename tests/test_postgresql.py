import os
import subprocess
from types import SimpleNamespace

import pytest

from vecta_agent import postgresql, restic


SOURCE = {
    "engine": "postgresql",
    "host": "db.internal",
    "port": 5432,
    "database": "orders-prod",
    "username": "backup_reader",
    "ssl_mode": "verify-full",
    "dump_format": "custom",
}


def test_builds_typed_pg_dump_argv_and_safe_stable_filename():
    argv = postgresql.build_pg_dump_argv(SOURCE)
    assert argv == [
        "pg_dump", "--no-password", "--host=db.internal", "--port=5432",
        "--username=backup_reader", "--dbname=orders-prod", "-Fc", "-Z", "0",
    ]
    assert postgresql.stdin_filename(SOURCE) == "postgresql-orders-prod.dump"
    assert all(arg != "--password" for arg in argv)


def test_source_values_are_trimmed_consistently_with_backend():
    source = postgresql.validate_source_config({**SOURCE, "host": " db.internal ", "database": " orders ", "username": " backup "})
    assert source["host"] == "db.internal"
    assert source["database"] == "orders"
    assert source["username"] == "backup"


@pytest.mark.parametrize(
    "mutation",
    [
        {"password": "secret"},
        {"host": "-x"},
        {"host": " postgres://db.internal/app "},
        {"host": "*"},
        {"host": "x" * 256},
        {"database": "*"},
        {"username": "*"},
        {"database": " "},
        {"database": "x" * 129},
        {"username": "x" * 129},
        {"port": True},
        {"ssl_mode": "unsafe"},
        {"dump_format": "plain"},
        {"database": "db\n--help"},
    ],
)
def test_rejects_invalid_database_configuration(mutation):
    source = {**SOURCE, **mutation}
    with pytest.raises(postgresql.PostgreSQLConfigError):
        postgresql.validate_source_config(source)


def test_restic_builds_stdin_from_command_without_shell():
    runner = restic.ResticRunner(
        None,
        "s3:bucket/repository",
        stdin_filename=postgresql.stdin_filename(SOURCE),
        source_command=postgresql.build_pg_dump_argv(SOURCE),
    )
    command = runner._build_cmd()
    assert command[:2] == ["restic", "backup"]
    assert command[command.index("--stdin-from-command")] == "--stdin-from-command"
    assert command[command.index("--stdin-filename") + 1] == "postgresql-orders-prod.dump"
    assert command[command.index("--") + 1:] == postgresql.build_pg_dump_argv(SOURCE)


def test_pgpass_escaping_scoped_storage_and_child_environment(tmp_config_dir):
    postgresql.save_pgpass(SOURCE, r"p:a\ss*word")
    path = postgresql.validate_pgpass_file(SOURCE)
    assert path is not None
    assert path.read_text(encoding="utf-8") == r"db.internal:5432:orders-prod:backup_reader:p\:a\\ss*word" + "\n"
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    env = postgresql.child_environment(SOURCE)
    assert env["PGPASSFILE"] == str(path)
    assert env["PGSSLMODE"] == "verify-full"
    assert "PGPASSWORD" not in env
    assert "PGPASSWORD" in postgresql.PG_ENV_UNSET


def test_pgpass_does_not_accept_unsafe_permissions(tmp_config_dir):
    path = postgresql.save_pgpass(SOURCE, "secret")
    if os.name == "posix":
        path.chmod(0o644)
        with pytest.raises(postgresql.PostgreSQLConfigError, match="mode 0600"):
            postgresql.validate_pgpass_file(SOURCE)


def test_connection_probe_uses_temporary_pgpass_and_bounded_schema_dump(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(postgresql.subprocess, "run", fake_run)
    ok, message = postgresql.test_connection(SOURCE, password="p:a\\ss")

    assert ok and not message
    assert captured["command"][-1] == "--schema-only"
    assert captured["timeout"] == postgresql.PG_CONNECTION_TIMEOUT_SECONDS
    assert captured["stdout"] is postgresql.subprocess.DEVNULL
    assert captured["env"]["PGCONNECT_TIMEOUT"] == str(postgresql.PG_CONNECT_TIMEOUT_SECONDS)
    assert not os.path.exists(captured["env"]["PGPASSFILE"])


def test_connection_probe_reports_timeout(monkeypatch):
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(postgresql.subprocess, "run", timeout)
    ok, message = postgresql.test_connection(SOURCE, password="secret")

    assert not ok
    assert f"timed out after {postgresql.PG_CONNECTION_TIMEOUT_SECONDS}s" in message


def test_version_detection_handles_installed_binary_formats():
    assert postgresql._version_tuple("restic 0.17.1 compiled with go") == (0, 17, 1)
    assert postgresql._version_tuple("pg_dump (PostgreSQL) 16.4") == (16, 4)
    assert postgresql._version_tuple("unknown") is None


def test_capability_check_reports_missing_pg_dump(monkeypatch):
    def fake_run(command, **kwargs):
        if command == ["restic", "version"]:
            return SimpleNamespace(returncode=0, stdout="restic 0.17.1", stderr="")
        if command == ["pg_dump", "--version"]:
            raise FileNotFoundError
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(postgresql.subprocess, "run", fake_run)
    assert postgresql.postgresql_support_issue() == "pg_dump_missing"
    assert not postgresql.probe_postgresql_support()


def test_capability_check_reports_outdated_restic(monkeypatch):
    monkeypatch.setattr(
        postgresql.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(returncode=0, stdout="restic 0.16.4", stderr=""),
    )
    assert postgresql.postgresql_support_issue() == "restic_version_unsupported"


def test_capability_check_reports_missing_stdin_options(monkeypatch):
    def fake_run(command, **kwargs):
        if command == ["restic", "version"]:
            return SimpleNamespace(returncode=0, stdout="restic 0.17.1", stderr="")
        if command == ["pg_dump", "--version"]:
            return SimpleNamespace(returncode=0, stdout="pg_dump (PostgreSQL) 16.4", stderr="")
        return SimpleNamespace(returncode=0, stdout="--stdin-from-command", stderr="")

    monkeypatch.setattr(postgresql.subprocess, "run", fake_run)
    assert postgresql.postgresql_support_issue() == "restic_stdin_options_missing"


def test_capability_check_reports_available_when_all_requirements_pass(monkeypatch):
    def fake_run(command, **kwargs):
        if command == ["restic", "version"]:
            return SimpleNamespace(returncode=0, stdout="restic 0.17.1", stderr="")
        if command == ["pg_dump", "--version"]:
            return SimpleNamespace(returncode=0, stdout="pg_dump (PostgreSQL) 16.4", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout="--stdin-from-command\n--stdin-filename",
            stderr="",
        )

    monkeypatch.setattr(postgresql.subprocess, "run", fake_run)
    assert postgresql.postgresql_support_issue() is None
    assert postgresql.probe_postgresql_support()
