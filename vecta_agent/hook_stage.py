"""Execution wrapper for cancellable pre/post hook stages."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from vecta_agent import api, hooks


def run_hook_stage(
    *,
    client: api.ApiClient,
    job_id: str,
    run_id: str,
    start_time: float,
    metadata: dict[str, Any],
    selection,
    phase: str,
    cancel_event: threading.Event,
    progress_reporter: Callable[..., None],
    progress_interval_seconds: float,
    cleanup_mode: bool = False,
) -> tuple[hooks.HookResult | None, int]:
    """Run a selected hook with status updates and bounded reporting cleanup."""
    definition, parameters = selection
    stage_started = time.monotonic()
    stage_done = threading.Event()
    stage_stats = {"stage": phase}
    client.report_status(
        job_id,
        {"job_id": job_id, "run_id": run_id, "status": "running", "stage": phase, **metadata},
    )
    progress_thread = threading.Thread(
        target=progress_reporter,
        args=(client, job_id, run_id, start_time, stage_stats, stage_done, metadata),
        daemon=True,
    )
    progress_thread.start()
    try:
        result = hooks.execute_hook(
            definition,
            parameters,
            cancel_event=None if cleanup_mode else cancel_event,
        )
    except Exception:
        result = None
    finally:
        stage_done.set()
        progress_thread.join(timeout=progress_interval_seconds + 2)
    return result, int(time.monotonic() - stage_started)


def record_pre_hook_result(result, duration, stage_results, stage_result):
    if result is None:
        message = "Pre-hook could not be started safely."
        stage_results.append(stage_result("pre_hook", "failed", 1, duration, message))
        return 1, "pre_hook", message
    outcome = "cancelled" if result.cancelled else "failed" if result.exit_code or result.timed_out else "success"
    message = (
        "Cancelled by user" if result.cancelled
        else "Pre-hook timed out." if result.timed_out
        else f"Pre-hook failed with exit code {result.exit_code}." if result.exit_code
        else None
    )
    stage_results.append(stage_result("pre_hook", outcome, result.exit_code, duration, message))
    if outcome == "success":
        return None
    code = 130 if result.cancelled else 124 if result.timed_out else result.exit_code
    return code, "pre_hook", message or "Pre-hook failed."


def record_post_hook_result(result, duration, stage_results, stage_result):
    if result is None:
        message = "Post-hook could not be started safely."
        stage_results.append(stage_result("post_hook", "failed", 1, duration, message))
        return 1, "post_hook", message
    failed = bool(result.exit_code or result.timed_out or result.cancelled)
    message = (
        "Cancelled by user" if result.cancelled
        else "Post-hook timed out." if result.timed_out
        else f"Post-hook failed with exit code {result.exit_code}." if result.exit_code
        else None
    )
    outcome = "cancelled" if result.cancelled else "failed" if failed else "success"
    stage_results.append(stage_result("post_hook", outcome, result.exit_code, duration, message))
    if not failed:
        return None
    code = 130 if result.cancelled else 124 if result.timed_out else result.exit_code
    return code, "post_hook", message or "Post-hook failed."
