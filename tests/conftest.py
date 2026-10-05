import logging
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def avoid_host_root_requirements(monkeypatch):
    from vecta_agent import cli

    monkeypatch.setattr(cli, "_IS_POSIX", False)


@pytest.fixture(autouse=True)
def set_vectalog_level():
    logging.getLogger("vecta_agent").setLevel(logging.DEBUG)


@pytest.fixture
def tmp_config_dir(tmp_path: Path, monkeypatch):
    config_dir = tmp_path / "vecta-config"
    config_dir.mkdir()
    monkeypatch.setenv("VECTA_CONFIG_DIR", str(config_dir))
    return config_dir


@pytest.fixture
def mock_hook_account(monkeypatch):
    from types import SimpleNamespace

    from vecta_agent import hooks

    monkeypatch.setattr(
        hooks,
        "pwd",
        SimpleNamespace(
            getpwnam=lambda _name: SimpleNamespace(pw_uid=1000, pw_gid=1000)
        ),
    )
