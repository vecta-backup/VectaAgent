"""Job exclusion/retention validation and safe post-backup pruning."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from vecta_agent import environment, restic, status


def validate_job_storage_config(
    job: dict[str, Any], backup_type: str
) -> tuple[list[str], dict[str, int | None] | None]:
    excludes = restic.validate_exclude_patterns(job.get("exclude_patterns", []))
    if backup_type == "database" and excludes:
        raise ValueError("Database jobs cannot use filesystem exclude patterns.")
    policy = restic.validate_retention_policy(job.get("retention_policy"))
    return excludes, policy


def run_prune(
    destination: str,
    port: int | None,
    env: dict[str, str],
    policy: dict[str, int | None] | None,
    cancel_event: threading.Event,
    *,
    job_id: str,
) -> tuple[dict[str, Any] | None, str | None, list[str]]:
    """Run configured post-snapshot pruning and return stage, safe message, and deleted IDs."""
    if policy is None:
        return None, None, []
    started = time.monotonic()
    try:
        result = restic.run_forget(
            destination, policy, port=port, env=env, cancel_event=cancel_event,
            job_id=job_id,
        )
    except FileNotFoundError:
        result = restic.ResticResult(127, stderr_tail="Restic executable not found on PATH.")
    except Exception:
        result = restic.ResticResult(1, stderr_tail="Restic pruning could not be completed safely.")
    duration = int(time.monotonic() - started)
    if result.exit_code == 0 and not result.cancelled and not result.timed_out:
        message = result.recovery_message
        safe = environment.redact_secrets(message, env) if message else None
        try:
            parsed_json = json.loads(result.stdout)
            pruned_ids = [
                snapshot["id"]
                for group in parsed_json
                for snapshot in group.get("remove", [])
                if isinstance(snapshot, dict) and "id" in snapshot
            ]
        except (json.JSONDecodeError, TypeError, AttributeError):
            pruned_ids = []
        return status.stage_result("prune", "success", 0, duration, safe), None, pruned_ids
    if result.cancelled:
        reason = "Pruning was cancelled after the snapshot was committed."
    elif result.timed_out:
        reason = f"Pruning timed out after {restic.PRUNE_TIMEOUT_SECONDS}s."
    else:
        reason = result.stderr_tail or f"Restic forget --prune exited with code {result.exit_code}."
    if result.recovery_message:
        reason = f"{reason} {result.recovery_message}"
    safe_message = environment.redact_secrets(reason, env)
    return status.stage_result("prune", "failed", result.exit_code, duration, safe_message), safe_message, []
