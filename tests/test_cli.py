import subprocess

import pytest

from vecta_agent import __version__, api, cli, config, credentials, restic


class FakeSetupClient:
    """Fake ApiClient for `setup` tests: serves one job config."""

    def __init__(self, job=None):
        self.job = job if job is not None else {
            "job_id": "j1", "source": "/src", "destination": "/dest",
        }
        self.get_job_calls: list[str] = []

    def get_job(self, job_id):
        self.get_job_calls.append(job_id)
        return self.job


class TestCli:
    def test_version(self, capsys):
        cli.main(["version"])
        captured = capsys.readouterr()
        assert captured.out.strip() == __version__

    def test_register(self, tmp_config_dir, monkeypatch, capsys):
        calls = []

        def fake_register(self, token):
            calls.append(token)
            return {"agent_id": "a1", "api_key": "vc_k1", "name": "n1"}

        monkeypatch.setattr(cli.api.ApiClient, "register", fake_register)
        cli.main(["register", "--token", "tok123"])
        assert calls == ["tok123"]

        cfg = config.load()
        assert cfg.agent_id == "a1"
        assert cfg.api_key == "vc_k1"
        assert cfg.name == "n1"

    def test_run(self, tmp_config_dir, monkeypatch, capsys):
        make_config(tmp_config_dir)
        ran = []

        def fake_run_agent():
            ran.append(True)

        monkeypatch.setattr(cli.agent, "run_agent", fake_run_agent)
        cli.main(["run"])
        assert ran

    def test_register_error_prints_message(self, tmp_config_dir, monkeypatch, capsys):
        def fake_register(self, token):
            raise api.RegisterError("Registration token is invalid or expired. Generate a new token.")

        monkeypatch.setattr(cli.api.ApiClient, "register", fake_register)
        with pytest.raises(SystemExit) as exc:
            cli.main(["register", "--token", "bad"])
        assert exc.value.code == 1
        assert "Error: Registration token is invalid or expired" in capsys.readouterr().err

    def test_register_network_error_prints_message(self, tmp_config_dir, monkeypatch, capsys):
        def fake_register(self, token):
            raise api.ApiError("Could not reach http://test/api: connection refused")

        monkeypatch.setattr(cli.api.ApiClient, "register", fake_register)
        with pytest.raises(SystemExit) as exc:
            cli.main(["register", "--token", "tok"])
        assert exc.value.code == 1
        assert "Error: Could not reach" in capsys.readouterr().err

    def test_run_api_error_prints_message(self, tmp_config_dir, monkeypatch, capsys):
        make_config(tmp_config_dir)

        def fake_run_agent():
            raise api.ApiError("Could not reach https://vectaapp.com/api")

        monkeypatch.setattr(cli.agent, "run_agent", fake_run_agent)
        with pytest.raises(SystemExit) as exc:
            cli.main(["run"])
        assert exc.value.code == 1
        assert "Error: Could not reach" in capsys.readouterr().err

    def test_run_auth_error_prints_message(self, tmp_config_dir, monkeypatch, capsys):
        make_config(tmp_config_dir)

        def fake_run_agent():
            raise api.AgentAuthError("Agent is not registered or the credentials are wrong.")

        monkeypatch.setattr(cli.agent, "run_agent", fake_run_agent)
        with pytest.raises(SystemExit) as exc:
            cli.main(["run"])
        assert exc.value.code == 1
        assert "Error: Agent is not registered" in capsys.readouterr().err

    def test_run_config_error_prints_message(self, tmp_config_dir, monkeypatch, capsys):
        def fake_run_agent():
            raise config.ConfigError("Config not found at /nonexistent.")

        monkeypatch.setattr(cli.agent, "run_agent", fake_run_agent)
        with pytest.raises(SystemExit) as exc:
            cli.main(["run"])
        assert exc.value.code == 1
        assert "Error: Config not found" in capsys.readouterr().err

    def test_repo_init_with_password(self, tmp_config_dir, monkeypatch, capsys):
        destinations = []

        def fake_init(destination, env=None):
            destinations.append(destination)
            return restic.ResticResult(exit_code=0)

        monkeypatch.setattr(cli.restic, "init_repo", fake_init)
        cli.main(["repo", "init", "/backups/data", "--password", "s3cret"])

        assert destinations == ["/backups/data"]
        out = capsys.readouterr().out
        assert "Repository initialized at /backups/data" in out
        assert "cannot be recovered" in out

        env_path = tmp_config_dir / "restic.env"
        assert "RESTIC_PASSWORD=s3cret" in env_path.read_text(encoding="utf-8")

    def test_repo_init_generate(self, tmp_config_dir, monkeypatch, capsys):
        monkeypatch.setattr(cli.restic, "init_repo", lambda *a, **kw: restic.ResticResult(exit_code=0))
        cli.main(["repo", "init", "/backups/data", "--generate"])

        out = capsys.readouterr().out
        assert "Generated repository password:" in out
        assert "cannot be recovered" in out

        env_path = tmp_config_dir / "restic.env"
        content = env_path.read_text(encoding="utf-8")
        assert "RESTIC_PASSWORD=" in content
        assert content.split("RESTIC_PASSWORD=")[1].strip()

    def test_repo_init_already_initialized(self, tmp_config_dir, monkeypatch, capsys):
        monkeypatch.setattr(
            cli.restic,
            "init_repo",
            lambda *a, **kw: restic.ResticResult(
                exit_code=1, stderr_tail="repository master key and config already initialized"
            ),
        )
        cli.main(["repo", "init", "/backups/data", "--password", "pw"])
        assert "already initialized" in capsys.readouterr().out
        assert not (tmp_config_dir / "restic.env").exists()

    def test_repo_init_preserves_existing_password(self, tmp_config_dir, monkeypatch, capsys):
        (tmp_config_dir / "restic.env").write_text("RESTIC_PASSWORD=correct\n")
        monkeypatch.setattr(
            cli.restic,
            "init_repo",
            lambda *a, **kw: restic.ResticResult(
                exit_code=1, stderr_tail="repository master key and config already initialized"
            ),
        )
        cli.main(["repo", "init", "/backups/data", "--password", "wrong"])
        assert "already initialized" in capsys.readouterr().out
        assert "RESTIC_PASSWORD=correct" in (tmp_config_dir / "restic.env").read_text(
            encoding="utf-8"
        )

    def test_repo_init_does_not_write_on_failure(self, tmp_config_dir, monkeypatch, capsys):
        monkeypatch.setattr(
            cli.restic,
            "init_repo",
            lambda *a, **kw: restic.ResticResult(exit_code=1, stderr_tail="storage unreachable"),
        )
        with pytest.raises(SystemExit) as exc:
            cli.main(["repo", "init", "/backups/data", "--password", "pw"])
        assert exc.value.code == 1
        assert "storage unreachable" in capsys.readouterr().err
        assert not (tmp_config_dir / "restic.env").exists()

    def test_repo_init_before_register_creates_config_dir(self, tmp_config_dir, monkeypatch, capsys):
        nested = tmp_config_dir / "not" / "yet" / "created"
        monkeypatch.setenv("VECTA_CONFIG_DIR", str(nested))
        monkeypatch.setattr(cli.restic, "init_repo", lambda *a, **kw: restic.ResticResult(exit_code=0))
        cli.main(["repo", "init", "/backups/data", "--password", "pw"])

        env_path = nested / "restic.env"
        assert env_path.exists()
        assert "RESTIC_PASSWORD=pw" in env_path.read_text(encoding="utf-8")

    def test_repo_init_failure(self, tmp_config_dir, monkeypatch, capsys):
        monkeypatch.setattr(
            cli.restic,
            "init_repo",
            lambda *a, **kw: restic.ResticResult(exit_code=1, stderr_tail="storage unreachable"),
        )
        with pytest.raises(SystemExit) as exc:
            cli.main(["repo", "init", "/backups/data", "--password", "pw"])
        assert exc.value.code == 1
        assert "storage unreachable" in capsys.readouterr().err

    def test_repo_init_missing_password(self, tmp_config_dir, monkeypatch, capsys):
        monkeypatch.setattr(cli.restic, "init_repo", lambda *a, **kw: restic.ResticResult(exit_code=0))
        with pytest.raises(SystemExit) as exc:
            cli.main(["repo", "init", "/backups/data", "--password", ""])
        assert exc.value.code == 1
        assert "non-empty password" in capsys.readouterr().err

    def test_repo_init_missing_restic_binary(self, tmp_config_dir, monkeypatch, capsys):
        def fake_init(*a, **kw):
            raise FileNotFoundError("restic")

        monkeypatch.setattr(cli.restic, "init_repo", fake_init)
        with pytest.raises(SystemExit) as exc:
            cli.main(["repo", "init", "/backups/data", "--password", "pw"])
        assert exc.value.code == 1
        assert "restic executable not found" in capsys.readouterr().err

    def test_help(self, capsys):
        with pytest.raises(SystemExit) as exc:
            cli.main(["--help"])
        assert exc.value.code == 0


