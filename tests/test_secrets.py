import os
import stat

import pytest

from vecta_agent import secrets


class TestProfileNameValidation:
    def test_valid_names(self, tmp_config_dir):
        for name in ("prod-s3", "a", "x_1-y", "0ab"):
            secrets.save_profile(name, {"K": "v"})
        names = sorted(secrets.list_profiles())
        assert names == ["0ab", "a", "prod-s3", "x_1-y"]

    def test_invalid_names_rejected(self, tmp_config_dir):
        for name in ("", "Upper", "with space", "a" * 65, "x!", ".hidden"):
            with pytest.raises(secrets.SecretsError):
                secrets.save_profile(name, {"K": "v"})
        assert secrets.list_profiles() == {}


class TestSaveLoadProfile:
    def test_save_and_load(self, tmp_config_dir):
        secrets.save_profile("prod-s3", {
            "RESTIC_PASSWORD": "p1",
            "AWS_ACCESS_KEY_ID": "AKIA",
        })
        profile = secrets.load_profile("prod-s3")
        assert profile == {"RESTIC_PASSWORD": "p1", "AWS_ACCESS_KEY_ID": "AKIA"}

    def test_save_merges_existing_keys(self, tmp_config_dir):
        secrets.save_profile("prod-s3", {"RESTIC_PASSWORD": "p1"})
        secrets.save_profile("prod-s3", {"AWS_ACCESS_KEY_ID": "AKIA"})
        profile = secrets.load_profile("prod-s3")
        assert profile["RESTIC_PASSWORD"] == "p1"
        assert profile["AWS_ACCESS_KEY_ID"] == "AKIA"

    def test_save_overwrites_key(self, tmp_config_dir):
        secrets.save_profile("p", {"RESTIC_PASSWORD": "old"})
        secrets.save_profile("p", {"RESTIC_PASSWORD": "new"})
        assert secrets.load_profile("p")["RESTIC_PASSWORD"] == "new"

    def test_other_profiles_preserved(self, tmp_config_dir):
        secrets.save_profile("a", {"K": "va"})
        secrets.save_profile("b", {"K": "vb"})
        secrets.save_profile("a", {"L": "la"})
        assert secrets.load_profile("a") == {"K": "va", "L": "la"}
        assert secrets.load_profile("b") == {"K": "vb"}

    def test_missing_profile_returns_none(self, tmp_config_dir):
        assert secrets.load_profile("nope") is None

    @pytest.mark.skipif(os.name == "nt", reason="chmod is a no-op on Windows")
    def test_chmod_600(self, tmp_config_dir):
        secrets.save_profile("p", {"K": "v"})
        mode = stat.S_IMODE(secrets.secrets_path().stat().st_mode)
        assert mode == 0o600

    def test_load_creates_config_dir(self, tmp_path, monkeypatch):
        nested = tmp_path / "not" / "yet" / "created"
        monkeypatch.setenv("VECTA_CONFIG_DIR", str(nested))
        secrets.save_profile("p", {"K": "v"})
        assert (nested / "secrets.toml").exists()

    def test_non_string_value_rejected(self, tmp_config_dir):
        path = secrets.secrets_path()
        path.write_text('[p]\nCOUNT = 3\n', encoding="utf-8")
        with pytest.raises(secrets.SecretsError, match="must be a string"):
            secrets.load_profile("p")

    def test_malformed_toml(self, tmp_config_dir):
        path = secrets.secrets_path()
        path.write_text("[p\nbroken", encoding="utf-8")
        with pytest.raises(secrets.SecretsError, match="Invalid TOML"):
            secrets.load_profile("p")

    def test_non_table_section(self, tmp_config_dir):
        path = secrets.secrets_path()
        path.write_text('p = "not-a-table"\n', encoding="utf-8")
        with pytest.raises(secrets.SecretsError, match="must be a table"):
            secrets.load_profile("p")


class TestRemoveProfile:
    def test_remove_existing(self, tmp_config_dir):
        secrets.save_profile("p", {"K": "v"})
        assert secrets.remove_profile("p") is True
        assert secrets.load_profile("p") is None

    def test_remove_missing(self, tmp_config_dir):
        assert secrets.remove_profile("nope") is False

    def test_remove_keeps_others(self, tmp_config_dir):
        secrets.save_profile("a", {"K": "va"})
        secrets.save_profile("b", {"K": "vb"})
        secrets.remove_profile("a")
        assert secrets.list_profiles() == {"b": ["K"]}


class TestListProfiles:
    def test_lists_keys_only(self, tmp_config_dir):
        secrets.save_profile("p", {"RESTIC_PASSWORD": "secret", "AWS_ACCESS_KEY_ID": "x"})
        assert secrets.list_profiles() == {
            "p": ["AWS_ACCESS_KEY_ID", "RESTIC_PASSWORD"]
        }

    def test_empty_file(self, tmp_config_dir):
        assert secrets.list_profiles() == {}