import threading

from vecta_agent import agent, config, hooks, postgresql, restic


DB_SOURCE = {
    "engine": "postgresql", "host": "db.internal", "port": 5432,
    "database": "orders", "username": "backup", "ssl_mode": "require", "dump_format": "custom",
}


class FakeClient:
    def __init__(self):
        self.status_reports = []

    def report_status(self, job_id, payload):
        self.status_reports.append({"job_id": job_id, **payload})

    def cancel_status(self, _job_id):
        return {"cancel_requested": False}


class FakeResticRunner:
    instances = []

    def __init__(self, source, destination, port=None, env=None, stdin_filename=None, source_command=None, unset_env=()):
        self.source = source
        self.destination = destination
        self.port = port
        self.env = env or {}
        self.stdin_filename = stdin_filename
        self.source_command = source_command
        self.unset_env = unset_env
        self.exit_code = 0
        self.stderr = ""
        self.events = [{
            "message_type": "summary", "snapshot_id": "snap-db",
            "total_bytes_processed": 4096, "data_added_packed": 1000,
        }]
        self.terminated = False
        self.instances.append(self)

    def start(self):
        pass

    def stream(self):
        yield from self.events

    def wait(self):
        return self.exit_code

    def terminate(self, grace_seconds=10):
        self.terminated = True

    def stderr_tail(self, max_lines=20):
        return self.stderr


def _setup_runtime(tmp_config_dir, monkeypatch):
    (tmp_config_dir / "restic.env").write_text("RESTIC_PASSWORD=repo-secret\n", encoding="utf-8")
    monkeypatch.setattr(agent.restic, "check_repo", lambda *a, **kw: restic.RepoCheck(True))
    monkeypatch.setattr(agent.restic, "ResticRunner", FakeResticRunner)
    monkeypatch.setattr(agent.hooks, "load_catalog", lambda: {})
    FakeResticRunner.instances.clear()


def test_database_job_uses_stdin_filename_and_reports_restore_metadata(tmp_config_dir, monkeypatch, caplog):
    _setup_runtime(tmp_config_dir, monkeypatch)
    monkeypatch.setattr(postgresql, "probe_postgresql_support", lambda: True)
    monkeypatch.setattr(postgresql, "child_environment", lambda _source: {"PGPASSFILE": "/secure/pgpass"})
    client = FakeClient()
    job = {
        "job_id": "db1", "backup_type": "database", "source": None,
        "source_config": DB_SOURCE, "destination": "/repo",
    }

    agent._run_single_job(client, config.Config("a", "k"), job, agent.load_restic_env(tmp_config_dir))

    runner = FakeResticRunner.instances[0]
    assert runner.source is None
    assert runner.stdin_filename == "postgresql-orders.dump"
    assert runner.source_command == postgresql.build_pg_dump_argv(DB_SOURCE)
    assert runner.env["PGPASSFILE"] == "/secure/pgpass"
    assert "PGPASSWORD" in runner.unset_env
    terminal = client.status_reports[-1]
    assert terminal["status"] == "success"
    assert terminal["backup_type"] == "database"
    assert terminal["stdin_filename"] == "postgresql-orders.dump"
    assert terminal["snapshot_id"] == "snap-db"
    assert [r["stage"] for r in terminal["stage_results"]] == ["repository", "source_capture", "restic"]
    assert {result["stage"] for result in terminal["stage_results"]} <= agent.STAGE_RESULT_STAGES
    assert {result["status"] for result in terminal["stage_results"]} <= agent.STAGE_RESULT_STATUSES
    assert "repo-secret" not in repr(client.status_reports)
    assert "Starting job db1: PostgreSQL db.internal:5432/orders -> /repo" in caplog.text
    assert "Job db1 succeeded (snapshot snap-db)." in caplog.text


def test_process_diagnostics_redact_local_restic_secrets(tmp_config_dir, monkeypatch):
    _setup_runtime(tmp_config_dir, monkeypatch)

    def failing_runner(*args, **kwargs):
        runner = FakeResticRunner(*args, **kwargs)
        runner.exit_code = 1
        runner.stderr = "failure included repo-secret"
        return runner

    monkeypatch.setattr(agent.restic, "ResticRunner", failing_runner)
    client = FakeClient()
    job = {"job_id": "redact", "source": "/src", "destination": "/repo"}
    agent._run_single_job(client, config.Config("a", "k"), job, agent.load_restic_env(tmp_config_dir))

    assert "repo-secret" not in repr(client.status_reports)
    assert "[REDACTED]" in client.status_reports[-1]["message"]


