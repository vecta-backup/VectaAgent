"""HTTP client for the Vecta backend."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

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

    def _check_error(self, response: httpx.Response) -> None:
        if response.status_code == 401:
            raise AgentAuthError(
                self._server_message(response, "Agent is not registered or the credentials are wrong.")
            )
        if response.status_code == 403:
            raise AgentDeactivatedError(
                self._server_message(response, "Agent is deactivated.")
            )
        if response.status_code >= 400:
            raise ApiError(
                f"Unexpected error {response.status_code} from {response.url}: {response.text[:200]}"
            )

    def _request_with_retry(
        self,
        method: str,
        url: str,
        json: dict[str, Any] | None = None,
        auth: bool = True,
        max_retries: int = 3,
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        if auth:
            headers["Authorization"] = self._auth_header

        last_err: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                with httpx.Client(timeout=30.0) as client:
                    response = client.request(method, url, headers=headers, json=json)
                if response.status_code >= 500:
                    last_err = ApiError(
                        f"Server error {response.status_code} from {url}: {response.text[:200]}"
                    )
                else:
                    return response
            except httpx.TimeoutException as exc:
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
                self._server_message(
                    response,
                    "Registration was rejected by the server. "
                    "The token may have already been used or expired - "
                    "generate a new token from the dashboard.",
                )
            )
        self._check_error(response)
        return response.json()

    def me(self) -> dict[str, Any]:
        """GET /agents/{id}/me — validates credentials and stamps last_seen_at."""
        response = self._request_with_retry("GET", self._url(f"/agents/{self.agent_id}/me"))
        self._check_error(response)
        return response.json()

    def fetch_jobs(self) -> list[dict[str, Any]]:
        """GET /agents/{id}/jobs — returns only jobs the backend says are runnable."""
        response = self._request_with_retry("GET", self._url(f"/agents/{self.agent_id}/jobs"))
        self._check_error(response)
        data = response.json()
        return data.get("jobs", [])

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
