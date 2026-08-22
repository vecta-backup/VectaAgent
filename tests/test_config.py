import stat

import pytest

from vecta_agent import config


class TestConfigLoadSave:
    def test_save_and_load(self, tmp_config_dir):
        cfg = config.Config(agent_id="abc123", api_key="vc_" + "a" * 64, name="my-agent")
        config.save(cfg)

        loaded = config.load()
        assert loaded.agent_id == "abc123"
        assert loaded.api_key == "vc_" + "a" * 64
        assert loaded.name == "my-agent"

        path = config.config_path()
        assert path.exists()
        import sys
        if sys.platform != "win32":
            mode = stat.S_IMODE(path.stat().st_mode)
            assert mode == 0o600

    def test_load_missing(self, tmp_config_dir):
        with pytest.raises(config.ConfigError, match="Run 'vecta-agent register --token"):
            config.load()

    def test_load_missing_keys(self, tmp_config_dir):
        path = config.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('agent_id = "abc"\n')
        with pytest.raises(config.ConfigError, match="must contain 'agent_id' and 'api_key'"):
            config.load()

    def test_load_bad_types(self, tmp_config_dir):
        path = config.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('agent_id = 123\napi_key = "vc_abc"\n')
        with pytest.raises(config.ConfigError, match="must be strings"):
            config.load()

    def test_save_without_name(self, tmp_config_dir):
        cfg = config.Config(agent_id="abc", api_key="vc_abc")
        config.save(cfg)
        loaded = config.load()
        assert loaded.name is None
