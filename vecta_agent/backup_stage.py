"""Restic invocation, streaming, recovery, and result normalization."""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable

from vecta_agent import environment, postgresql, restic

logger = logging.getLogger("vecta_agent")


def run_backup_stage(
    *,
    client,
    job_id: str,
    run_id: str,
    backup_type: str,
    source: str | None,
    source_config: dict[str, Any] | None,
    stdin_name: str | None,
    destination: str,
    port: int | None,
    run_env: dict[str, str],
    excludes: list[str],
    start_time: float,
    cancel_event: threading.Event,
    done_event: threading.Event,
    terminator,
    metadata: dict[str, Any],
    stage_results: list[dict[str, Any]],
    snapshot_stats: dict[str, Any],
    progress_reporter: Callable[..., None],
    timeout_watchdog: Callable[..., None],
    retry_backup_after_lock: Callable[..., Any],
    stage_result: Callable[..., dict[str, Any]],
    timeout_message: Callable[[], str],
    cancel_interval_seconds: float,
    legacy_file_job: bool,
) -> tuple[int, str, str] | None:
    """Run one source through the shared Restic runner and return any failure."""
    stage = "source_capture" if backup_type == "database" else "restic"
    stats: dict[str, Any] = {"stage": stage}
    client.report_status(
        job_id,
        {"job_id": job_id, "run_id": run_id, "status": "running", "stage": stage, **metadata},
    )
    timeout_event = threading.Event()
    if backup_type == "database":
        runner = restic.ResticRunner(
            None, destination, port=port, env=run_env,
            stdin_filename=stdin_name,
            source_command=postgresql.build_pg_dump_argv(source_config),
            unset_env=postgresql.PG_ENV_UNSET,
            job_id=job_id,
        )
    else:
        runner_args = {"exclude_patterns": excludes} if excludes else {}
        runner = restic.ResticRunner(source, destination, port=port, env=run_env, job_id=job_id, **runner_args)
    try:
        runner.start()
    except FileNotFoundError:
        message = (
            "restic executable not found on PATH; install restic to run backups"
            if legacy_file_job
            else "Restic executable not found on PATH."
        )
        stage_results.append(stage_result(stage, "failed", 127, 0, message))
        return 127, stage, message
    except Exception:
        message = "Backup process could not be started safely."
        stage_results.append(stage_result(stage, "failed", 1, 0, message))
        return 1, stage, message

    terminator.set_runner(runner)
    if cancel_event.is_set():
        runner.terminate()
    progress_thread = threading.Thread(
        target=progress_reporter,
        args=(client, job_id, run_id, start_time, stats, done_event, metadata),
        daemon=True,
    )
    timeout_thread = threading.Thread(
        target=timeout_watchdog,
        args=(runner, timeout_event, done_event),
        daemon=True,
    )
    progress_thread.start()
    timeout_thread.start()
    stage_started = time.monotonic()
    stream_error = False
    try:
        for obj in runner.stream():
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
    finally:
        try:
            exit_code = runner.wait()
        except Exception:
            exit_code = 1
            stream_error = True
        terminator.set_probe(cancel_event)
        done_event.set()
        progress_thread.join(timeout=cancel_interval_seconds + 2)
        timeout_thread.join(timeout=cancel_interval_seconds + 2)
    duration = int(time.monotonic() - stage_started)
    recovery_note = None
    if exit_code != 0 and not stream_error and not timeout_event.is_set() and not cancel_event.is_set():
        def make_runner():
            if backup_type == "database":
                return restic.ResticRunner(
                    None, destination, port=port, env=run_env,
                    stdin_filename=stdin_name,
                    source_command=postgresql.build_pg_dump_argv(source_config),
                    unset_env=postgresql.PG_ENV_UNSET,
                    job_id=job_id,
                )
            return restic.ResticRunner(
                source, destination, port=port, env=run_env,
                job_id=job_id,
                **({"exclude_patterns": excludes} if excludes else {}),
            )

        retry = retry_backup_after_lock(
            make_runner, runner.stderr_tail(), destination, port, run_env,
            client, job_id, run_id, start_time, cancel_event, terminator, metadata,
        )
        retry_runner, retry_stats, retry_code, retry_timeout, retry_cancelled, retry_stream_error, recovery_note = retry
        if retry_runner is not None:
            runner = retry_runner
            stats.clear()
            stats.update(retry_stats)
            exit_code = retry_code if retry_code is not None else 1
            stream_error = retry_stream_error
            if retry_timeout:
                timeout_event.set()
            if retry_cancelled:
                cancel_event.set()
            snapshot_stats.clear()
        if recovery_note:
            logger.info("Job %s %s", job_id, recovery_note)
    snapshot_stats.update({key: value for key, value in stats.items() if not key.startswith("_") and key not in {"stage", "progress"}})
    if stats.get("_summary_seen"):
        snapshot_stats["_summary_seen"] = True
    if stream_error:
        message = "Restic progress stream failed; backup outcome is unverified."
        stage_results.append(stage_result("restic", "failed", exit_code if exit_code else 1, duration, "Backup outcome could not be verified."))
        snapshot_stats.pop("snapshot_id", None)
        return exit_code if exit_code else 1, stage, message
    if cancel_event.is_set():
        stage_results.append(stage_result(stage, "cancelled", 130, duration, "Cancelled by user"))
        return 130, stage, "Cancelled by user"
    if timeout_event.is_set():
        message = timeout_message()
        stage_results.append(stage_result(stage, "failed", 124, duration, message))
        return 124, stage, message
    if exit_code == 0:
        safe_recovery = environment.redact_secrets(recovery_note, run_env) if recovery_note else None
        stage_results.append(stage_result(stage, "success", 0, duration, safe_recovery))
        if backup_type == "database":
            stage_results.append(stage_result("restic", "success", 0, duration, safe_recovery))
        return None

    stderr = runner.stderr_tail()
    producer_status = re.search(r"failed:\s*exit status\s+(\d+)", stderr, re.IGNORECASE)
    if backup_type == "database" and producer_status:
        stage_results.append(stage_result("source_capture", "failed", int(producer_status.group(1)), duration, "pg_dump exited unsuccessfully; Restic did not create a snapshot."))
        stage_results.append(stage_result("restic", "failed", exit_code, duration, "Restic rejected the failed pg_dump stream."))
        primary_failure = (exit_code, "source_capture", "pg_dump failed; Restic did not create a snapshot.")
    else:
        message = environment.redact_secrets(
            (stderr or f"Restic exited with code {exit_code}")
            + (f" {recovery_note}" if recovery_note else "")
            + environment.auth_failure_hint(stderr, job_id),
            run_env,
        )
        stage_results.append(stage_result("restic", "failed", exit_code, duration, message))
        primary_failure = (exit_code, "restic", message)
    snapshot_stats.pop("snapshot_id", None)
    return primary_failure
