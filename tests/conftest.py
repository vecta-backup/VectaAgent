import logging
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def set_vectalog_level():
    logging.getLogger("vecta_agent").setLevel(logging.DEBUG)


@pytest.fixture
def tmp_config_dir(tmp_path: Path, monkeypatch):
    config_dir = tmp_path / "vecta-config"
    config_dir.mkdir()
    monkeypatch.setenv("VECTA_CONFIG_DIR", str(config_dir))
    return config_dir