class TestSetup:
    def _install_client(self, monkeypatch, job):
        fake = FakeSetupClient(job)
        monkeypatch.setattr(cli.api, "ApiClient", lambda *a, **kw: fake)
        return fake

    @staticmethod
    def _capture_input(monkeypatch, answers):
        """Replace builtins.input, recording the prompts it is shown."""
        answers = iter(answers)
        prompts = []

        def fake_input(prompt=""):
            prompts.append(prompt)
            return next(answers)

        monkeypatch.setattr("builtins.input", fake_input)
        return prompts

    def test_fresh_s3_prompts_inits_and_gates(self, tmp_config_dir, monkeypatch, capsys):
        make_config(tmp_config_dir)
        dest = "s3:https://acct.r2.cloudflarestorage.com/bucket"
        self._install_client(monkeypatch, {"job_id": "j1", "source": "/src", "destination": dest})

        answers = iter(["access-key-id", "secret-key"])
        monkeypatch.setattr(cli.getpass, "getpass", lambda *a, **kw: next(answers))
        init_env = {}
        monkeypatch.setattr(
            cli.restic, "check_repo", lambda *a, **kw: restic.RepoCheck(False)
        )

        def fake_init(destination, env=None, port=None):
            init_env.update(env or {})
            return restic.ResticResult(exit_code=0)

        monkeypatch.setattr(cli.restic, "init_repo", fake_init)
        prompts = self._capture_input(monkeypatch, ["SAVED"])

        cli.main(["setup", "j1"])

        out = capsys.readouterr().out
        assert "Generated repository password:" in out
        assert "cannot be recovered" in out
        assert "already configured" not in out
        assert init_env["AWS_ACCESS_KEY_ID"] == "access-key-id"
        assert init_env["AWS_SECRET_ACCESS_KEY"] == "secret-key"

        saved = credentials.load_credentials(
            credentials.destination_fingerprint(dest)
        )
        assert saved["AWS_ACCESS_KEY_ID"] == "access-key-id"
        assert saved["AWS_SECRET_ACCESS_KEY"] == "secret-key"
        assert saved["RESTIC_PASSWORD"]
        assert "Type SAVED" in prompts[0]

    def test_gate_blocks_until_saved(self, tmp_config_dir, monkeypatch, capsys):
        make_config(tmp_config_dir)
        dest = "/backups/data"
        self._install_client(monkeypatch, {"job_id": "j1", "source": "/src", "destination": dest})

        prompts = self._capture_input(monkeypatch, ["no", "still no", "SAVED"])
        monkeypatch.setattr(cli.restic, "check_repo", lambda *a, **kw: restic.RepoCheck(False))
        monkeypatch.setattr(
            cli.restic, "init_repo", lambda *a, **kw: restic.ResticResult(exit_code=0)
        )

        cli.main(["setup", "j1"])

        out = capsys.readouterr().out
        assert len(prompts) == 3
        assert all("Type SAVED" in p for p in prompts)
        assert credentials.load_credentials(credentials.destination_fingerprint(dest))

    def test_stored_credentials_skip_prompts(self, tmp_config_dir, monkeypatch, capsys):
        make_config(tmp_config_dir)
        dest = "s3:https://acct.r2.cloudflarestorage.com/bucket"
        self._install_client(monkeypatch, {"job_id": "j1", "source": "/src", "destination": dest})
        credentials.save_credentials(
            credentials.destination_fingerprint(dest),
            dest,
            {"RESTIC_PASSWORD": "stored-pw", "AWS_ACCESS_KEY_ID": "AKIA"},
        )

        def no_prompt(*a, **kw):
            raise AssertionError("setup must not prompt when credentials are stored")

        monkeypatch.setattr(cli.getpass, "getpass", no_prompt)
        monkeypatch.setattr(cli.restic, "check_repo", lambda *a, **kw: restic.RepoCheck(True))
        monkeypatch.setattr(
            cli.restic,
            "init_repo",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no init expected")),
        )

        cli.main(["setup", "j1"])

        out = capsys.readouterr().out
        assert "already configured" in out
        assert "Repository at" in out and "already existed" in out

    def test_repo_exists_skips_init_and_saves_global_password(self, tmp_config_dir, monkeypatch, capsys):
        make_config(tmp_config_dir)
        (tmp_config_dir / "restic.env").write_text("RESTIC_PASSWORD=test\n")
        dest = "/backups/data"
        self._install_client(monkeypatch, {"job_id": "j1", "source": "/src", "destination": dest})

        probe_env = {}
        monkeypatch.setattr(cli.restic, "check_repo", lambda *a, **kw: restic.RepoCheck(True))
        monkeypatch.setattr(
            cli.restic,
            "init_repo",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no init expected")),
        )

        cli.main(["setup", "j1"])

        saved = credentials.load_credentials(credentials.destination_fingerprint(dest))
        assert saved["RESTIC_PASSWORD"] == "test"
        out = capsys.readouterr().out
        assert "already existed" in out
        assert "Type SAVED" not in out  # password was known, nothing generated

    def test_init_failure_saves_nothing(self, tmp_config_dir, monkeypatch, capsys):
        make_config(tmp_config_dir)
        dest = "s3:https://acct.r2.cloudflarestorage.com/bucket"
        self._install_client(monkeypatch, {"job_id": "j1", "source": "/src", "destination": dest})

        answers = iter(["AKIA", "shhh"])
        monkeypatch.setattr(cli.getpass, "getpass", lambda *a, **kw: next(answers))
        monkeypatch.setattr(cli.restic, "check_repo", lambda *a, **kw: restic.RepoCheck(False))
        monkeypatch.setattr(
            cli.restic,
            "init_repo",
            lambda *a, **kw: restic.ResticResult(exit_code=1, stderr_tail="AccessDenied"),
        )

        with pytest.raises(SystemExit) as exc:
            cli.main(["setup", "j1"])

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "AccessDenied" in err
        assert "vecta-agent setup j1" in err  # auth hint
        assert not (tmp_config_dir / "credentials.toml").exists()

    def test_existing_repo_with_unknown_password_prompts(self, tmp_config_dir, monkeypatch, capsys):
        make_config(tmp_config_dir)
        (tmp_config_dir / "restic.env").write_text("")  # no global password
        dest = "/backups/existing"
        self._install_client(monkeypatch, {"job_id": "j1", "source": "/src", "destination": dest})

        probes = iter([restic.RepoCheck(None, "wrong password"), restic.RepoCheck(True)])
        monkeypatch.setattr(
            cli.restic, "check_repo", lambda *a, **kw: next(probes)
        )
        pw_answers = iter(["real-pw", "real-pw"])
        monkeypatch.setattr(cli.getpass, "getpass", lambda *a, **kw: next(pw_answers))

        cli.main(["setup", "j1"])

        saved = credentials.load_credentials(credentials.destination_fingerprint(dest))
        assert saved["RESTIC_PASSWORD"] == "real-pw"
        out = capsys.readouterr().out
        assert "already existed" in out

    def test_sftp_connectivity_failure_prints_fix_commands(self, tmp_config_dir, monkeypatch, capsys):
        make_config(tmp_config_dir)
        dest = "sftp:ops@nas:/backups"
        self._install_client(
            monkeypatch,
            {"job_id": "j1", "source": "/src", "destination": dest, "port": 2222},
        )

        class FakeProc:
            returncode = 255
            stderr = "Permission denied (publickey)."
            stdout = ""

        monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: FakeProc())

        with pytest.raises(SystemExit) as exc:
            cli.main(["setup", "j1"])

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "Permission denied" in err
        assert "ssh-copy-id -p 2222 ops@nas" in err
        assert "sftp -P 2222 ops@nas" in err
        assert not (tmp_config_dir / "credentials.toml").exists()

    def test_sftp_connectivity_timeout_fails_cleanly(self, tmp_config_dir, monkeypatch, capsys):
        make_config(tmp_config_dir)
        dest = "sftp:ops@nas:/backups"
        self._install_client(
            monkeypatch,
            {"job_id": "j1", "source": "/src", "destination": dest, "port": 2222},
        )

        def hang(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 20))

        monkeypatch.setattr(cli.subprocess, "run", hang)

        with pytest.raises(SystemExit) as exc:
            cli.main(["setup", "j1"])

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "timed out" in err
        assert "ssh-copy-id -p 2222 ops@nas" in err
        assert not (tmp_config_dir / "credentials.toml").exists()

    def test_sftp_connectivity_ok_then_inits(self, tmp_config_dir, monkeypatch, capsys):
        make_config(tmp_config_dir)
        dest = "sftp:ops@nas:/backups"
        self._install_client(
            monkeypatch,
            {"job_id": "j1", "source": "/src", "destination": dest, "port": 2222},
        )

        class FakeProc:
            returncode = 0
            stderr = ""
            stdout = ""

        ssh_calls = []
        monkeypatch.setattr(
            cli.subprocess, "run", lambda cmd, **kw: ssh_calls.append((cmd, kw)) or FakeProc()
        )
        monkeypatch.setattr(cli.restic, "check_repo", lambda *a, **kw: restic.RepoCheck(False))
        monkeypatch.setattr(
            cli.restic, "init_repo", lambda *a, **kw: restic.ResticResult(exit_code=0)
        )
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "SAVED")

        cli.main(["setup", "j1"])

        out = capsys.readouterr().out
        assert "SSH key authentication" in out
        assert "Generated repository password:" in out
        cmd, kwargs = ssh_calls[0]
        assert cmd[0] == "ssh"
        assert "-p" in cmd and "2222" in cmd
        assert cmd[-3:] == ["-s", "sftp", "ops@nas"]
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["timeout"] == 20
        saved = credentials.load_credentials(credentials.destination_fingerprint(dest))
        assert saved["RESTIC_PASSWORD"]

    def test_sftp_never_prompts_for_credentials(self, tmp_config_dir, monkeypatch, capsys):
        make_config(tmp_config_dir)
        dest = "sftp:ops@nas:/backups"
        self._install_client(monkeypatch, {"job_id": "j1", "source": "/src", "destination": dest})

        def no_prompt(*a, **kw):
            raise AssertionError("SFTP setup must not prompt for credentials")

        monkeypatch.setattr(cli.getpass, "getpass", no_prompt)

        class FakeProc:
            returncode = 0
            stderr = ""
            stdout = ""

        monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: FakeProc())
        monkeypatch.setattr(cli.restic, "check_repo", lambda *a, **kw: restic.RepoCheck(True))

        cli.main(["setup", "j1"])
        assert "already existed" in capsys.readouterr().out

    def test_job_fetch_failure_prints_message(self, tmp_config_dir, monkeypatch, capsys):
        make_config(tmp_config_dir)

        class FailingClient:
            def get_job(self, job_id):
                raise api.ApiError("Unexpected error 404 from http://test: Job not found")

        monkeypatch.setattr(cli.api, "ApiClient", lambda *a, **kw: FailingClient())

        with pytest.raises(SystemExit) as exc:
            cli.main(["setup", "nope"])
        assert exc.value.code == 1
        assert "Job not found" in capsys.readouterr().err

    def test_malformed_credentials_file(self, tmp_config_dir, monkeypatch, capsys):
        make_config(tmp_config_dir)
        dest = "s3:https://acct.r2.cloudflarestorage.com/bucket"
        self._install_client(monkeypatch, {"job_id": "j1", "source": "/src", "destination": dest})
        path = credentials.credentials_path()
        path.write_text(
            f'[{credentials.destination_fingerprint(dest)}]\nCOUNT = 3\n', encoding="utf-8"
        )

        with pytest.raises(SystemExit) as exc:
            cli.main(["setup", "j1"])
        assert exc.value.code == 1
        assert "must be a string" in capsys.readouterr().err


def make_config(tmp_config_dir):
    cfg = config.Config(agent_id="a1", api_key="vc_" + "k" * 64)
    config.save(cfg)