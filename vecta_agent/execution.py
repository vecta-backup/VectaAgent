"""Unified lifecycle for filesystem and database backup jobs."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from functools import partial
from typing import Any

from vecta_agent import api, backup_stage, environment, execution_plan, execution_report, hook_stage, hooks, postgresql, repository_stage, restic
from vecta_agent.locks import RunLocks

logger = logging.getLogger("vecta_agent")


def run_pipeline_job(
    client: api.ApiClient,
    job: dict[str, Any],
    restic_env: dict[str, str],
    *,
    progress_interval_seconds: float,
    cancel_interval_seconds: float,
    max_stage_results: int,
    max_stage_message_chars: int,
    progress_reporter,
    cancel_poller,
    timeout_watchdog,
    retry_backup_after_lock,
    terminator_factory,
    validate_job_storage_config,
    resolve_job_env,
    run_prune,
    stage_result,
    timeout_message,
    destination_timeout_message,
) -> None:
    """Run database and hook-enabled jobs with a unified pipeline lifecycle."""
    job_id = job.get("job_id")
    run_id = uuid.uuid4().hex
    if not isinstance(job_id, str) or not job_id:
        logger.error("Ignoring job with invalid job_id.")
        return
    backup_type = job.get("backup_type", "files")
    legacy_file_job = execution_plan.is_legacy_file_job(job)
    metadata: dict[str, Any] = {"backup_type": backup_type if backup_type in {"files", "database"} else "files", "stdin_filename": None}
    destination = job.get("destination")
    port = job.get("port")
    lock = RunLocks(job_id, destination if isinstance(destination, str) else "")
    if not lock.acquire():
        return
    start_time = time.monotonic()
    cancel_event = threading.Event()
    done_event = threading.Event()
    terminator = terminator_factory()
    cancel_thread: threading.Thread | None = None
    stage_results: list[dict[str, Any]] = []
    pipeline_started = False
    primary_failure: tuple[int, str, str] | None = None
    snapshot_stats: dict[str, Any] = {}

    report_terminal = partial(
        execution_report.report_terminal,
        client,
        job_id,
        run_id,
        start_time=start_time,
        metadata=metadata,
        stage_results=stage_results,
        snapshot_stats=snapshot_stats,
        max_stage_results=max_stage_results,
        max_stage_message_chars=max_stage_message_chars,
    )

    try:
        client.report_status(
            job_id,
            {"job_id": job_id, "run_id": run_id, "status": "running", "stage": "restic", **metadata},
        )

        plan = execution_plan.build_execution_plan(
            client,
            job,
            restic_env,
            run_id,
            metadata,
            validate_storage_config=validate_job_storage_config,
            resolve_job_env=resolve_job_env,
        )
        if plan is None:
            return
        backup_type = plan.backup_type
        destination = plan.destination
        port = plan.port
        source = plan.source
        source_config = plan.source_config
        stdin_name = plan.stdin_name
        excludes = plan.excludes
        retention_policy = plan.retention_policy
        pre_selection = plan.pre_selection
        post_selection = plan.post_selection
        job_env = plan.job_env
        run_env = plan.run_env

        # Repository preparation is intentionally outside the user pipeline.
        terminator.set_probe(cancel_event)
        cancel_thread = threading.Thread(
            target=cancel_poller,
            args=(client, job_id, cancel_event, done_event, terminator),
            daemon=True,
        )
        cancel_thread.start()
        repository = repository_stage.prepare_repository(
            destination,
            port,
            job_env,
            cancel_event,
            job_id,
            legacy_file_job=legacy_file_job,
            destination_timeout_message=destination_timeout_message,
            redact_secrets=environment.redact_secrets,
            auth_failure_hint=environment.auth_failure_hint,
        )
        if repository.status == "cancelled":
            stage_results.append(
                stage_result("repository", "cancelled", 130, repository.duration_seconds, "Cancelled by user")
            )
            report_terminal("failed", 130, "Cancelled by user", "repository")
            logger.info("Job %s cancelled.", job_id)
            return
        if repository.status == "failed":
            stage_results.append(
                stage_result("repository", "failed", repository.exit_code, repository.duration_seconds, repository.message)
            )
            report_terminal("failed", repository.exit_code, repository.message, "repository")
            return
        if not legacy_file_job:
            stage_results.append(
                stage_result("repository", "success", 0, repository.duration_seconds)
            )

        # Pipeline begins immediately before its first hook or source stage.
        pipeline_started = True
        if pre_selection is not None:
            result, duration = hook_stage.run_hook_stage(
                client=client,
                job_id=job_id,
                run_id=run_id,
                start_time=start_time,
                metadata=metadata,
                selection=pre_selection,
                phase="pre_hook",
                cancel_event=cancel_event,
                progress_reporter=progress_reporter,
                progress_interval_seconds=progress_interval_seconds,
            )
            primary_failure = hook_stage.record_pre_hook_result(
                result, duration, stage_results, stage_result
            )

        if primary_failure is None and cancel_event.is_set():
            cancelled_stage = "source_capture" if backup_type == "database" else "restic"
            primary_failure = (130, cancelled_stage, "Cancelled by user")
            stage_results.append(stage_result(cancelled_stage, "cancelled", 130, 0, "Cancelled by user"))

        if primary_failure is None:
            primary_failure = backup_stage.run_backup_stage(
                client=client,
                job_id=job_id,
                run_id=run_id,
                backup_type=backup_type,
                source=source,
                source_config=source_config if backup_type == "database" else None,
                stdin_name=stdin_name,
                destination=destination,
                port=port,
                run_env=run_env,
                excludes=excludes,
                start_time=start_time,
                cancel_event=cancel_event,
                done_event=done_event,
                terminator=terminator,
                metadata=metadata,
                stage_results=stage_results,
                snapshot_stats=snapshot_stats,
                progress_reporter=progress_reporter,
                timeout_watchdog=timeout_watchdog,
                retry_backup_after_lock=retry_backup_after_lock,
                stage_result=stage_result,
                timeout_message=timeout_message,
                cancel_interval_seconds=cancel_interval_seconds,
                legacy_file_job=legacy_file_job,
            )

        if pipeline_started and post_selection is not None:
            cleanup_mode = primary_failure is not None
            # A post-hook after a prior failure/cancellation is bounded cleanup;
            # on success it remains cancellable like every active pipeline stage.
            result, duration = hook_stage.run_hook_stage(
                client=client,
                job_id=job_id,
                run_id=run_id,
                start_time=start_time,
                metadata=metadata,
                selection=post_selection,
                phase="post_hook",
                cancel_event=cancel_event,
                progress_reporter=progress_reporter,
                progress_interval_seconds=progress_interval_seconds,
                cleanup_mode=cleanup_mode,
            )
            post_failure = hook_stage.record_post_hook_result(
                result, duration, stage_results, stage_result
            )
            if post_failure is not None and primary_failure is None:
                primary_failure = post_failure

        prune_message: str | None = None
        pruned_ids: list[str] = []
        backup_committed = bool(snapshot_stats.get("snapshot_id")) and any(
            result["stage"] in {"restic", "source_capture"} and result["status"] == "success"
            for result in stage_results
        )
        if backup_committed and retention_policy is not None:
            prune_stage, prune_message, pruned_ids = run_prune(
                destination, port, job_env, retention_policy, cancel_event, job_id=job_id
            )
            snapshot_stats["pruned_snapshots"] = pruned_ids
            if prune_stage is not None:
                stage_results.append(prune_stage)

        execution_report.finalize_run(
            primary_failure=primary_failure,
            backup_type=backup_type,
            post_selection=post_selection,
            prune_message=prune_message,
            job_id=job_id,
            stage_results=stage_results,
            snapshot_stats=snapshot_stats,
            report_terminal=report_terminal,
            stage_result=stage_result,
        )
    except (ValueError, hooks.HookCatalogError, postgresql.PostgreSQLConfigError) as exc:
        execution_report.report_validation_failure(
            job_id,
            exc,
            stage_results,
            report_terminal,
            stage_result,
            max_stage_message_chars=max_stage_message_chars,
        )
    except Exception:
        logger.exception("Unexpected pipeline error for job %s.", job_id)
        stage_results.append(stage_result("pipeline", "failed", 1, int(time.monotonic() - start_time), "Unexpected pipeline error."))
        report_terminal("failed", 1, "Unexpected pipeline error; inspect the local agent log.", "pipeline")
    finally:
        done_event.set()
        if cancel_thread is not None:
            cancel_thread.join(timeout=cancel_interval_seconds + 2)
        lock.release()


