"""Progress, cancellation, and timeout supervision for active job stages."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from vecta_agent import api, restic

logger = logging.getLogger("vecta_agent")


def progress_reporter(
    client: api.ApiClient,
    job_id: str,
    run_id: str,
    start_time: float,
    stats: dict[str, Any],
    done_event: threading.Event,
    metadata: dict[str, Any] | None = None,
    *,
    interval_seconds: float,
) -> None:
    """Post running status periodically while a backup or hook is active."""
    while not done_event.wait(interval_seconds):
        with threading.Lock():
            payload: dict[str, Any] = {
                "job_id": job_id,
                "run_id": run_id,
                "status": "running",
                **(metadata or {}),
                "stage": stats.get("stage", "restic"),
                "duration_seconds": int(time.monotonic() - start_time),
                "files_processed": stats.get("files_processed"),
                "bytes_processed": stats.get("bytes_processed"),
            }
            progress = stats.get("progress")
            if progress is not None:
                payload["progress"] = progress
            transferred = stats.get("transferred_bytes")
            if transferred is not None:
                payload["transferred_bytes"] = transferred

        try:
            client.report_status(job_id, payload)
        except api.AgentAuthError:
            raise
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to report progress for job %s: %s", job_id, exc)


class JobTerminator:
    """Terminate the active Restic command or signal a one-shot repo probe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._probe_event: threading.Event | None = None
        self._runner: restic.ResticRunner | None = None

    def set_probe(self, event: threading.Event) -> None:
        with self._lock:
            self._probe_event = event
            self._runner = None

    def set_runner(self, runner: restic.ResticRunner) -> None:
        with self._lock:
            self._probe_event = None
            self._runner = runner

    def terminate(self, grace_seconds: float = 10.0) -> None:
        with self._lock:
            probe_event = self._probe_event
            runner = self._runner
        if runner is not None:
            runner.terminate(grace_seconds)
        elif probe_event is not None:
            probe_event.set()


def cancel_poller(
    client: api.ApiClient,
    job_id: str,
    cancel_event: threading.Event,
    done_event: threading.Event,
    terminator: JobTerminator,
    *,
    interval_seconds: float,
) -> None:
    """Poll the backend and terminate active Restic work when requested."""
    while not done_event.wait(interval_seconds):
        try:
            data = client.cancel_status(job_id)
        except api.AgentAuthError:
            raise
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to poll cancel status for job %s: %s", job_id, exc)
            continue

        if data.get("cancel_requested"):
            cancel_event.set()
            terminator.terminate()
            logger.info("Cancellation requested for job %s; terminating restic.", job_id)
            break


def timeout_watchdog(
    runner: restic.ResticRunner,
    timeout_event: threading.Event,
    done_event: threading.Event,
    *,
    max_duration_seconds: float,
    timeout_message: Callable[[], str],
) -> None:
    """Terminate Restic after its configured maximum stage duration."""
    if done_event.wait(max_duration_seconds):
        return
    timeout_event.set()
    runner.terminate()
    logger.warning("Job exceeded the maximum duration; terminating restic. %s", timeout_message())


def retry_backup_after_lock(
    runner_factory: Callable[[], restic.ResticRunner],
    stderr: str,
    destination: str,
    port: int | None,
    env: dict[str, str],
    client: api.ApiClient,
    job_id: str,
    run_id: str,
    start_time: float,
    cancel_event: threading.Event,
    terminator: JobTerminator,
    metadata: dict[str, Any],
    *,
    progress_reporter: Callable[..., None],
    timeout_watchdog: Callable[..., None],
    cancel_poller: Callable[..., None],
    cancel_interval_seconds: float,
    redact_secrets: Callable[[str, dict[str, str] | None], str],
) -> tuple[restic.ResticRunner | None, dict[str, Any], int | None, bool, bool, bool, str | None]:
    """Unlock a detected stale lock and retry one backup exactly once."""
    if not restic.is_repository_lock_error(stderr) or cancel_event.is_set():
        return None, {}, None, False, False, False, None
    try:
        unlock = restic.unlock_repo(destination, port=port, env=env, cancel_event=cancel_event)
    except Exception:
        return None, {}, None, False, False, False, "Lock recovery: unlock could not be started safely."
    recovery = f"Lock recovery: unlock exited {unlock.exit_code}."
    if unlock.exit_code != 0 or unlock.timed_out or unlock.cancelled:
        details = redact_secrets(unlock.stderr_tail, env) if unlock.stderr_tail else ""
        return None, {}, None, unlock.timed_out, unlock.cancelled, False, f"{recovery} {details}".strip()

    try:
        retry_runner = runner_factory()
        retry_runner.start()
    except Exception:
        return None, {}, 1, False, False, False, f"{recovery} Backup retry could not be started."
    terminator.set_runner(retry_runner)
    stats: dict[str, Any] = {"stage": "restic"}
    attempt_done = threading.Event()
    timeout_event = threading.Event()
    progress_thread = threading.Thread(
        target=progress_reporter,
        args=(client, job_id, run_id, start_time, stats, attempt_done, metadata),
        daemon=True,
    )
    timeout_thread = threading.Thread(
        target=timeout_watchdog,
        args=(retry_runner, timeout_event, attempt_done),
        daemon=True,
    )
    cancel_done = threading.Event()
    cancel_thread = threading.Thread(
        target=cancel_poller,
        args=(client, job_id, cancel_event, cancel_done, terminator),
        daemon=True,
    )
    progress_thread.start()
    timeout_thread.start()
    cancel_thread.start()
    stream_error = False
    try:
        for obj in retry_runner.stream():
            if obj.get("message_type") == "status":
                stats["progress"] = restic.compute_progress(obj)
                if isinstance(obj.get("files_done"), int):
                    stats["files_processed"] = obj["files_done"]
                if isinstance(obj.get("bytes_done"), int):
                    stats["bytes_processed"] = obj["bytes_done"]
            elif obj.get("message_type") == "summary":
                stats.update(restic.parse_summary(obj))
                stats["_summary_seen"] = True
    except Exception:
        stream_error = True
    try:
        exit_code = retry_runner.wait()
    except Exception:
        exit_code = 1
        stream_error = True
    attempt_done.set()
    cancel_done.set()
    progress_thread.join(timeout=cancel_interval_seconds + 2)
    timeout_thread.join(timeout=cancel_interval_seconds + 2)
    cancel_thread.join(timeout=cancel_interval_seconds + 2)
    stats.pop("progress", None)
    return (
        retry_runner,
        stats,
        exit_code,
        timeout_event.is_set(),
        cancel_event.is_set(),
        stream_error,
        f"{recovery} Retry exited {exit_code}.",
    )
