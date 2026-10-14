"""Terminal status payload construction and run outcome reporting."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from vecta_agent import api

logger = logging.getLogger("vecta_agent")


def report_terminal(
    client: api.ApiClient,
    job_id: str,
    run_id: str,
    status: str,
    code: int | None,
    message: str,
    stage: str,
    start_time: float,
    metadata: dict[str, Any],
    stage_results: list[dict[str, Any]],
    snapshot_stats: dict[str, Any],
    *,
    max_stage_results: int,
    max_stage_message_chars: int,
) -> None:
    payload: dict[str, Any] = {
        "job_id": job_id,
        "run_id": run_id,
        "status": status,
        "exit_code": code,
        "message": message[:max_stage_message_chars],
        "duration_seconds": int(time.monotonic() - start_time),
        **metadata,
        "stage_results": stage_results[-max_stage_results:],
        "pruned_snapshots": snapshot_stats.get("pruned_snapshots", []),
    }
    if stage in {"validation", "pre_hook", "source_capture", "database_dump", "restic", "post_hook", "prune"}:
        payload["stage"] = stage
    elif stage == "repository":
        payload["stage"] = "restic"
    payload.update({
        key: snapshot_stats.get(key)
        for key in ("snapshot_id", "files_processed", "bytes_processed", "transferred_bytes")
        if key in snapshot_stats
    })
    client.report_status(job_id, payload)


def finalize_run(
    *,
    primary_failure: tuple[int, str, str] | None,
    backup_type: str,
    post_selection,
    prune_message: str | None,
    job_id: str,
    stage_results: list[dict[str, Any]],
    snapshot_stats: dict[str, Any],
    report_terminal: Callable[..., None],
    stage_result: Callable[..., dict[str, Any]],
) -> None:
    """Report the terminal success, warning, or failure for a completed run."""
    if primary_failure is not None:
        code, failed_stage, message = primary_failure
        report_terminal("failed", code, message, failed_stage)
        if code == 130:
            logger.info("Job %s cancelled.", job_id)
        else:
            logger.error(
                "Job %s failed during %s with exit code %s: %s",
                job_id,
                failed_stage,
                code,
                message,
            )
        return

    zero_files = backup_type == "files" and any(
        result["stage"] == "restic" and result["status"] == "success"
        for result in stage_results
    ) and snapshot_stats.get("_summary_seen") and snapshot_stats.get("files_processed") == 0
    message = prune_message or (
        "Backup completed but no files were processed. Check the source path."
        if zero_files
        else "Backup completed"
    )
    terminal_stage = "prune" if prune_message else (
        "post_hook" if post_selection else ("source_capture" if backup_type == "database" else "restic")
    )
    report_terminal("warning" if zero_files or prune_message else "success", 0, message, terminal_stage)
    if prune_message:
        logger.warning("Job %s completed with pruning warning: %s", job_id, prune_message)
    elif zero_files:
        logger.warning("Job %s completed with no files processed.", job_id)
    else:
        logger.info("Job %s succeeded (snapshot %s).", job_id, snapshot_stats.get("snapshot_id"))


def report_validation_failure(
    job_id: str,
    error: Exception,
    stage_results: list[dict[str, Any]],
    report_terminal: Callable[..., None],
    stage_result: Callable[..., dict[str, Any]],
    *,
    max_stage_message_chars: int,
) -> None:
    message = str(error)[:max_stage_message_chars]
    stage_results.append(stage_result("validation", "failed", 1, None, message))
    report_terminal("failed", 1, message, "validation")
    logger.error("Job %s failed validation: %s", job_id, message)