def test_pg_dump_failure_does_not_report_snapshot_and_preserves_producer_code(tmp_config_dir, monkeypatch, caplog):
    _setup_runtime(tmp_config_dir, monkeypatch)
    monkeypatch.setattr(postgresql, "probe_postgresql_support", lambda: True)
    monkeypatch.setattr(postgresql, "child_environment", lambda _source: {"PGPASSFILE": "/secure/pgpass"})

    def failing_runner(*args, **kwargs):
        runner = FakeResticRunner(*args, **kwargs)
        runner.exit_code = 1
        runner.stderr = "command [pg_dump] failed: exit status 7"
        runner.events.append({"message_type": "summary", "snapshot_id": "must-not-report"})
        return runner

    monkeypatch.setattr(agent.restic, "ResticRunner", failing_runner)
    client = FakeClient()
    job = {"job_id": "db-fail", "backup_type": "database", "source_config": DB_SOURCE, "destination": "/repo"}
    agent._run_single_job(client, config.Config("a", "k"), job, agent.load_restic_env(tmp_config_dir))

    terminal = client.status_reports[-1]
    assert terminal["status"] == "failed"
    assert "snapshot_id" not in terminal
    producer = next(item for item in terminal["stage_results"] if item["stage"] == "source_capture")
    assert producer["exit_code"] == 7
    assert "Job db-fail failed during source_capture with exit code 1" in caplog.text
    assert "pg_dump failed; Restic did not create a snapshot." in caplog.text


def test_cancellation_during_database_capture_reaps_runner_and_runs_cleanup(tmp_config_dir, monkeypatch, caplog):
    _setup_runtime(tmp_config_dir, monkeypatch)
    monkeypatch.setattr(postgresql, "probe_postgresql_support", lambda: True)
    monkeypatch.setattr(postgresql, "child_environment", lambda _source: {"PGPASSFILE": "/secure/pgpass"})
    started = threading.Event()

    class CancellableRunner(FakeResticRunner):
        def start(self):
            started.set()

        def stream(self):
            while not self.terminated:
                threading.Event().wait(0.01)
            return
            yield  # pragma: no cover

        def terminate(self, grace_seconds=10):
            self.terminated = True
            self.exit_code = 130

    monkeypatch.setattr(agent.restic, "ResticRunner", CancellableRunner)
    post = hooks.HookDefinition(
        "post", "post", "desc", ("post",), "/local/post", (),
        {"type": "object", "properties": {}}, "vecta-hook", False, 10, 100,
    )
    monkeypatch.setattr(agent.hooks, "load_catalog", lambda: {"post": post})
    cleanup = []
    monkeypatch.setattr(agent.hooks, "execute_hook", lambda definition, params, **kwargs: cleanup.append(kwargs.get("cancel_event")) or hooks.HookResult(0, 1))

    def cancel_when_dump_starts(client, job_id, cancel_event, done_event, terminator):
        assert started.wait(2)
        cancel_event.set()
        terminator.terminate()

    monkeypatch.setattr(agent, "_cancel_poller", cancel_when_dump_starts)
    client = FakeClient()
    job = {
        "job_id": "db-cancel", "backup_type": "database", "source_config": DB_SOURCE,
        "destination": "/repo", "post_hook": {"hook_id": "post", "parameters": {}},
    }
    agent._run_single_job(client, config.Config("a", "k"), job, agent.load_restic_env(tmp_config_dir))

    runner = CancellableRunner.instances[-1]
    assert runner.terminated
    assert cleanup == [None]
    terminal = client.status_reports[-1]
    assert terminal["status"] == "failed"
    assert terminal["exit_code"] == 130
    assert terminal["stage"] == "source_capture"
    assert "snapshot_id" not in terminal
    assert "Job db-cancel cancelled." in caplog.text


def test_pre_hook_failure_skips_backup_but_attempts_post_cleanup(tmp_config_dir, monkeypatch):
    _setup_runtime(tmp_config_dir, monkeypatch)
    pre = hooks.HookDefinition(
        "pre", "pre", "desc", ("pre",), "/local/pre", (),
        {"type": "object", "properties": {}}, "vecta-hook", False, 10, 100,
    )
    post = hooks.HookDefinition(
        "post", "post", "desc", ("post",), "/local/post", (),
        {"type": "object", "properties": {}}, "vecta-hook", False, 10, 100,
    )
    monkeypatch.setattr(agent.hooks, "load_catalog", lambda: {"pre": pre, "post": post})
    executed = []

    def run_hook(definition, parameters, **kwargs):
        executed.append(definition.id)
        return hooks.HookResult(exit_code=2 if definition.id == "pre" else 0, duration_seconds=1)

    monkeypatch.setattr(agent.hooks, "execute_hook", run_hook)
    client = FakeClient()
    job = {
        "job_id": "hook-fail", "backup_type": "files", "source": "/src", "destination": "/repo",
        "pre_hook": {"hook_id": "pre", "parameters": {}},
        "post_hook": {"hook_id": "post", "parameters": {}},
    }
    agent._run_single_job(client, config.Config("a", "k"), job, agent.load_restic_env(tmp_config_dir))

    assert executed == ["pre", "post"]
    assert not FakeResticRunner.instances
    terminal = client.status_reports[-1]
    assert terminal["status"] == "failed"
    assert [item["stage"] for item in terminal["stage_results"]] == ["repository", "pre_hook", "post_hook"]


