import os
import stat

import pytest

from vecta_agent import credentials


class TestFingerprint:
    def test_stable_across_trimming(self, tmp_config_dir):
        trimmed = credentials.destination_fingerprint("s3:https://e.com/bucket")
        assert trimmed == credentials.destination_fingerprint("  s3:https://e.com/bucket  ")

    def test_distinct_destinations_differ(self):
        a = credentials.destination_fingerprint("s3:https://e.com/a")
        b = credentials.destination_fingerprint("s3:https://e.com/b")
        assert a != b

    def test_fingerprint_is_16_hex_chars(self):
        fp = credentials.destination_fingerprint("/backups")
        assert len(fp) == 16
        int(fp, 16)  # raises ValueError when not hex


class TestSaveLoad:
    def test_roundtrip(self, tmp_config_dir):
        credentials.save_credentials("fp1", "s3:e/b", {"RESTIC_PASSWORD": "p1"})
        assert credentials.load_credentials("fp1") == {"RESTIC_PASSWORD": "p1"}

    def test_destination_annotation_stored_but_not_returned(self, tmp_config_dir):
        credentials.save_credentials("fp1", "s3:e/b", {"RESTIC_PASSWORD": "p1"})
        assert credentials.load_credentials("fp1") == {"RESTIC_PASSWORD": "p1"}
        assert "s3:e/b" in credentials.credentials_path().read_text(encoding="utf-8")

    def test_save_merges_keys_across_calls(self, tmp_config_dir):
        credentials.save_credentials("fp1", "d", {"RESTIC_PASSWORD": "p1"})
        credentials.save_credentials("fp1", "d", {"AWS_ACCESS_KEY_ID": "AKIA"})
        assert credentials.load_credentials("fp1") == {
            "RESTIC_PASSWORD": "p1",
            "AWS_ACCESS_KEY_ID": "AKIA",
        }

    def test_save_overwrites_key(self, tmp_config_dir):
        credentials.save_credentials("fp1", "d", {"RESTIC_PASSWORD": "old"})
        credentials.save_credentials("fp1", "d", {"RESTIC_PASSWORD": "new"})
        assert credentials.load_credentials("fp1") == {"RESTIC_PASSWORD": "new"}

    def test_other_destinations_preserved(self, tmp_config_dir):
        credentials.save_credentials("a", "da", {"RESTIC_PASSWORD": "pa"})
        credentials.save_credentials("b", "db", {"RESTIC_PASSWORD": "pb"})
        credentials.save_credentials("a", "da", {"AWS_ACCESS_KEY_ID": "AKIA"})
        assert credentials.load_credentials("a") == {
            "RESTIC_PASSWORD": "pa",
            "AWS_ACCESS_KEY_ID": "AKIA",
        }
        assert credentials.load_credentials("b") == {"RESTIC_PASSWORD": "pb"}

    def test_missing_fingerprint_returns_none(self, tmp_config_dir):
        assert credentials.load_credentials("nope") is None

    @pytest.mark.skipif(os.name == "nt", reason="chmod is a no-op on Windows")
    def test_chmod_600(self, tmp_config_dir):
        credentials.save_credentials("p", "d", {"K": "v"})
        mode = stat.S_IMODE(credentials.credentials_path().stat().st_mode)
        assert mode == 0o600

    def test_save_creates_config_dir(self, tmp_path, monkeypatch):
        nested = tmp_path / "not" / "yet" / "created"
        monkeypatch.setenv("VECTA_CONFIG_DIR", str(nested))
        credentials.save_credentials("p", "d", {"K": "v"})
        assert (nested / "credentials.toml").exists()

    def test_non_string_value_rejected(self, tmp_config_dir):
        path = credentials.credentials_path()
        path.write_text('[p]\nCOUNT = 3\n', encoding="utf-8")
        with pytest.raises(credentials.CredentialsError, match="must be a string"):
            credentials.load_credentials("p")

    def test_malformed_toml(self, tmp_config_dir):
        path = credentials.credentials_path()
        path.write_text("[p\nbroken", encoding="utf-8")
        with pytest.raises(credentials.CredentialsError, match="Invalid TOML"):
            credentials.load_credentials("p")

    def test_non_table_section(self, tmp_config_dir):
        path = credentials.credentials_path()
        path.write_text('p = "not-a-table"\n', encoding="utf-8")
        with pytest.raises(credentials.CredentialsError, match="must be a table"):
            credentials.load_credentials("p")

    def test_empty_file_returns_none(self, tmp_config_dir):
        assert credentials.load_credentials("p") is None