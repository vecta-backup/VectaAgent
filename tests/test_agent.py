import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from vecta_agent import agent, api, config, restic


class FakeApiClient:
    def __init__(self, agent_id, api_key, base_url=None):
        self.agent_id = agent_id
        self.api_key = api_key
        self.base_url = base_url
        self.me_called = False
        self.jobs: list[dict] = []
        self.status_reports: list[dict] = []
        self.cancel_returns: list[dict] = []
        self.cancel_call_count = 0

    def me(self):
        self.me_called = True
        return {"agent_id": self.agent_id, "name": "test", "is_active": True}

    def fetch_jobs(self):
        return self.jobs

    def report_status(self, job_id, payload):
        self.status_reports.append({"job_id": job_id, **payload})

    def cancel_status(self, job_id):
        self.cancel_call_count += 1
        if self.cancel_returns:
            return self.cancel_returns.pop(0)
        return {"cancel_requested": False}


class FakeRunner:
    def __init__(self, source, destination, env=None):
        self.source = source
        self.destination = destination
        self.env = env or {}
        self.terminated = False
        self.exit_code = 0
        self.stderr = ""
        self.summary: dict | None = None
        self.status_count = 3

    def start(self):
        pass

    def stream(self):
        for i in range(self.status_count):
            if self.terminated:
                return
            yield {
                "message_type": "status",
                "percent_done": (i + 1) * 0.2,
                "files_done": i + 1,
                "bytes_done": (i + 1) * 1000,
            }
            time.sleep(0.01)
        if self.summary:
            yield self.summary

    def terminate(self, grace_seconds=10):
        self.terminated = True

    def wait(self):
        return self.exit_code

    def stderr_tail(self, max_lines=20):
        return self.stderr


def make_config(tmp_config_dir):
    cfg = config.Config(agent_id="a1", api_key="vc_" + "k" * 64)
    config.save(cfg)
    (tmp_config_dir / "restic.env").write_text("RESTIC_PASSWORD=test\n")
    return cfg


def patch_repo(monkeypatch, exists=True, init_stderr=""):
    init_calls = []
    monkeypatch.setattr(agent.restic, "check_repo", lambda *a, **kw: restic.RepoCheck(exists))
    monkeypatch.setattr(
        agent.restic,
        "init_repo",
        lambda *a, **kw: init_calls.append(a) or restic.ResticResult(exit_code=0, stderr_tail=init_stderr),
    )
    return init_calls


