import httpx
import pytest

from vecta_agent import api


def _resp(method, url, status=200, json=None, text=""):
    body = json if json is not None else text
    if json is not None:
        return httpx.Response(status, json=json, request=httpx.Request(method, url))
    return httpx.Response(status, text=text, request=httpx.Request(method, url))


class TestApiClient:
    def test_register_success(self, monkeypatch):
        calls = []

        def fake_request(method, url, headers=None, json=None):
            calls.append((method, url, headers, json))
            return _resp(method, url, 200, {"agent_id": "a1", "api_key": "vc_k1", "name": "n1"})

        monkeypatch.setattr(api.httpx, "Client", lambda **kw: _FakeClient(fake_request))
        client = api.ApiClient("", "", base_url="http://test/api")
        result = client.register("tok")
        assert result["agent_id"] == "a1"
        assert calls[0][0] == "POST"
        assert calls[0][3] == {"registration_token": "tok"}

    def test_register_token_used(self, monkeypatch):
        def fake_request(method, url, headers=None, json=None):
            return _resp(method, url, 400, {"success": False, "message": "Registration token has already been used"})

        monkeypatch.setattr(api.httpx, "Client", lambda **kw: _FakeClient(fake_request))
        client = api.ApiClient("", "", base_url="http://test/api")
        with pytest.raises(api.RegisterError, match="already been used"):
            client.register("tok")

    def test_register_token_invalid(self, monkeypatch):
        def fake_request(method, url, headers=None, json=None):
            return _resp(method, url, 401, {"success": False, "message": "Invalid or expired token"})

        monkeypatch.setattr(api.httpx, "Client", lambda **kw: _FakeClient(fake_request))
        client = api.ApiClient("", "", base_url="http://test/api")
        with pytest.raises(api.RegisterError, match="invalid or expired"):
            client.register("tok")

    def test_register_forbidden_uses_server_message(self, monkeypatch):
        def fake_request(method, url, headers=None, json=None):
            return _resp(method, url, 403, {"success": False, "message": "Token already used"})

        monkeypatch.setattr(api.httpx, "Client", lambda **kw: _FakeClient(fake_request))
        client = api.ApiClient("", "", base_url="http://test/api")
        with pytest.raises(api.RegisterError, match="Token already used"):
            client.register("tok")

    def test_register_forbidden_fallback_message(self, monkeypatch):
        def fake_request(method, url, headers=None, json=None):
            return _resp(method, url, 403, text="Forbidden")

        monkeypatch.setattr(api.httpx, "Client", lambda **kw: _FakeClient(fake_request))
        client = api.ApiClient("", "", base_url="http://test/api")
        with pytest.raises(api.RegisterError, match="rejected by the server"):
            client.register("tok")

    def test_unexpected_status_raises_api_error(self, monkeypatch):
        def fake_request(method, url, headers=None, json=None):
            return _resp(method, url, 404, text="not found")

        monkeypatch.setattr(api.httpx, "Client", lambda **kw: _FakeClient(fake_request))
        client = api.ApiClient("a1", "vc_k1", base_url="http://test/api")
        with pytest.raises(api.ApiError, match="404"):
            client.me()

    def test_401_uses_server_message(self, monkeypatch):
        def fake_request(method, url, headers=None, json=None):
            return _resp(method, url, 401, {"success": False, "message": "credential check failed"})

        monkeypatch.setattr(api.httpx, "Client", lambda **kw: _FakeClient(fake_request))
        client = api.ApiClient("a1", "vc_k1", base_url="http://test/api")
        with pytest.raises(api.AgentAuthError, match="credential check failed"):
            client.me()

    def test_me(self, monkeypatch):
        calls = []

        def fake_request(method, url, headers=None, json=None):
            calls.append((method, url, headers))
            return _resp(method, url, 200, {"agent_id": "a1", "name": "n1", "is_active": True})

        monkeypatch.setattr(api.httpx, "Client", lambda **kw: _FakeClient(fake_request))
        client = api.ApiClient("a1", "vc_k1", base_url="http://test/api")
        result = client.me()
        assert result["name"] == "n1"
        assert calls[0][2]["Authorization"] == "Bearer vc_k1"

    def test_fetch_jobs(self, monkeypatch):
        def fake_request(method, url, headers=None, json=None):
            return _resp(method, url, 200, {"jobs": [{"job_id": "j1"}]})

        monkeypatch.setattr(api.httpx, "Client", lambda **kw: _FakeClient(fake_request))
        client = api.ApiClient("a1", "vc_k1", base_url="http://test/api")
        jobs = client.fetch_jobs()
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == "j1"

    def test_report_status(self, monkeypatch):
        calls = []

        def fake_request(method, url, headers=None, json=None):
            calls.append(json)
            return _resp(method, url, 200, {"status": "success"})

        monkeypatch.setattr(api.httpx, "Client", lambda **kw: _FakeClient(fake_request))
        client = api.ApiClient("a1", "vc_k1", base_url="http://test/api")
        client.report_status("j1", {"status": "running"})
        assert calls[0]["status"] == "running"

    def test_cancel_status(self, monkeypatch):
        def fake_request(method, url, headers=None, json=None):
            return _resp(method, url, 200, {"cancel_requested": True})

        monkeypatch.setattr(api.httpx, "Client", lambda **kw: _FakeClient(fake_request))
        client = api.ApiClient("a1", "vc_k1", base_url="http://test/api")
        result = client.cancel_status("j1")
        assert result["cancel_requested"] is True

    def test_401_raises(self, monkeypatch):
        def fake_request(method, url, headers=None, json=None):
            return _resp(method, url, 401, {"success": False, "message": "bad"})

        monkeypatch.setattr(api.httpx, "Client", lambda **kw: _FakeClient(fake_request))
        client = api.ApiClient("a1", "vc_k1", base_url="http://test/api")
        with pytest.raises(api.AgentAuthError):
            client.me()

    def test_403_raises_deactivated(self, monkeypatch):
        def fake_request(method, url, headers=None, json=None):
            return _resp(method, url, 403, {"success": False, "message": "deactivated"})

        monkeypatch.setattr(api.httpx, "Client", lambda **kw: _FakeClient(fake_request))
        client = api.ApiClient("a1", "vc_k1", base_url="http://test/api")
        with pytest.raises(api.AgentDeactivatedError, match="deactivated"):
            client.me()

    def test_server_error_retries_then_raises(self, monkeypatch):
        attempt = 0

        def fake_request(method, url, headers=None, json=None):
            nonlocal attempt
            attempt += 1
            return _resp(method, url, 500, text="boom")

        monkeypatch.setattr(api.httpx, "Client", lambda **kw: _FakeClient(fake_request))
        monkeypatch.setattr(api, "time", _FakeTime())
        client = api.ApiClient("a1", "vc_k1", base_url="http://test/api")
        with pytest.raises(api.ApiError):
            client.me()
        assert attempt == 4  # initial + 3 retries


class _FakeClient:
    def __init__(self, request_fn):
        self._request = request_fn

    def request(self, method, url, headers=None, json=None):
        return self._request(method, url, headers, json)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _FakeTime:
    def sleep(self, seconds):
        pass
