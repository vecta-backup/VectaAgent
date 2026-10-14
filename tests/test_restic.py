import subprocess
import threading
import time
import io
from types import SimpleNamespace

from vecta_agent import restic


class FakeResult:
    def __init__(self, exit_code, stderr_tail="", timed_out=False, cancelled=False, stdout=""):
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail
        self.timed_out = timed_out
        self.cancelled = cancelled
        self.stdout = stdout


class TestRepoOptions:
    def test_sftp_with_port(self):
        assert restic.repo_options("sftp:user@host:/path", 2222) == [
            "-o", "sftp.command=ssh -p 2222 user@host -s sftp",
        ]

    def test_sftp_with_port_keeps_sftp_subsystem(self):
        # restic does not append `-s sftp` when sftp.command is set — verified
        # against restic sftp.go buildSSHCommand.
        opts = restic.repo_options("sftp:user@host:/path", 2222)
        assert opts[1].endswith(" -s sftp")

    def test_sftp_without_port(self):
        assert restic.repo_options("sftp:user@host:/path", None) == []

    def test_sftp_url_format_uses_url_port(self):
        # sftp://user@host:port/path: restic reads the port from the URL.
        assert restic.repo_options("sftp://user@host:2222/path", 2222) == []

    def test_sftp_domain_confined_user(self):
        assert restic.repo_options("sftp:user@domain@host:/path", 2222) == [
            "-o", "sftp.command=ssh -p 2222 user@domain@host -s sftp",
        ]

    def test_malformed_sftp_port_is_loud(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            assert restic.repo_options("sftp::/path", 2222) == []
        assert "Could not parse the SFTP destination" in caplog.text

    def test_non_sftp_port_ignored(self):
        assert restic.repo_options("/mnt/backups", 2222) == []
        assert restic.repo_options("s3:s3.amazonaws.com/bucket", 2222) == []

    def test_backup_cmd_includes_sftp_command(self):
        runner = restic.ResticRunner("/src", "sftp:user@host:/path", port=2222, job_id="job-7")
        assert runner._build_cmd() == [
            "restic",
            "backup",
            "--json",
            "--tag=job:job-7",
            "-o", "sftp.command=ssh -p 2222 user@host -s sftp",
            "--repo",
            "sftp:user@host:/path",
            "/src",
        ]

    def test_excludes_are_individual_argv_values(self):
        runner = restic.ResticRunner(
            "/src", "/repo", exclude_patterns=["*.log", "dir with space/quote'*"], job_id="job-7"
        )
        assert runner._build_cmd() == [
            "restic", "backup", "--json", "--tag=job:job-7", "--exclude=*.log",
            "--exclude=dir with space/quote'*", "--repo", "/repo", "/src",
        ]

    def test_empty_excludes_preserve_legacy_argv(self):
        assert restic.ResticRunner("/src", "/repo", job_id="legacy-job")._build_cmd() == [
            "restic", "backup", "--json", "--tag=job:legacy-job", "--repo", "/repo", "/src",
        ]

    def test_backup_and_forget_commands_are_scoped_to_job(self):
        runner = restic.ResticRunner("/src", "/repo", job_id="job-42")
        backup_argv = runner._build_cmd()
        forget_argv = restic.forget_args(
            "/repo",
            {"keep_within_days": 7, "keep_daily": 0, "keep_weekly": 0, "keep_monthly": 0},
            job_id="job-42",
        )

        assert "--tag=job:job-42" in backup_argv
        assert "--tag=job:job-42" in forget_argv

    def test_exclude_patterns_are_validated_and_deduplicated(self):
        assert restic.validate_exclude_patterns([" *.log ", "*.log", "cache/*"]) == ["*.log", "cache/*"]
        for invalid in (["-x"], ["!include"], ["bad\npattern"], [""], ["x" * 257], [1]):
            try:
                restic.validate_exclude_patterns(invalid)
            except ValueError:
                pass
            else:
                raise AssertionError(f"Expected invalid excludes to fail: {invalid!r}")

    def test_stdin_runner_removes_ambient_pg_environment(self, monkeypatch):
        captured = {}

        class FakeProc:
            stdout = io.StringIO("")
            stderr = io.StringIO("")

            def __init__(self, command, **kwargs):
                captured["command"] = command
                captured["env"] = kwargs["env"]
                captured["shell"] = kwargs["shell"]

        monkeypatch.setattr(restic, "os", SimpleNamespace(name="nt", environ={"PGPASSWORD": "ambient"}))
        monkeypatch.setattr(restic.subprocess, "Popen", FakeProc)
        runner = restic.ResticRunner(
            None, "/repo", env={"PGPASSFILE": "/secure/pgpass"},
            stdin_filename="postgresql-db.dump", source_command=["pg_dump", "--no-password"],
            unset_env=("PGPASSWORD", "PGSERVICE"),
            job_id="db-job",
        )
        runner.start()
        runner._stderr_thread.join(timeout=1)
        assert captured["env"]["PGPASSFILE"] == "/secure/pgpass"
        assert "PGPASSWORD" not in captured["env"]
        assert captured["shell"] is False

    def test_terminate_signals_process_group_and_reaps_direct_child(self, monkeypatch):
        calls = []

        class FakeProc:
            pid = 1234
            stdout = None
            stderr = None
            returncode = None

            def __init__(self):
                self.wait_calls = 0

            def wait(self, timeout=None):
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise subprocess.TimeoutExpired("restic", timeout)
                self.returncode = -9
                return self.returncode

        proc = FakeProc()
        monkeypatch.setattr(restic, "os", SimpleNamespace(name="posix", killpg=lambda pid, sig: calls.append((pid, sig))))
        monkeypatch.setattr(restic, "signal", SimpleNamespace(SIGTERM=15, SIGKILL=9))
        runner = restic.ResticRunner("/src", "/repo", job_id="job-1")
        runner._proc = proc
        runner.terminate(grace_seconds=0.01)
        assert calls == [(1234, 15), (1234, 9)]
        assert proc.wait_calls == 2
        assert proc.returncode == -9

    def test_check_repo_uses_port(self, monkeypatch):
        calls = []

        def fake_run_restic(args, env=None, **kw):
            calls.append(args)
            return FakeResult(0)

        monkeypatch.setattr(restic, "run_restic", fake_run_restic)
        restic.check_repo("sftp:user@host:/path", port=2222)
        assert calls[0] == [
            "-o", "sftp.command=ssh -p 2222 user@host -s sftp",
            "cat", "config", "--repo", "sftp:user@host:/path",
        ]

    def test_init_repo_uses_port(self, monkeypatch):
        calls = []

        def fake_run_restic(args, env=None, **kw):
            calls.append(args)
            return FakeResult(0)

        monkeypatch.setattr(restic, "run_restic", fake_run_restic)
        restic.init_repo("sftp:user@host:/path", port=2222)
        assert calls[0] == [
            "-o", "sftp.command=ssh -p 2222 user@host -s sftp",
            "init", "--repo", "sftp:user@host:/path",
        ]


class TestRunRestic:
    def test_command_and_env(self, monkeypatch):
        captured = {}

        class FakeProc:
            returncode = 0

            def __init__(self, *args, **kwargs):
                captured["args"] = args[0]
                captured["env"] = kwargs["env"]
                captured["shell"] = kwargs["shell"]

            def communicate(self, timeout=None):
                captured["timeout"] = timeout
                return "out", "err"

        monkeypatch.setattr(restic.subprocess, "Popen", FakeProc)
        result = restic.run_restic(["init", "--repo", "r"], env={"K": "V"})
        assert captured["args"] == ["restic", "init", "--repo", "r"]
        assert captured["env"]["K"] == "V"
        assert captured["shell"] is False
        assert result.exit_code == 0
        assert result.stdout == "out"
        assert result.stderr_tail == "err"
        assert result.timed_out is False

    def test_retention_args_and_lock_recovery_retry(self, monkeypatch):
        assert restic.forget_args("sftp:user@host:/repo", {"keep_within_days": 14, "keep_daily": 0, "keep_weekly": 0, "keep_monthly": 0}, 2222, job_id="job-7") == [
            "-o", "sftp.command=ssh -p 2222 user@host -s sftp", "forget", "--json", "--prune", "--tag=job:job-7",
            "--keep-within", "14d", "--repo", "sftp:user@host:/repo",
        ]
        assert restic.forget_args("r", {"keep_within_days": None, "keep_daily": 2, "keep_weekly": 0, "keep_monthly": 3}, job_id="job-7") == [
            "forget", "--json", "--prune", "--tag=job:job-7", "--keep-daily", "2", "--keep-monthly", "3", "--repo", "r",
        ]
        calls = []
        outcomes = [
            FakeResult(1, "repository is locked"),
            FakeResult(0),
            FakeResult(0, stdout='[{"remove":[{"id":"deleted-snapshot"}]}]'),
        ]

        def fake_run(args, env=None, **kwargs):
            calls.append((args, env, kwargs))
            return outcomes.pop(0)

        monkeypatch.setattr(restic, "run_restic", fake_run)
        result = restic.run_forget("repo", {"keep_within_days": 7, "keep_daily": 0, "keep_weekly": 0, "keep_monthly": 0}, env={"RESTIC_PASSWORD": "secret"}, job_id="job-7")
        assert result.exit_code == 0
        assert result.stdout == '[{"remove":[{"id":"deleted-snapshot"}]}]'
        assert [call[0][0] for call in calls] == ["forget", "unlock", "forget"]
        assert calls[0][0] == calls[2][0]
        assert all(call[1] == {"RESTIC_PASSWORD": "secret"} for call in calls)
        assert all("--remove-all" not in call[0] for call in calls)
        assert "unlock exited 0" in result.recovery_message

    def test_lock_detector_is_specific(self):
        for message in (
            "Fatal: repository is already locked by another process",
            "unable to create lock in backend",
            "Lock file /repo/locks/abc exists",
        ):
            assert restic.is_repository_lock_error(message)
        for message in ("permission denied", "wrong password", "network lock service unavailable", "lock cleanup complete"):
            assert not restic.is_repository_lock_error(message)

    def test_retention_validation_is_strict(self):
        for policy in (
            {"keep_within_days": True},
            {"keep_within_days": 7.0},
            {"keep_within_days": "7"},
            {"keep_within_days": 7, "keep_daily": 1},
            {"keep_daily": 0, "keep_weekly": 0, "keep_monthly": 0},
            {"keep_daily": 1, "extra": 2},
        ):
            try:
                restic.forget_args("repo", policy, job_id="job-7")
            except ValueError:
                pass
            else:
                raise AssertionError(f"Expected invalid retention policy to fail: {policy!r}")

    def test_default_timeout_is_120(self, monkeypatch):
        captured = {}

        class FakeProc:
            returncode = 0

            def __init__(self, *args, **kwargs):
                pass

            def communicate(self, timeout=None):
                captured["timeout"] = timeout
                return "", ""

        monkeypatch.setattr(restic.subprocess, "Popen", FakeProc)
        restic.run_restic(["cat", "config", "--repo", "r"])
        assert captured["timeout"] == restic.PROBE_TIMEOUT_SECONDS
        assert restic.PROBE_TIMEOUT_SECONDS == 120

    def test_timeout_kills_process_and_reports_124(self, monkeypatch):
        killed = []

        class HangingProc:
            returncode = None

            def __init__(self, *args, **kwargs):
                pass

            def communicate(self, timeout=None):
                if not killed:
                    raise subprocess.TimeoutExpired(cmd="restic", timeout=timeout)
                return "", ""

            def kill(self):
                killed.append(True)

        monkeypatch.setattr(restic.subprocess, "Popen", HangingProc)
        monkeypatch.setattr(restic, "_kill_process_group", lambda proc: proc.kill())
        result = restic.run_restic(["cat", "config", "--repo", "r"], timeout_seconds=3)
        assert killed == [True]
        assert result.timed_out is True
        assert result.exit_code == 124
        assert "timed out after 3s" in result.stderr_tail

    def test_cancel_event_kills_process_and_reports_cancelled(self, monkeypatch):
        killed = []
        event = threading.Event()

        class CancellableProc:
            returncode = None

            def __init__(self, *args, **kwargs):
                pass

            def communicate(self, timeout=None):
                if not killed:
                    event.set()
                    raise subprocess.TimeoutExpired(cmd="restic", timeout=timeout)
                return "", ""

            def kill(self):
                killed.append(True)

        monkeypatch.setattr(restic.subprocess, "Popen", CancellableProc)
        monkeypatch.setattr(restic, "_kill_process_group", lambda proc: proc.kill())
        result = restic.run_restic(["cat", "config", "--repo", "r"], cancel_event=event)
        assert killed == [True]
        assert result.cancelled is True
        assert result.timed_out is False
        assert result.exit_code == 130
        assert "cancelled" in result.stderr_tail

    def test_cancel_mode_still_enforces_timeout(self, monkeypatch):
        killed = []

        class HangingProc:
            returncode = None

            def __init__(self, *args, **kwargs):
                pass

            def communicate(self, timeout=None):
                if not killed:
                    time.sleep(timeout)
                    raise subprocess.TimeoutExpired(cmd="restic", timeout=timeout)
                return "", ""

            def kill(self):
                killed.append(True)

        monkeypatch.setattr(restic.subprocess, "Popen", HangingProc)
        monkeypatch.setattr(restic, "_kill_process_group", lambda proc: proc.kill())
        result = restic.run_restic(
            ["cat", "config", "--repo", "r"],
            timeout_seconds=0.3,
            cancel_event=threading.Event(),  # never set
        )
        assert killed == [True]
        assert result.timed_out is True
        assert result.exit_code == 124


class TestCheckRepo:
    def test_exists(self, monkeypatch):
        monkeypatch.setattr(restic, "run_restic", lambda *a, **kw: FakeResult(0))
        assert restic.check_repo("r1").exists is True

    def test_missing_exit_10(self, monkeypatch):
        monkeypatch.setattr(
            restic, "run_restic",
            lambda *a, **kw: FakeResult(10, "repository does not exist"),
        )
        check = restic.check_repo("r1")
        assert check.exists is False

    def test_missing_old_restic_stderr(self, monkeypatch):
        monkeypatch.setattr(
            restic, "run_restic",
            lambda *a, **kw: FakeResult(
                1, "unable to open config file: Stat: /path/config: no such file or directory"
            ),
        )
        assert restic.check_repo("r1").exists is False

    def test_missing_exit1_repository_does_not_exist(self, monkeypatch):
        monkeypatch.setattr(
            restic, "run_restic",
            lambda *a, **kw: FakeResult(1, "repository does not exist"),
        )
        assert restic.check_repo("r1").exists is False

    def test_permission_denied_is_indeterminate(self, monkeypatch):
        monkeypatch.setattr(
            restic, "run_restic",
            lambda *a, **kw: FakeResult(
                1, "unable to open config file: open /path/config: permission denied"
            ),
        )
        check = restic.check_repo("r1")
        assert check.exists is None

    def test_indeterminate_network_error(self, monkeypatch):
        monkeypatch.setattr(
            restic, "run_restic",
            lambda *a, **kw: FakeResult(1, "connection refused"),
        )
        check = restic.check_repo("r1")
        assert check.exists is None
        assert check.stderr_tail == "connection refused"

    def test_indeterminate_wrong_password(self, monkeypatch):
        monkeypatch.setattr(restic, "run_restic", lambda *a, **kw: FakeResult(12, "wrong password"))
        assert restic.check_repo("r1").exists is None

    def test_timeout_is_indeterminate_and_flagged(self, monkeypatch):
        monkeypatch.setattr(
            restic, "run_restic",
            lambda *a, **kw: FakeResult(
                124, "restic cat timed out after 120s and was killed", timed_out=True
            ),
        )
        check = restic.check_repo("r1")
        assert check.exists is None
        assert check.timed_out is True

    def test_cancelled_is_indeterminate_and_flagged(self, monkeypatch):
        monkeypatch.setattr(
            restic, "run_restic",
            lambda *a, **kw: FakeResult(
                130, "restic cat was cancelled by user", cancelled=True
            ),
        )
        check = restic.check_repo("r1")
        assert check.exists is None
        assert check.cancelled is True
        assert check.timed_out is False

    def test_passes_default_timeout(self, monkeypatch):
        captured = {}

        def fake_run_restic(args, env=None, **kw):
            captured.update(kw)
            return FakeResult(0)

        monkeypatch.setattr(restic, "run_restic", fake_run_restic)
        restic.check_repo("r1")
        assert captured["timeout_seconds"] == restic.PROBE_TIMEOUT_SECONDS

    def test_passes_cancel_event(self, monkeypatch):
        captured = {}

        def fake_run_restic(args, env=None, **kw):
            captured.update(kw)
            return FakeResult(0)

        monkeypatch.setattr(restic, "run_restic", fake_run_restic)
        event = threading.Event()
        restic.check_repo("r1", cancel_event=event)
        assert captured["cancel_event"] is event


class TestInitRepo:
    def test_init_command(self, monkeypatch):
        calls = []

        def fake_run_restic(args, env=None, **kw):
            calls.append((args, env))
            return FakeResult(0)

        monkeypatch.setattr(restic, "run_restic", fake_run_restic)
        restic.init_repo("dest")
        assert calls[0][0] == ["init", "--repo", "dest"]

    def test_passes_cancel_event(self, monkeypatch):
        captured = {}

        def fake_run_restic(args, env=None, **kw):
            captured.update(kw)
            return FakeResult(0)

        monkeypatch.setattr(restic, "run_restic", fake_run_restic)
        event = threading.Event()
        restic.init_repo("dest", cancel_event=event)
        assert captured["cancel_event"] is event


class TestParseSummary:
    def test_parse_summary(self):
        obj = {
            "message_type": "summary",
            "snapshot_id": "snap123",
            "total_files_processed": 100,
            "total_bytes_processed": 1024,
            "data_added_packed": 512,
            "data_added": 600,
        }
        result = restic.parse_summary(obj)
        assert result["snapshot_id"] == "snap123"
        assert result["files_processed"] == 100
        assert result["bytes_processed"] == 1024
        assert result["transferred_bytes"] == 512

    def test_parse_summary_falls_back_to_data_added(self):
        obj = {
            "message_type": "summary",
            "snapshot_id": "snap456",
            "total_files_processed": 10,
            "total_bytes_processed": 2048,
            "data_added": 256,
        }
        result = restic.parse_summary(obj)
        assert result["transferred_bytes"] == 256

    def test_parse_summary_no_transferred(self):
        obj = {
            "message_type": "summary",
            "snapshot_id": "snap789",
            "total_files_processed": 5,
            "total_bytes_processed": 100,
        }
        result = restic.parse_summary(obj)
        assert result["transferred_bytes"] is None


class TestComputeProgress:
    def test_percent_done(self):
        assert restic.compute_progress({"percent_done": 0.456}) == 45
        assert restic.compute_progress({"percent_done": 1.0}) == 99
        assert restic.compute_progress({"percent_done": 0.0}) == 0

    def test_bytes_ratio(self):
        assert restic.compute_progress({"bytes_done": 50, "total_bytes": 100}) == 50

    def test_no_progress(self):
        assert restic.compute_progress({"bytes_done": 50}) is None
