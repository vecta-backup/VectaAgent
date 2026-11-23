"""HTTP client for the Vecta backend."""

from __future__ import annotations

import os
import re
import time
from typing import Any

import httpx

from vecta_agent import __version__

BASE_URL = os.environ.get("VECTA_BASE_URL", "https://vectaapp.com/api")


class AgentAuthError(Exception):
    """Raised on 401/403 from the backend."""


class AgentDeactivatedError(AgentAuthError):
    """Raised when the agent is deactivated (403)."""


class ApiError(Exception):
    """Raised for unexpected API responses."""


class RegisterError(ApiError):
    """Raised when a registration attempt fails with a clear reason."""


class ApiClient:
    """Thin wrapper around httpx for the Vecta agent API."""

    def __init__(self, agent_id: str, api_key: str, base_url: str | None = None) -> None:
        self.agent_id = agent_id
        self.api_key = api_key
        self.base_url = (base_url or BASE_URL).rstrip("/")
        self._auth_header = f"Bearer {api_key}"

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    @staticmethod
    def _server_message(response: httpx.Response, default: str) -> str:
        """Prefer the backend's JSON `message` field when present."""
        try:
            body = response.json()
        except ValueError:
            return default
        if isinstance(body, dict) and body.get("message"):
            return body["message"]
        return default

    def _redact_sensitive_text(self, message: str, *sensitive_values: str) -> str:
        for value in (self.api_key, *sensitive_values):
            if value:
                message = message.replace(value, "[REDACTED]")
        return re.sub(r"\bvc_[A-Za-z0-9_-]{32,}\b", "[REDACTED]", message)

    def _check_error(self, response: httpx.Response) -> None:
        if response.status_code == 401:
            raise AgentAuthError(
                self._redact_sensitive_text(
                    self._server_message(
                        response,
                        "Agent is not registered or the credentials are wrong.",
                    )
                )
            )
        if response.status_code == 403:
            raise AgentDeactivatedError(
                self._redact_sensitive_text(
                    self._server_message(response, "Agent is deactivated.")
                )
            )
        if response.status_code >= 400:
            raise ApiError(
                self._redact_sensitive_text(
                    f"Unexpected error {response.status_code} from {response.url}: "
                    f"{response.text[:200]}"
                )
            )

    def _request_with_retry(
        self,
        method: str,
        url: str,
        json: dict[str, Any] | None = None,
        auth: bool = True,
        max_retries: int = 3,
        safe_errors: bool = False,
    ) -> httpx.Response:
        headers: dict[str, str] = {"X-Vecta-Agent-Version": __version__}
        if auth:
            headers["Authorization"] = self._auth_header

        last_err: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                with httpx.Client(timeout=30.0) as client:
                    response = client.request(method, url, headers=headers, json=json)
                if response.status_code >= 500:
                    details = "" if safe_errors else f": {response.text[:200]}"
                    details = self._redact_sensitive_text(details)
                    last_err = ApiError(f"Server error {response.status_code} from {url}{details}")
                else:
                    return response
            except httpx.TimeoutException:
                last_err = ApiError(f"Request to {url} timed out after 30.0s")
            except httpx.NetworkError as exc:
                last_err = ApiError(f"Could not reach {url}: {exc}")
            except httpx.HTTPStatusError as exc:
                return exc.response

            if attempt < max_retries:
                backoff = 2 ** attempt
                time.sleep(backoff)

        raise last_err or ApiError(f"Request to {url} failed after retries.")

    def register(self, token: str) -> dict[str, Any]:
        """Exchange a registration token for agent credentials (no auth)."""
        response = self._request_with_retry(
            "POST",
            self._url("/agents/register"),
            json={"registration_token": token},
            auth=False,
            max_retries=1,
            safe_errors=True,
        )
        if response.status_code == 200:
            return response.json()
        if response.status_code == 400:
            raise RegisterError("Registration token has already been used.")
        if response.status_code == 401:
            raise RegisterError(
                "Registration token is invalid or expired. "
                "Generate a new token from the dashboard and try again."
            )
        if response.status_code == 403:
            raise RegisterError(
                self._redact_sensitive_text(
                    self._server_message(
                        response,
                        "Registration was rejected by the server. "
                        "The token may have already been used or expired - "
                        "generate a new token from the dashboard.",
                    ),
                    token,
                )
            )
        self._check_error(response)
        return response.json()

    def rotate_key(self, rotation_token: str) -> dict[str, Any]:
        """Exchange a one-time rotation token for a fresh API key.

        This operation is deliberately not retried: if the server consumed the
        token but the response was lost, retrying cannot safely recover the
        one-time raw key.
        """
        response = self._request_with_retry(
            "POST",
            self._url("/agents/rotate"),
            json={"agent_id": self.agent_id, "rotation_token": rotation_token},
            auth=False,
            max_retries=0,
            safe_errors=True,
        )
        if response.status_code != 200:
            # Do not surface arbitrary server text here: it could echo the
            # submitted credential or other sensitive request details.
            message = self._redact_sensitive_text(
                self._server_message(response, "API key rotation was rejected."),
                rotation_token,
            )
            raise ApiError(message)
        result = response.json()
        api_key = result.get("api_key") if isinstance(result, dict) else None
        if not isinstance(api_key, str) or not re.fullmatch(r"vc_[0-9a-f]{32}", api_key):
            raise ApiError("The backend returned an invalid API key during rotation.")
        if result.get("agent_id") != self.agent_id:
            raise ApiError("The backend returned a key for a different agent.")
        return result

    def me(self) -> dict[str, Any]:
        """GET /agents/{id}/me — validates credentials and stamps last_seen_at."""
        response = self._request_with_retry("GET", self._url(f"/agents/{self.agent_id}/me"))
        self._check_error(response)
        return response.json()

    def report_capabilities(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST the agent's validated local capabilities."""
        response = self._request_with_retry(
            "POST",
            self._url(f"/agents/{self.agent_id}/capabilities"),
            json=payload,
        )
        self._check_error(response)
        return response.json()

    def fetch_jobs(self) -> list[dict[str, Any]]:
        """GET /agents/{id}/jobs — returns only jobs the backend says are runnable."""
        response = self._request_with_retry("GET", self._url(f"/agents/{self.agent_id}/jobs"))
        self._check_error(response)
        data = response.json()
        return data.get("jobs", [])

    def get_job(self, job_id: str) -> dict[str, Any]:
        """GET /agents/{id}/jobs/{job_id} — fetch one job's non-secret config on demand.

        Unlike fetch_jobs, this returns the job regardless of whether it is
        currently due to run (used by `vecta-agent setup`).
        """
        response = self._request_with_retry(
            "GET", self._url(f"/agents/{self.agent_id}/jobs/{job_id}")
        )
        self._check_error(response)
        return response.json()

    def report_status(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /agents/{id}/jobs/{job_id}/status."""
        response = self._request_with_retry(
            "POST",
            self._url(f"/agents/{self.agent_id}/jobs/{job_id}/status"),
            json=payload,
        )
        self._check_error(response)
        return response.json()

    def cancel_status(self, job_id: str) -> dict[str, Any]:
        """GET /agents/{id}/jobs/{job_id}/cancel-status."""
        response = self._request_with_retry(
            "GET",
            self._url(f"/agents/{self.agent_id}/jobs/{job_id}/cancel-status"),
        )
        self._check_error(response)
        return response.json()
