import hashlib

import httpx
import pytest

from vecta_agent import __version__, cli, update


def _resp(status: int, content: bytes | None = None, headers: dict | None = None,
          url: str = "http://test") -> httpx.Response:
    return httpx.Response(status, content=content, headers=headers or {},
                          request=httpx.Request("GET", url))


def _fake_http(monkeypatch, responses: dict[str, httpx.Response]) -> None:
    """Routes update's httpx calls to canned responses keyed by URL."""

    class _FakeClient:
        def __init__(self, **kwargs):
            self._kw = kwargs

        def request(self, method, url, headers=None, json=None):
            if url not in responses:
                raise AssertionError(f"unexpected request: {method} {url}")
            return responses[url]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(update.httpx, "Client", lambda **kw: _FakeClient(**kw))


def _download_urls(tag: str) -> dict[str, str]:
    base = f"https://github.com/vecta-backup/VectaAgent/releases/download/{tag}"
    return {"binary": f"{base}/vecta-agent", "sums": f"{base}/SHA256SUMS"}


class TestParseVersion:
    def test_parses_tags_with_and_without_v(self):
        assert update.parse_version("v0.3.0") == (0, 3, 0)
        assert update.parse_version("0.3.0") == (0, 3, 0)

    def test_invalid_returns_none(self):
        assert update.parse_version("banana") is None
        assert update.parse_version("v1.2") is None
        assert update.parse_version("") is None


class TestIsNewer:
    def test_newer(self):
        assert update.is_newer("v0.4.0", "0.3.0")

    def test_same_is_not_newer(self):
        assert not update.is_newer("v0.3.0", "0.3.0")

    def test_older_is_not_newer(self):
        assert not update.is_newer("v0.2.9", "0.3.0")

    def test_invalid_is_never_newer(self):
        assert not update.is_newer("nonsense", "0.3.0")


class TestLatestVersion:
    def test_resolves_redirect_location(self, monkeypatch):
        _fake_http(monkeypatch, {
            update.RELEASES_LATEST_URL: _resp(
                302,
                headers={"location": "https://github.com/vecta-backup/VectaAgent/releases/tag/v0.9.1"},
                url=update.RELEASES_LATEST_URL,
            )
        })
        assert update.latest_version() == "v0.9.1"

    def test_rejects_non_redirect(self, monkeypatch):
        _fake_http(monkeypatch, {
            update.RELEASES_LATEST_URL: _resp(200, url=update.RELEASES_LATEST_URL)
        })
        with pytest.raises(update.UpdateError, match="latest"):
            update.latest_version()

    def test_rejects_invalid_tag(self, monkeypatch):
        _fake_http(monkeypatch, {
            update.RELEASES_LATEST_URL: _resp(
                302,
                headers={"location": "https://github.com/vecta-backup/VectaAgent/releases/tag/v1.2"},
                url=update.RELEASES_LATEST_URL,
            )
        })
        with pytest.raises(update.UpdateError, match="not a valid version"):
            update.latest_version()

    def test_network_error_wrapped(self, monkeypatch):
        class _BrokenClient:
            def __init__(self, **kw):
                pass

            def request(self, *a, **kw):
                raise httpx.ConnectError("no route to host")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(update.httpx, "Client", lambda **kw: _BrokenClient())
        with pytest.raises(update.UpdateError, match="Could not reach"):
            update.latest_version()


class TestInstall:
    def test_installs_verified_binary_atomically(self, tmp_path, monkeypatch):
        binary = b"vecta-binary-content"
        urls = _download_urls("v0.9.0")
        _fake_http(monkeypatch, {
            urls["binary"]: _resp(200, content=binary, url=urls["binary"]),
            urls["sums"]: _resp(200, content=f"{hashlib.sha256(binary).hexdigest()}  vecta-agent\n".encode(),
                                url=urls["sums"]),
        })
        target = tmp_path / "vecta-agent"
        target.write_bytes(b"old-binary")

        update.install("v0.9.0", str(target))

        assert target.read_bytes() == binary
        assert not (tmp_path / "vecta-agent.new").exists()

    def test_checksum_mismatch_leaves_target_untouched(self, tmp_path, monkeypatch):
        binary = b"vecta-binary-content"
        urls = _download_urls("v0.9.0")
        _fake_http(monkeypatch, {
            urls["binary"]: _resp(200, content=binary, url=urls["binary"]),
            urls["sums"]: _resp(200, content=b"deadbeef  vecta-agent\n", url=urls["sums"]),
        })
        target = tmp_path / "vecta-agent"
        target.write_bytes(b"old-binary")

        with pytest.raises(update.UpdateError, match="Checksum mismatch"):
            update.install("v0.9.0", str(target))

        assert target.read_bytes() == b"old-binary"
        assert not (tmp_path / "vecta-agent.new").exists()

    def test_missing_sums_entry_rejected(self, tmp_path, monkeypatch):
        urls = _download_urls("v0.9.0")
        _fake_http(monkeypatch, {
            urls["binary"]: _resp(200, content=b"data", url=urls["binary"]),
            urls["sums"]: _resp(200, content=b"deadbeef  other-file\n", url=urls["sums"]),
        })
        target = tmp_path / "vecta-agent"
        with pytest.raises(update.UpdateError, match="No checksum entry"):
            update.install("v0.9.0", str(target))

    def test_download_failure_wrapped(self, tmp_path, monkeypatch):
        urls = _download_urls("v0.9.0")
        _fake_http(monkeypatch, {
            urls["binary"]: _resp(404, url=urls["binary"]),
        })
        with pytest.raises(update.UpdateError, match="Download failed"):
            update.install("v0.9.0", str(tmp_path / "vecta-agent"))

    def test_handles_binary_mode_sums_marker(self, tmp_path, monkeypatch):
        binary = b"data"
        urls = _download_urls("v1.0.0")
        _fake_http(monkeypatch, {
            urls["binary"]: _resp(200, content=binary, url=urls["binary"]),
            urls["sums"]: _resp(200, content=f"{hashlib.sha256(binary).hexdigest()} *vecta-agent\n".encode(),
                                url=urls["sums"]),
        })
        update.install("v1.0.0", str(tmp_path / "vecta-agent"))
        assert (tmp_path / "vecta-agent").read_bytes() == binary