class TestRunAgent:
    def test_no_jobs(self, tmp_config_dir, monkeypatch, caplog):
        make_config(tmp_config_dir)
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        agent.run_agent()
        assert fake.me_called
        assert fake.status_reports == []
        assert "No jobs due" in caplog.text

    def test_job_success(self, tmp_config_dir, monkeypatch, caplog):
        make_config(tmp_config_dir)
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "j1", "source": "/src", "destination": "/dest"}]

        def fake_runner(source, destination, env=None):
            r = FakeRunner(source, destination, env)
            r.summary = {
                "message_type": "summary",
                "snapshot_id": "snap1",
                "total_files_processed": 10,
                "total_bytes_processed": 5000,
                "data_added_packed": 1000,
            }
            return r

        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        patch_repo(monkeypatch)
        monkeypatch.setattr(agent.restic, "ResticRunner", fake_runner)
        monkeypatch.setattr(agent, "PROGRESS_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(agent, "CANCEL_INTERVAL_SECONDS", 0.05)

        agent.run_agent()

        assert fake.status_reports[0]["status"] == "running"
        success = fake.status_reports[-1]
        assert success["status"] == "success"
        assert success["snapshot_id"] == "snap1"
        assert success["files_processed"] == 10
        assert success["bytes_processed"] == 5000
        assert success["transferred_bytes"] == 1000

    def test_job_failure(self, tmp_config_dir, monkeypatch, caplog):
        make_config(tmp_config_dir)
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "j2", "source": "/src", "destination": "/dest"}]

        def fake_runner(source, destination, env=None):
            r = FakeRunner(source, destination, env)
            r.exit_code = 1
            r.stderr = "restic failed"
            return r

        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        patch_repo(monkeypatch)
        monkeypatch.setattr(agent.restic, "ResticRunner", fake_runner)
        monkeypatch.setattr(agent, "PROGRESS_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(agent, "CANCEL_INTERVAL_SECONDS", 0.05)

        agent.run_agent()

        assert fake.status_reports[0]["status"] == "running"
        failure = fake.status_reports[-1]
        assert failure["status"] == "failed"
        assert failure["exit_code"] == 1
        assert "restic failed" in failure["message"]

    def test_job_cancellation(self, tmp_config_dir, monkeypatch, caplog):
        make_config(tmp_config_dir)
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "j3", "source": "/src", "destination": "/dest"}]
        fake.cancel_returns = [
            {"cancel_requested": False},
            {"cancel_requested": True},
        ]

        def fake_runner(source, destination, env=None):
            r = FakeRunner(source, destination, env)
            r.status_count = 100  # long-running
            r.exit_code = 130
            return r

        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        patch_repo(monkeypatch)
        monkeypatch.setattr(agent.restic, "ResticRunner", fake_runner)
        monkeypatch.setattr(agent, "PROGRESS_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(agent, "CANCEL_INTERVAL_SECONDS", 0.05)

        agent.run_agent()

        assert fake.status_reports[0]["status"] == "running"
        failure = fake.status_reports[-1]
        assert failure["status"] == "failed"
        assert failure["exit_code"] == 130
        assert failure["message"] == "Cancelled by user"

    def test_auth_error_exits(self, tmp_config_dir, monkeypatch):
        make_config(tmp_config_dir)

        class FailingClient:
            def __init__(self, *a, **kw):
                pass

            def me(self):
                raise api.AgentAuthError("bad creds")

        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: FailingClient())

        with pytest.raises(api.AgentAuthError):
            agent.run_agent()

    def test_restic_env_loaded(self, tmp_config_dir, monkeypatch):
        make_config(tmp_config_dir)
        env_path = tmp_config_dir / "restic.env"
        env_path.write_text('RESTIC_PASSWORD=secret\nAWS_ACCESS_KEY_ID=AKIA\n')

        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "j1", "source": "/src", "destination": "/dest"}]

        captured_env = {}

        def fake_runner(source, destination, env=None):
            captured_env.update(env or {})
            r = FakeRunner(source, destination, env)
            r.summary = {"message_type": "summary", "snapshot_id": "s1"}
            return r

        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        patch_repo(monkeypatch)
        monkeypatch.setattr(agent.restic, "ResticRunner", fake_runner)
        monkeypatch.setattr(agent, "PROGRESS_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(agent, "CANCEL_INTERVAL_SECONDS", 0.05)

        agent.run_agent()

        assert captured_env["RESTIC_PASSWORD"] == "secret"
        assert captured_env["AWS_ACCESS_KEY_ID"] == "AKIA"

    def test_missing_password_reports_failed(self, tmp_config_dir, monkeypatch):
        make_config(tmp_config_dir)
        (tmp_config_dir / "restic.env").write_text("AWS_ACCESS_KEY_ID=AKIA\n")
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "j5", "source": "/src", "destination": "/dest"}]
        runner_called = []

        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        monkeypatch.setattr(agent.restic, "ResticRunner", lambda *a, **kw: runner_called.append(a))
        monkeypatch.setattr(agent.restic, "check_repo", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not probe")))

        agent.run_agent()

        assert runner_called == []
        assert fake.status_reports[0]["status"] == "running"
        failure = fake.status_reports[-1]
        assert failure["status"] == "failed"
        assert failure["exit_code"] == 1
        assert "repo init" in failure["message"]

    def test_repo_missing_auto_inits_then_backs_up(self, tmp_config_dir, monkeypatch):
        make_config(tmp_config_dir)
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "j6", "source": "/src", "destination": "/dest"}]
        init_calls = patch_repo(monkeypatch, exists=False)

        def fake_runner(source, destination, env=None):
            r = FakeRunner(source, destination, env)
            r.summary = {"message_type": "summary", "snapshot_id": "snap1"}
            return r

        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        monkeypatch.setattr(agent.restic, "ResticRunner", fake_runner)
        monkeypatch.setattr(agent, "PROGRESS_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(agent, "CANCEL_INTERVAL_SECONDS", 0.05)

        agent.run_agent()

        assert len(init_calls) == 1
        assert init_calls[0][0] == "/dest"
        success = fake.status_reports[-1]
        assert success["status"] == "success"
        assert success["snapshot_id"] == "snap1"

    def test_repo_init_failure_reports_failed(self, tmp_config_dir, monkeypatch):
        make_config(tmp_config_dir)
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "j7", "source": "/src", "destination": "/dest"}]

        init_calls = []
        monkeypatch.setattr(agent.restic, "check_repo", lambda *a, **kw: restic.RepoCheck(False))
        monkeypatch.setattr(
            agent.restic,
            "init_repo",
            lambda *a, **kw: init_calls.append(a)
            or restic.ResticResult(exit_code=1, stderr_tail="storage unreachable"),
        )
        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        monkeypatch.setattr(agent.restic, "ResticRunner", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no backup expected")))

        agent.run_agent()

        assert len(init_calls) == 1
        failure = fake.status_reports[-1]
        assert failure["status"] == "failed"
        assert failure["exit_code"] == 1
        assert "storage unreachable" in failure["message"]

    def test_repo_indeterminate_reports_failed(self, tmp_config_dir, monkeypatch):
        make_config(tmp_config_dir)
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "j8", "source": "/src", "destination": "/dest"}]

        init_calls = []
        monkeypatch.setattr(agent.restic, "check_repo", lambda *a, **kw: restic.RepoCheck(None, "connection refused"))
        monkeypatch.setattr(
            agent.restic,
            "init_repo",
            lambda *a, **kw: init_calls.append(a) or restic.ResticResult(exit_code=0),
        )
        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        monkeypatch.setattr(agent.restic, "ResticRunner", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no backup expected")))

        agent.run_agent()

        assert init_calls == []
        failure = fake.status_reports[-1]
        assert failure["status"] == "failed"
        assert "connection refused" in failure["message"]

    def test_repo_exists_skips_init(self, tmp_config_dir, monkeypatch):
        make_config(tmp_config_dir)
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "j9", "source": "/src", "destination": "/dest"}]

        init_calls = []
        monkeypatch.setattr(agent.restic, "check_repo", lambda *a, **kw: restic.RepoCheck(True))
        monkeypatch.setattr(
            agent.restic,
            "init_repo",
            lambda *a, **kw: init_calls.append(a) or restic.ResticResult(exit_code=0),
        )
        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        monkeypatch.setattr(agent.restic, "ResticRunner", lambda source, destination, env=None: FakeRunner(source, destination, env))

        agent.run_agent()

        assert init_calls == []
        assert fake.status_reports[-1]["status"] == "success"

    def test_missing_restic_reports_failed(self, tmp_config_dir, monkeypatch):
        make_config(tmp_config_dir)
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "j4", "source": "/src", "destination": "/dest"}]

        class MissingRunner:
            def __init__(self, source, destination, env=None):
                pass

            def start(self):
                raise FileNotFoundError("restic")

        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        patch_repo(monkeypatch)
        monkeypatch.setattr(agent.restic, "ResticRunner", MissingRunner)

        agent.run_agent()

        assert fake.status_reports[0]["status"] == "running"
        failure = fake.status_reports[-1]
        assert failure["status"] == "failed"
        assert failure["exit_code"] == 127
        assert "restic executable not found" in failure["message"]

    def test_missing_restic_in_repo_probe_reports_failed(self, tmp_config_dir, monkeypatch):
        make_config(tmp_config_dir)
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "j10", "source": "/src", "destination": "/dest"}]

        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        monkeypatch.setattr(
            agent.restic,
            "check_repo",
            lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("restic")),
        )
        monkeypatch.setattr(
            agent.restic,
            "ResticRunner",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no backup expected")),
        )

        agent.run_agent()

        assert fake.status_reports[0]["status"] == "running"
        failure = fake.status_reports[-1]
        assert failure["status"] == "failed"
        assert failure["exit_code"] == 127
        assert "restic binary not found in PATH" in failure["message"]

    def test_missing_restic_in_repo_init_reports_failed(self, tmp_config_dir, monkeypatch):
        make_config(tmp_config_dir)
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "j11", "source": "/src", "destination": "/dest"}]

        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        monkeypatch.setattr(agent.restic, "check_repo", lambda *a, **kw: restic.RepoCheck(False))
        monkeypatch.setattr(
            agent.restic,
            "init_repo",
            lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError("restic")),
        )
        monkeypatch.setattr(
            agent.restic,
            "ResticRunner",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no backup expected")),
        )

        agent.run_agent()

        assert fake.status_reports[0]["status"] == "running"
        failure = fake.status_reports[-1]
        assert failure["status"] == "failed"
        assert failure["exit_code"] == 127
        assert "restic binary not found in PATH" in failure["message"]


class TestJobLock:
    def test_sanitizes_job_id(self):
        lock = agent.JobLock("../../etc/passwd")
        assert ".." not in str(lock.path)
        assert "/" not in lock.path.name
        assert "\\" not in lock.path.name
        assert str(lock.path).startswith(str(Path(tempfile.gettempdir())))

    def test_safe_lock_name(self):
        assert agent._safe_lock_name("a/b") == "a_b"
        assert agent._safe_lock_name("job-1") == "job-1"
        assert agent._safe_lock_name("") == ""
