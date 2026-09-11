from vecta_agent import restic


class FakeResult:
    def __init__(self, exit_code, stderr_tail=""):
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail


class TestRepoOptions:
    def test_sftp_with_port(self):
        assert restic.repo_options("sftp:user@host:/path", 2222) == [
            "-o", "sftp.args=-p 2222",
        ]

    def test_sftp_without_port(self):
        assert restic.repo_options("sftp:user@host:/path", None) == []

    def test_non_sftp_port_ignored(self):
        assert restic.repo_options("/mnt/backups", 2222) == []
        assert restic.repo_options("s3:s3.amazonaws.com/bucket", 2222) == []

    def test_backup_cmd_includes_sftp_args(self):
        runner = restic.ResticRunner("/src", "sftp:user@host:/path", port=2222)
        assert runner._build_cmd() == [
            "restic",
            "backup",
            "--json",
            "-o", "sftp.args=-p 2222",
            "--repo",
            "sftp:user@host:/path",
            "/src",
        ]

    def test_check_repo_uses_port(self, monkeypatch):
        calls = []

        def fake_run_restic(args, env=None):
            calls.append(args)
            return FakeResult(0)

        monkeypatch.setattr(restic, "run_restic", fake_run_restic)
        restic.check_repo("sftp:user@host:/path", port=2222)
        assert calls[0] == [
            "-o", "sftp.args=-p 2222", "cat", "config", "--repo", "sftp:user@host:/path",
        ]

    def test_init_repo_uses_port(self, monkeypatch):
        calls = []

        def fake_run_restic(args, env=None):
            calls.append(args)
            return FakeResult(0)

        monkeypatch.setattr(restic, "run_restic", fake_run_restic)
        restic.init_repo("sftp:user@host:/path", port=2222)
        assert calls[0] == [
            "-o", "sftp.args=-p 2222", "init", "--repo", "sftp:user@host:/path",
        ]


class TestRunRestic:
    def test_command_and_env(self, monkeypatch):
        captured = {}

        class FakeProc:
            returncode = 0

            def __init__(self, *args, **kwargs):
                captured["args"] = args[0]
                captured["env"] = kwargs["env"]

            def communicate(self):
                return "out", "err"

        monkeypatch.setattr(restic.subprocess, "Popen", FakeProc)
        result = restic.run_restic(["init", "--repo", "r"], env={"K": "V"})
        assert captured["args"] == ["restic", "init", "--repo", "r"]
        assert captured["env"]["K"] == "V"
        assert result.exit_code == 0
        assert result.stderr_tail == "err"


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


class TestInitRepo:
    def test_init_command(self, monkeypatch):
        calls = []

        def fake_run_restic(args, env=None):
            calls.append((args, env))
            return FakeResult(0)

        monkeypatch.setattr(restic, "run_restic", fake_run_restic)
        restic.init_repo("dest")
        assert calls[0][0] == ["init", "--repo", "dest"]


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
