import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from vecta_agent import agent, api, config, credentials, restic


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
    def __init__(self, source, destination, port=None, env=None):
        self.source = source
        self.destination = destination
        self.port = port
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

        def fake_runner(source, destination, port=None, env=None):
            r = FakeRunner(source, destination, port, env)
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

        def fake_runner(source, destination, port=None, env=None):
            r = FakeRunner(source, destination, port, env)
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

        def fake_runner(source, destination, port=None, env=None):
            r = FakeRunner(source, destination, port, env)
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

    def test_job_timeout_reports_distinct_failure(self, tmp_config_dir, monkeypatch):
        make_config(tmp_config_dir)
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "jt", "source": "/src", "destination": "/dest"}]
        runners = []

        def fake_runner(source, destination, port=None, env=None):
            r = FakeRunner(source, destination, port, env)
            r.status_count = 1000  # would never finish on its own
            runners.append(r)
            return r

        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        patch_repo(monkeypatch)
        monkeypatch.setattr(agent.restic, "ResticRunner", fake_runner)
        monkeypatch.setattr(agent, "PROGRESS_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(agent, "CANCEL_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(agent, "MAX_BACKUP_DURATION_SECONDS", 0.2)

        agent.run_agent()

        assert runners[0].terminated is True
        failure = fake.status_reports[-1]
        assert failure["status"] == "failed"
        assert failure["exit_code"] == 124
        assert "timed out" in failure["message"]

    def test_run_id_present_and_stable_across_reports(self, tmp_config_dir, monkeypatch):
        make_config(tmp_config_dir)
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "jr", "source": "/src", "destination": "/dest"}]

        def fake_runner(source, destination, port=None, env=None):
            r = FakeRunner(source, destination, port, env)
            r.summary = {"message_type": "summary", "snapshot_id": "s1"}
            return r

        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        patch_repo(monkeypatch)
        monkeypatch.setattr(agent.restic, "ResticRunner", fake_runner)
        monkeypatch.setattr(agent, "PROGRESS_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(agent, "CANCEL_INTERVAL_SECONDS", 0.05)

        agent.run_agent()

        assert len(fake.status_reports) >= 2
        run_ids = {r.get("run_id") for r in fake.status_reports}
        assert len(run_ids) == 1
        assert run_ids.pop()  # non-empty

    def test_zero_files_processed_reports_warning(self, tmp_config_dir, monkeypatch):
        make_config(tmp_config_dir)
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "jz", "source": "/src", "destination": "/dest"}]

        def fake_runner(source, destination, port=None, env=None):
            r = FakeRunner(source, destination, port, env)
            r.summary = {
                "message_type": "summary",
                "snapshot_id": "snap0",
                "total_files_processed": 0,
                "total_bytes_processed": 0,
                "data_added_packed": 0,
            }
            return r

        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        patch_repo(monkeypatch)
        monkeypatch.setattr(agent.restic, "ResticRunner", fake_runner)
        monkeypatch.setattr(agent, "PROGRESS_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(agent, "CANCEL_INTERVAL_SECONDS", 0.05)

        agent.run_agent()

        report = fake.status_reports[-1]
        assert report["status"] == "warning"
        assert report["exit_code"] == 0
        assert report["snapshot_id"] == "snap0"
        assert report["files_processed"] == 0
        assert "no files were processed" in report["message"]

    def test_zero_new_files_but_processed_reports_success(self, tmp_config_dir, monkeypatch):
        # Healthy dedup: restic scanned files but added nothing new. Must stay
        # a plain success — only zero total-processed may be flagged.
        make_config(tmp_config_dir)
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "jd", "source": "/src", "destination": "/dest"}]

        def fake_runner(source, destination, port=None, env=None):
            r = FakeRunner(source, destination, port, env)
            r.summary = {
                "message_type": "summary",
                "snapshot_id": "snapd",
                "files_new": 0,
                "total_files_processed": 25,
                "total_bytes_processed": 5000,
                "data_added_packed": 0,
            }
            return r

        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        patch_repo(monkeypatch)
        monkeypatch.setattr(agent.restic, "ResticRunner", fake_runner)
        monkeypatch.setattr(agent, "PROGRESS_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(agent, "CANCEL_INTERVAL_SECONDS", 0.05)

        agent.run_agent()

        report = fake.status_reports[-1]
        assert report["status"] == "success"
        assert report["files_processed"] == 25

    def test_zero_files_from_status_events_without_summary_reports_success(self, tmp_config_dir, monkeypatch):
        # Without a summary event we cannot distinguish "scanned nothing" from
        # "not finished reporting" — never flag on status-event counts alone.
        make_config(tmp_config_dir)
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "jn", "source": "/src", "destination": "/dest"}]

        class NoSummaryRunner(FakeRunner):
            def stream(self):
                yield {
                    "message_type": "status",
                    "percent_done": 0.0,
                    "files_done": 0,
                    "bytes_done": 0,
                }

        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        patch_repo(monkeypatch)
        monkeypatch.setattr(agent.restic, "ResticRunner", lambda *a, **kw: NoSummaryRunner("/src", "/dest"))
        monkeypatch.setattr(agent, "PROGRESS_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(agent, "CANCEL_INTERVAL_SECONDS", 0.05)

        agent.run_agent()

        report = fake.status_reports[-1]
        assert report["status"] == "success"

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

        def fake_runner(source, destination, port=None, env=None):
            captured_env.update(env or {})
            r = FakeRunner(source, destination, port, env)
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
        assert "vecta-agent setup j5" in failure["message"]

    def test_repo_missing_auto_inits_then_backs_up(self, tmp_config_dir, monkeypatch):
        make_config(tmp_config_dir)
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "j6", "source": "/src", "destination": "/dest"}]
        init_calls = patch_repo(monkeypatch, exists=False)

        def fake_runner(source, destination, port=None, env=None):
            r = FakeRunner(source, destination, port, env)
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
        monkeypatch.setattr(agent.restic, "ResticRunner", lambda source, destination, port=None, env=None: FakeRunner(source, destination, port, env))

        agent.run_agent()

        assert init_calls == []
        assert fake.status_reports[-1]["status"] == "success"

    def test_missing_restic_reports_failed(self, tmp_config_dir, monkeypatch):
        make_config(tmp_config_dir)
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "j4", "source": "/src", "destination": "/dest"}]

        class MissingRunner:
            def __init__(self, source, destination, port=None, env=None):
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


