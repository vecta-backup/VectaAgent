"""Repository existence check and safe initialization before backup stages."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from vecta_agent import restic

logger = logging.getLogger("vecta_agent")


@dataclass(frozen=True)
class RepositoryOutcome:
    status: str
    exit_code: int | None = None
    message: str | None = None
    duration_seconds: int = 0


def prepare_repository(
    destination: str,
    port: int | None,
    env: dict[str, str],
    cancel_event: threading.Event,
    job_id: str,
    *,
    legacy_file_job: bool,
    destination_timeout_message: Callable[[str], str],
    redact_secrets: Callable[[str, dict[str, str] | None], str],
    auth_failure_hint: Callable[[str, str], str],
) -> RepositoryOutcome:
    """Verify or initialize the destination and return a bounded outcome."""
    started = time.monotonic()
    try:
        check = restic.check_repo(destination, env=env, port=port, cancel_event=cancel_event)
        if check.cancelled or cancel_event.is_set():
            return RepositoryOutcome("cancelled", 130, "Cancelled by user", int(time.monotonic() - started))
        if check.exists is False:
            logger.info("Repository at %s not found; initializing.", destination)
            init = restic.init_repo(destination, env=env, port=port, cancel_event=cancel_event)
            if init.cancelled or cancel_event.is_set():
                return RepositoryOutcome("cancelled", 130, "Cancelled by user", int(time.monotonic() - started))
            if init.exit_code != 0:
                message = (
                    destination_timeout_message("initialize")
                    if init.timed_out
                    else redact_secrets(
                        (init.stderr_tail or "Failed to initialize repository.")
                        + auth_failure_hint(init.stderr_tail, job_id),
                        env,
                    )
                )
                logger.error(
                    "Job %s failed: could not initialize repository at %s: %s",
                    job_id,
                    destination,
                    message,
                )
                return RepositoryOutcome("failed", init.exit_code, message, int(time.monotonic() - started))
            logger.info("Repository at %s initialized.", destination)
        elif check.exists is None:
            message = (
                destination_timeout_message("verify")
                if check.timed_out
                else redact_secrets(
                    (check.stderr_tail or "Could not verify repository at destination.")
                    + auth_failure_hint(check.stderr_tail, job_id),
                    env,
                )
            )
            code = 124 if check.timed_out else 1
            logger.error(
                "Job %s failed: could not verify repository at %s: %s",
                job_id,
                destination,
                message,
            )
            return RepositoryOutcome("failed", code, message, int(time.monotonic() - started))
    except FileNotFoundError:
        message = (
            "restic binary not found in PATH"
            if legacy_file_job
            else "restic executable not found on PATH; install Restic to run backups."
        )
        logger.error("Job %s failed: restic executable not found on PATH.", job_id)
        return RepositoryOutcome("failed", 127, message, int(time.monotonic() - started))
    return RepositoryOutcome("ready", duration_seconds=int(time.monotonic() - started))
