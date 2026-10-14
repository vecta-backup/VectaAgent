"""Shared run-stage result schema and bounded status-report constants."""

from __future__ import annotations

import logging
import time
from typing import Any

from vecta_agent import api

logger = logging.getLogger("vecta_agent")
MAX_STAGE_RESULTS = 8
MAX_STAGE_MESSAGE_CHARS = 500
STAGE_RESULT_STAGES = frozenset({
    "repository", "validation", "pipeline", "pre_hook", "source_capture",
    "database_dump", "restic", "post_hook", "prune",
})
STAGE_RESULT_STATUSES = frozenset({"success", "failed", "skipped", "cancelled", "warning"})


def stage_result(
    stage: str,
    status: str,
    exit_code: int | None,
    duration: int | None,
    message: str | None = None,
) -> dict[str, Any]:
    if stage not in STAGE_RESULT_STAGES:
        raise ValueError(f"Unsupported run stage: {stage}")
    if status not in STAGE_RESULT_STATUSES:
        raise ValueError(f"Unsupported run stage status: {status}")
    return {
        "stage": stage[:40],
        "status": status[:32],
        "exit_code": exit_code,
        "duration_seconds": duration,
        "message": message[:MAX_STAGE_MESSAGE_CHARS] if message else None,
    }


def report_cancelled(
    client: api.ApiClient,
    job_id: str,
    run_id: str,
    start_time: float,
    metadata: dict[str, Any] | None = None,
    stage_results: list[dict[str, Any]] | None = None,
    stage: str = "restic",
) -> None:
    """Report the standard user-cancellation failure for one job run."""
    duration = int(time.monotonic() - start_time)
    client.report_status(
        job_id,
        {
            "job_id": job_id,
            "run_id": run_id,
            "status": "failed",
            **(metadata or {}),
            "stage": stage,
            "exit_code": 130,
            "message": "Cancelled by user",
            "duration_seconds": duration,
            "stage_results": (stage_results or [
                stage_result(stage, "cancelled", 130, duration, "Cancelled by user")
            ])[-MAX_STAGE_RESULTS:],
        },
    )
    logger.info("Job %s cancelled.", job_id)