class TestDestinationCredentials:
    def _save_for(self, destination, entries):
        credentials.save_credentials(
            credentials.destination_fingerprint(destination), destination, entries
        )

    def test_stored_creds_merged_and_override_global(self, tmp_config_dir, monkeypatch):
        make_config(tmp_config_dir)  # restic.env: RESTIC_PASSWORD=test
        dest = "s3:https://acct.r2.cloudflarestorage.com/bucket"
        self._save_for(dest, {"RESTIC_PASSWORD": "dest-secret", "AWS_ACCESS_KEY_ID": "AKIA"})

        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "j1", "source": "/src", "destination": dest}]

        captured_env = {}

        def fake_runner(source, destination, port=None, env=None):
            captured_env.update(env or {})
            r = FakeRunner(source, destination, port, env)
            r.summary = {"message_type": "summary", "snapshot_id": "s1"}
            return r

        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        patch_repo(monkeypatch)
        monkeypatch.setattr(agent.restic, "ResticRunner", fake_runner)
        monkeypatch.setattr(agent, "PROGRESS_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(agent, "CANCEL_INTERVAL_SECONDS", 0.05)

        agent.run_agent()

        assert captured_env["RESTIC_PASSWORD"] == "dest-secret"
        assert captured_env["AWS_ACCESS_KEY_ID"] == "AKIA"
        assert fake.status_reports[-1]["status"] == "success"

    def test_other_destinations_creds_not_applied(self, tmp_config_dir, monkeypatch):
        make_config(tmp_config_dir)
        self._save_for(
            "s3:https://other.example.com/bucket", {"RESTIC_PASSWORD": "other-secret"}
        )

        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "j1", "source": "/src", "destination": "/dest"}]

        captured_env = {}

        def fake_runner(source, destination, port=None, env=None):
            captured_env.update(env or {})
            r = FakeRunner(source, destination, port, env)
            r.summary = {"message_type": "summary", "snapshot_id": "s1"}
            return r

        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        patch_repo(monkeypatch)
        monkeypatch.setattr(agent.restic, "ResticRunner", fake_runner)
        monkeypatch.setattr(agent, "PROGRESS_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(agent, "CANCEL_INTERVAL_SECONDS", 0.05)

        agent.run_agent()

        assert captured_env["RESTIC_PASSWORD"] == "test"  # global, untouched

    def test_global_restic_env_still_works_without_stored_creds(self, tmp_config_dir, monkeypatch):
        make_config(tmp_config_dir)
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "j1", "source": "/src", "destination": "/dest"}]

        captured_env = {}

        def fake_runner(source, destination, port=None, env=None):
            captured_env.update(env or {})
            r = FakeRunner(source, destination, port, env)
            r.summary = {"message_type": "summary", "snapshot_id": "s1"}
            return r

        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        patch_repo(monkeypatch)
        monkeypatch.setattr(agent.restic, "ResticRunner", fake_runner)
        monkeypatch.setattr(agent, "PROGRESS_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(agent, "CANCEL_INTERVAL_SECONDS", 0.05)

        agent.run_agent()

        assert captured_env["RESTIC_PASSWORD"] == "test"
        assert fake.status_reports[-1]["status"] == "success"

    def test_malformed_credentials_file_reports_failed(self, tmp_config_dir, monkeypatch):
        make_config(tmp_config_dir)
        dest = "s3:https://acct.r2.cloudflarestorage.com/bucket"
        path = credentials.credentials_path()
        path.write_text(f"[{credentials.destination_fingerprint(dest)}]\nCOUNT = 3\n", encoding="utf-8")

        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "j1", "source": "/src", "destination": dest}]
        runner_called = []

        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        monkeypatch.setattr(
            agent.restic, "ResticRunner", lambda *a, **kw: runner_called.append(a)
        )

        agent.run_agent()

        assert runner_called == []
        failure = fake.status_reports[-1]
        assert failure["status"] == "failed"
        assert "Invalid local credentials file" in failure["message"]

    def test_no_stored_creds_does_not_fail_before_restic(self, tmp_config_dir, monkeypatch):
        # Destinations needing nothing stored (global env, instance roles, SSH
        # keys) must run: no pre-check may gate on stored credentials.
        make_config(tmp_config_dir)
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "j1", "source": "/src", "destination": "/dest"}]
        runner_called = []

        def fake_runner(source, destination, port=None, env=None):
            runner_called.append((source, destination))
            r = FakeRunner(source, destination, port, env)
            r.summary = {"message_type": "summary", "snapshot_id": "s1"}
            return r

        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        patch_repo(monkeypatch)
        monkeypatch.setattr(agent.restic, "ResticRunner", fake_runner)
        monkeypatch.setattr(agent, "PROGRESS_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(agent, "CANCEL_INTERVAL_SECONDS", 0.05)

        agent.run_agent()

        assert runner_called
        assert fake.status_reports[-1]["status"] == "success"

    def test_auth_failure_appends_setup_hint(self, tmp_config_dir, monkeypatch):
        make_config(tmp_config_dir)
        dest = "s3:https://acct.r2.cloudflarestorage.com/bucket"
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "j1", "source": "/src", "destination": dest}]

        def fake_runner(source, destination, port=None, env=None):
            r = FakeRunner(source, destination, port, env)
            r.exit_code = 1
            r.stderr = "AccessDenied: The AWS Access Key Id you provided does not exist"
            return r

        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        patch_repo(monkeypatch)
        monkeypatch.setattr(agent.restic, "ResticRunner", fake_runner)
        monkeypatch.setattr(agent, "PROGRESS_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(agent, "CANCEL_INTERVAL_SECONDS", 0.05)

        agent.run_agent()

        failure = fake.status_reports[-1]
        assert failure["status"] == "failed"
        assert "AccessDenied" in failure["message"]
        assert "vecta-agent setup j1" in failure["message"]

    def test_non_auth_failure_has_no_hint(self, tmp_config_dir, monkeypatch):
        make_config(tmp_config_dir)
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "j1", "source": "/src", "destination": "/dest"}]

        def fake_runner(source, destination, port=None, env=None):
            r = FakeRunner(source, destination, port, env)
            r.exit_code = 1
            r.stderr = "no space left on device"
            return r

        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        patch_repo(monkeypatch)
        monkeypatch.setattr(agent.restic, "ResticRunner", fake_runner)
        monkeypatch.setattr(agent, "PROGRESS_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(agent, "CANCEL_INTERVAL_SECONDS", 0.05)

        agent.run_agent()

        failure = fake.status_reports[-1]
        assert "no space left on device" in failure["message"]
        assert "vecta-agent setup" not in failure["message"]

    def test_stored_creds_used_for_repo_probe_and_init(self, tmp_config_dir, monkeypatch):
        make_config(tmp_config_dir)
        dest = "s3:https://acct.r2.cloudflarestorage.com/bucket"
        self._save_for(dest, {"RESTIC_PASSWORD": "dest-secret"})
        fake = FakeApiClient("a1", "vc_" + "k" * 64)
        fake.jobs = [{"job_id": "j1", "source": "/src", "destination": dest}]

        probe_env = {}
        init_env = {}

        def fake_check(destination, env=None, port=None):
            probe_env.update(env or {})
            return restic.RepoCheck(False)

        def fake_init(destination, env=None, port=None):
            init_env.update(env or {})
            return restic.ResticResult(exit_code=0)

        monkeypatch.setattr(agent.restic, "check_repo", fake_check)
        monkeypatch.setattr(agent.restic, "init_repo", fake_init)
        monkeypatch.setattr(agent.api, "ApiClient", lambda *a, **kw: fake)
        monkeypatch.setattr(
            agent.restic, "ResticRunner",
            lambda source, destination, port=None, env=None: FakeRunner(source, destination, port, env),
        )
        monkeypatch.setattr(agent, "PROGRESS_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(agent, "CANCEL_INTERVAL_SECONDS", 0.05)

        agent.run_agent()

        assert probe_env["RESTIC_PASSWORD"] == "dest-secret"
        assert init_env["RESTIC_PASSWORD"] == "dest-secret"


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