def test_post_hook_failure_keeps_completed_snapshot_id(tmp_config_dir, monkeypatch):
    _setup_runtime(tmp_config_dir, monkeypatch)
    post = hooks.HookDefinition(
        "post", "post", "desc", ("post",), "/local/post", (),
        {"type": "object", "properties": {}}, "vecta-hook", False, 10, 100,
    )
    monkeypatch.setattr(agent.hooks, "load_catalog", lambda: {"post": post})
    monkeypatch.setattr(agent.hooks, "execute_hook", lambda *a, **kw: hooks.HookResult(exit_code=9, duration_seconds=1))
    client = FakeClient()
    job = {
        "job_id": "post-fail", "backup_type": "files", "source": "/src", "destination": "/repo",
        "post_hook": {"hook_id": "post", "parameters": {}},
    }
    agent._run_single_job(client, config.Config("a", "k"), job, agent.load_restic_env(tmp_config_dir))

    terminal = client.status_reports[-1]
    assert terminal["status"] == "failed"
    assert terminal["stage"] == "post_hook"
    assert terminal["snapshot_id"] == "snap-db"


def test_pre_hook_timeout_skips_capture_and_reports_timeout(tmp_config_dir, monkeypatch):
    _setup_runtime(tmp_config_dir, monkeypatch)
    pre = hooks.HookDefinition(
        "pre", "pre", "desc", ("pre",), "/local/pre", (),
        {"type": "object", "properties": {}}, "vecta-hook", False, 10, 100,
    )
    monkeypatch.setattr(agent.hooks, "load_catalog", lambda: {"pre": pre})
    monkeypatch.setattr(agent.hooks, "execute_hook", lambda *a, **kw: hooks.HookResult(exit_code=-15, duration_seconds=10, timed_out=True))
    client = FakeClient()
    job = {
        "job_id": "pre-timeout", "backup_type": "files", "source": "/src", "destination": "/repo",
        "pre_hook": {"hook_id": "pre", "parameters": {}},
    }
    agent._run_single_job(client, config.Config("a", "k"), job, agent.load_restic_env(tmp_config_dir))

    assert not FakeResticRunner.instances
    terminal = client.status_reports[-1]
    assert terminal["status"] == "failed"
    assert terminal["exit_code"] == 124
    assert terminal["stage"] == "pre_hook"


def test_cancelled_pre_hook_gets_uncancellable_bounded_cleanup(tmp_config_dir, monkeypatch):
    _setup_runtime(tmp_config_dir, monkeypatch)
    pre = hooks.HookDefinition(
        "pre", "pre", "desc", ("pre",), "/local/pre", (),
        {"type": "object", "properties": {}}, "vecta-hook", False, 10, 100,
    )
    post = hooks.HookDefinition(
        "post", "post", "desc", ("post",), "/local/post", (),
        {"type": "object", "properties": {}}, "vecta-hook", False, 10, 100,
    )
    monkeypatch.setattr(agent.hooks, "load_catalog", lambda: {"pre": pre, "post": post})
    cleanup_calls = []

    def run_hook(definition, parameters, **kwargs):
        if definition.id == "pre":
            kwargs["cancel_event"].set()
            return hooks.HookResult(exit_code=130, duration_seconds=1, cancelled=True)
        cleanup_calls.append(kwargs.get("cancel_event"))
        return hooks.HookResult(exit_code=0, duration_seconds=1)

    monkeypatch.setattr(agent.hooks, "execute_hook", run_hook)
    client = FakeClient()
    job = {
        "job_id": "cancel-cleanup", "backup_type": "files", "source": "/src", "destination": "/repo",
        "pre_hook": {"hook_id": "pre", "parameters": {}},
        "post_hook": {"hook_id": "post", "parameters": {}},
    }
    agent._run_single_job(client, config.Config("a", "k"), job, agent.load_restic_env(tmp_config_dir))

    assert cleanup_calls == [None]
    assert client.status_reports[-1]["message"] == "Cancelled by user"


def test_cancellation_during_normal_post_hook_fails_run_but_keeps_snapshot(tmp_config_dir, monkeypatch):
    _setup_runtime(tmp_config_dir, monkeypatch)
    post = hooks.HookDefinition(
        "post", "post", "desc", ("post",), "/local/post", (),
        {"type": "object", "properties": {}}, "vecta-hook", False, 10, 100,
    )
    monkeypatch.setattr(agent.hooks, "load_catalog", lambda: {"post": post})

    def cancel_hook(_definition, _parameters, **kwargs):
        kwargs["cancel_event"].set()
        return hooks.HookResult(exit_code=130, duration_seconds=1, cancelled=True)

    monkeypatch.setattr(agent.hooks, "execute_hook", cancel_hook)
    client = FakeClient()
    job = {
        "job_id": "post-cancel", "backup_type": "files", "source": "/src", "destination": "/repo",
        "post_hook": {"hook_id": "post", "parameters": {}},
    }
    agent._run_single_job(client, config.Config("a", "k"), job, agent.load_restic_env(tmp_config_dir))

    terminal = client.status_reports[-1]
    assert terminal["status"] == "failed"
    assert terminal["exit_code"] == 130
    assert terminal["stage"] == "post_hook"
    assert terminal["snapshot_id"] == "snap-db"
