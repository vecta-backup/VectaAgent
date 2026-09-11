import pytest

from vecta_agent import __version__, api, cli, config, restic


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


def make_config(tmp_config_dir):
    cfg = config.Config(agent_id="a1", api_key="vc_" + "k" * 64)
    config.save(cfg)