class TestRunUpdate:
    def test_refuses_when_running_from_source(self, monkeypatch):
        monkeypatch.setattr(update, "latest_version", lambda: "v9.9.9")
        with pytest.raises(update.UpdateError, match="running from source"):
            update.run_update()

    def test_check_reports_up_to_date(self, monkeypatch, capsys):
        monkeypatch.setattr(update, "latest_version", lambda: "v0.3.0")
        update.run_update(check_only=True)
        assert "up to date" in capsys.readouterr().out

    def test_check_reports_available_update(self, monkeypatch, capsys):
        monkeypatch.setattr(update, "latest_version", lambda: "v9.9.9")
        update.run_update(check_only=True)
        out = capsys.readouterr().out
        assert f"{__version__} -> 9.9.9" in out
        assert "sudo vecta-agent update" in out

    def test_install_flow(self, tmp_path, monkeypatch, capsys):
        installed = []

        def fake_install(tag, target):
            installed.append((tag, target))

        monkeypatch.setattr(update, "latest_version", lambda: "v9.9.9")
        monkeypatch.setattr(update, "target_binary_path", lambda: str(tmp_path / "vecta-agent"))
        monkeypatch.setattr(update, "install", fake_install)

        update.run_update()

        assert installed == [("v9.9.9", str(tmp_path / "vecta-agent"))]
        out = capsys.readouterr().out
        assert "Updated vecta-agent" in out

    def test_up_to_date_skips_install(self, monkeypatch, capsys):
        monkeypatch.setattr(update, "latest_version", lambda: f"v{__version__}")
        monkeypatch.setattr(update, "target_binary_path",
                            lambda: (_ for _ in ()).throw(AssertionError("should not resolve target")))
        update.run_update()
        assert "already up to date" in capsys.readouterr().out

    def test_explicit_version_installs_even_if_older(self, tmp_path, monkeypatch, capsys):
        installed = []
        monkeypatch.setattr(update, "target_binary_path", lambda: str(tmp_path / "vecta-agent"))
        monkeypatch.setattr(update, "install", lambda tag, target: installed.append(tag))
        update.run_update(requested_version="0.1.0")
        assert installed == ["v0.1.0"]

    def test_explicit_version_without_v_prefix(self, tmp_path, monkeypatch, capsys):
        installed = []
        monkeypatch.setattr(update, "target_binary_path", lambda: str(tmp_path / "vecta-agent"))
        monkeypatch.setattr(update, "install", lambda tag, target: installed.append(tag))
        update.run_update(requested_version="1.2.3")
        assert installed == ["v1.2.3"]

    def test_invalid_explicit_version_rejected(self, monkeypatch, capsys):
        with pytest.raises(update.UpdateError, match="Invalid version"):
            update.run_update(requested_version="not-a-version")


class TestUpdateCli:
    def test_default_invocation(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(update, "run_update",
                            lambda requested_version=None, check_only=False:
                            captured.update(version=requested_version, check=check_only))
        cli.main(["update"])
        assert captured == {"version": None, "check": False}

    def test_check_flag(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(update, "run_update",
                            lambda requested_version=None, check_only=False:
                            captured.update(version=requested_version, check=check_only))
        cli.main(["update", "--check"])
        assert captured == {"version": None, "check": True}

    def test_version_flag(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(update, "run_update",
                            lambda requested_version=None, check_only=False:
                            captured.update(version=requested_version, check=check_only))
        cli.main(["update", "--version", "v1.2.3"])
        assert captured == {"version": "v1.2.3", "check": False}

    def test_update_error_prints_message(self, monkeypatch, capsys):
        def raise_update_error(requested_version=None, check_only=False):
            raise update.UpdateError("Checksum mismatch; aborting.")

        monkeypatch.setattr(update, "run_update", raise_update_error)
        with pytest.raises(SystemExit) as exc:
            cli.main(["update"])
        assert exc.value.code == 1
        assert "Error: Checksum mismatch" in capsys.readouterr().err