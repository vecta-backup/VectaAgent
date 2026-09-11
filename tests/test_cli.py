import pytest

from vecta_agent import __version__, api, cli, config, restic, secrets


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


class TestSecretSet:
    def test_set_with_password(self, tmp_config_dir, capsys):
        cli.main(["secret", "set", "prod-s3", "--password", "s3cret"])

        out = capsys.readouterr().out
        assert "Profile 'prod-s3' saved" in out
        assert "cannot be recovered" in out
        assert secrets.load_profile("prod-s3") == {"RESTIC_PASSWORD": "s3cret"}

    def test_set_with_generate(self, tmp_config_dir, capsys):
        cli.main(["secret", "set", "p", "--generate"])
        profile = secrets.load_profile("p")
        assert profile["RESTIC_PASSWORD"]
        assert "Generated repository password:" in capsys.readouterr().out

    def test_set_with_env_only(self, tmp_config_dir, capsys):
        cli.main(["secret", "set", "r2", "--env", "AWS_ACCESS_KEY_ID=AKIA",
                  "--env", "AWS_SECRET_ACCESS_KEY=topsecret"])
        assert secrets.load_profile("r2") == {
            "AWS_ACCESS_KEY_ID": "AKIA",
            "AWS_SECRET_ACCESS_KEY": "topsecret",
        }
        out = capsys.readouterr().out
        assert "Keys: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY" in out

    def test_set_merges_keys_across_calls(self, tmp_config_dir):
        cli.main(["secret", "set", "r2", "--password", "pw"])
        cli.main(["secret", "set", "r2", "--env", "AWS_ACCESS_KEY_ID=AKIA"])
        assert secrets.load_profile("r2") == {
            "RESTIC_PASSWORD": "pw",
            "AWS_ACCESS_KEY_ID": "AKIA",
        }

    def test_set_password_and_env(self, tmp_config_dir):
        cli.main(["secret", "set", "r2", "--password", "pw",
                  "--env", "AWS_SECRET_ACCESS_KEY=k"])
        assert secrets.load_profile("r2") == {
            "RESTIC_PASSWORD": "pw",
            "AWS_SECRET_ACCESS_KEY": "k",
        }

    def test_set_invalid_env_pair(self, tmp_config_dir, capsys):
        with pytest.raises(SystemExit) as exc:
            cli.main(["secret", "set", "r2", "--env", "NOVALUE"])
        assert exc.value.code == 1
        assert "KEY=VALUE" in capsys.readouterr().err

    def test_set_invalid_name(self, tmp_config_dir):
        with pytest.raises(SystemExit) as exc:
            cli.main(["secret", "set", "Bad Name", "--password", "pw"])
        assert exc.value.code == 1

    def test_set_empty_password_rejected(self, tmp_config_dir):
        with pytest.raises(SystemExit) as exc:
            cli.main(["secret", "set", "r2", "--password", ""])
        assert exc.value.code == 1
        assert secrets.list_profiles() == {}

    def test_set_before_register_creates_config_dir(self, tmp_path, monkeypatch):
        nested = tmp_path / "not" / "yet" / "created"
        monkeypatch.setenv("VECTA_CONFIG_DIR", str(nested))
        cli.main(["secret", "set", "r2", "--password", "pw"])
        assert (nested / "secrets.toml").exists()


class TestSecretList:
    def test_list_profiles(self, tmp_config_dir, capsys):
        secrets.save_profile("r2", {"AWS_ACCESS_KEY_ID": "x", "RESTIC_PASSWORD": "y"})
        secrets.save_profile("prod", {"RESTIC_PASSWORD": "z"})
        cli.main(["secret", "list"])
        out = capsys.readouterr().out
        assert "r2: AWS_ACCESS_KEY_ID, RESTIC_PASSWORD" in out
        assert "prod: RESTIC_PASSWORD" in out
        assert "x" not in out.split("r2:")[1].splitlines()[0]

    def test_list_empty(self, tmp_config_dir, capsys):
        cli.main(["secret", "list"])
        assert "No credential profiles" in capsys.readouterr().out


class TestSecretRemove:
    def test_remove_existing(self, tmp_config_dir, capsys):
        secrets.save_profile("p", {"K": "v"})
        cli.main(["secret", "remove", "p"])
        assert "removed" in capsys.readouterr().out
        assert secrets.list_profiles() == {}

    def test_remove_missing(self, tmp_config_dir, capsys):
        with pytest.raises(SystemExit) as exc:
            cli.main(["secret", "remove", "nope"])
        assert exc.value.code == 1
        assert "not found" in capsys.readouterr().err


class TestRepoInitProfile:
    def test_init_profile_saves_to_secrets(self, tmp_config_dir, monkeypatch, capsys):
        monkeypatch.setattr(cli.restic, "init_repo", lambda *a, **kw: restic.ResticResult(exit_code=0))
        cli.main(["repo", "init", "sftp:user@host:/path", "--profile", "prod-sftp",
                  "--password", "pw"])

        env_path = tmp_config_dir / "restic.env"
        assert not env_path.exists()
        assert secrets.load_profile("prod-sftp") == {"RESTIC_PASSWORD": "pw"}
        assert "profile 'prod-sftp'" in capsys.readouterr().out

    def test_init_profile_merges_profile_env_for_init(self, tmp_config_dir, monkeypatch):
        secrets.save_profile("prod-sftp", {"AWS_ACCESS_KEY_ID": "AKIA"})
        captured = {}

        def fake_init(destination, env=None):
            captured.update(env or {})
            return restic.ResticResult(exit_code=0)

        monkeypatch.setattr(cli.restic, "init_repo", fake_init)
        cli.main(["repo", "init", "s3:bucket", "--profile", "prod-sftp", "--password", "pw"])
        assert captured["AWS_ACCESS_KEY_ID"] == "AKIA"
        assert captured["RESTIC_PASSWORD"] == "pw"
        assert secrets.load_profile("prod-sftp")["RESTIC_PASSWORD"] == "pw"

    def test_init_profile_already_initialized(self, tmp_config_dir, monkeypatch, capsys):
        monkeypatch.setattr(
            cli.restic,
            "init_repo",
            lambda *a, **kw: restic.ResticResult(
                exit_code=1, stderr_tail="repository master key and config already initialized"
            ),
        )
        cli.main(["repo", "init", "/dest", "--profile", "p", "--password", "pw"])
        out = capsys.readouterr().out
        assert "already initialized" in out
        assert "secret set p" in out
        assert secrets.list_profiles() == {}


def make_config(tmp_config_dir):
    cfg = config.Config(agent_id="a1", api_key="vc_" + "k" * 64)
    config.save(cfg)
