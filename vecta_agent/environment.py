"""Local Restic environment loading and safe credential diagnostics."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from vecta_agent import api, credentials, status

logger = logging.getLogger("vecta_agent")

MAX_SECRET_MESSAGE_CHARS = 500
_SECRET_ENV_MARKERS = (
    "PASSWORD", "SECRET", "TOKEN", "ACCESS_KEY", "ACCOUNT_KEY", "CREDENTIAL", "AUTH",
)
_AUTH_ERROR_MARKERS = (
    "access denied",
    "accessdenied",
    "unauthorized",
    "forbidden",
    "auth",
    "credentials",
    "signature",
    "invalidaccesskeyid",
    "wrong password",
    "passphrase",
    "unable to open config file",
    "permission denied",
    "publickey",
)


def load_restic_env(config_dir: Path) -> dict[str, str]:
    """Load the optional key=value Restic environment file."""
    env_path = config_dir / "restic.env"
    if not env_path.exists():
        return {}

    result: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        result[key] = value
    return result


def save_restic_env(entries: dict[str, str], config_dir: Path) -> Path:
    """Update Restic environment entries while preserving unrelated lines."""
    env_path = config_dir / "restic.env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    for key, value in entries.items():
        marker = f"{key}="
        for index, line in enumerate(lines):
            if line.strip().startswith(marker):
                lines[index] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(env_path, 0o600)
    return env_path


def has_repo_password(restic_env: dict[str, str]) -> bool:
    """Whether Restic can obtain a repository password from process or file env."""
    merged = {**os.environ, **restic_env}
    return bool(
        merged.get("RESTIC_PASSWORD")
        or merged.get("RESTIC_PASSWORD_FILE")
        or merged.get("RESTIC_PASSWORD_COMMAND")
    )


def auth_failure_hint(stderr: str, job_id: str) -> str:
    """Return setup advice when Restic diagnostics look authentication-related."""
    low = stderr.lower()
    if any(marker in low for marker in _AUTH_ERROR_MARKERS):
        return (
            " This looks like an authentication failure. Run "
            f"'vecta-agent setup {job_id}' on this machine to configure the "
            "credentials for this destination."
        )
    return ""


def redact_secrets(message: str, env: dict[str, str] | None) -> str:
    """Remove known local secret values before logging or reporting a message."""
    result = message
    for key, value in (env or {}).items():
        if value and any(marker in key.upper() for marker in _SECRET_ENV_MARKERS):
            result = result.replace(value, "[REDACTED]")
    return result[:MAX_SECRET_MESSAGE_CHARS]


def resolve_job_env(
    client: api.ApiClient,
    job_id: str,
    job: dict[str, Any],
    restic_env: dict[str, str],
    run_id: str,
    status_metadata: dict[str, Any] | None = None,
) -> dict[str, str] | None:
    """Merge process-wide Restic values with per-destination stored secrets."""
    job_env = dict(restic_env)
    try:
        stored = credentials.load_credentials(
            credentials.destination_fingerprint(job["destination"])
        )
    except credentials.CredentialsError as exc:
        client.report_status(
            job_id,
            {
                "job_id": job_id,
                "run_id": run_id,
                "status": "failed",
                **(status_metadata or {}),
                "exit_code": 1,
                "message": f"Invalid local credentials file: {exc}",
                "stage_results": [status.stage_result(
                    "validation", "failed", 1, None, "Invalid local credentials file."
                )],
            },
        )
        logger.error("Job %s failed: invalid local credentials file: %s", job_id, exc)
        return None
    if stored:
        job_env.update(stored)
    return job_env
