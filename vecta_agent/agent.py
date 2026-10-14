"""Core agent runtime: fetch jobs, run restic, report status."""

from __future__ import annotations

import logging
from typing import Any

from vecta_agent import api, config, environment, execution, hooks, monitoring, postgresql, restic, retention, status
from vecta_agent.locks import JobLock as JobLock
from vecta_agent.locks import RepositoryLock as RepositoryLock
from vecta_agent.locks import RunLocks as RunLocks
from vecta_agent.locks import _safe_lock_name as _safe_lock_name

logger = logging.getLogger("vecta_agent")

PROGRESS_INTERVAL_SECONDS = 10
CANCEL_INTERVAL_SECONDS = 10

# Hard cap on a single restic backup invocation. A hung restic process (or a
# suspended machine) would otherwise keep the job "running" forever. When the
# cap is hit the process is terminated via the same SIGTERM-then-SIGKILL path
# used for user cancellation and the run is reported as a distinct timeout
# failure. Fixed on purpose — not user-configurable for now.
MAX_BACKUP_DURATION_SECONDS = 12 * 60 * 60
MAX_STAGE_RESULTS = status.MAX_STAGE_RESULTS
MAX_STAGE_MESSAGE_CHARS = status.MAX_STAGE_MESSAGE_CHARS
STAGE_RESULT_STAGES = status.STAGE_RESULT_STAGES
STAGE_RESULT_STATUSES = status.STAGE_RESULT_STATUSES
load_restic_env = environment.load_restic_env
save_restic_env = environment.save_restic_env
_has_repo_password = environment.has_repo_password
_auth_failure_hint = environment.auth_failure_hint
_redact_secrets = environment.redact_secrets
_resolve_job_env = environment.resolve_job_env
_validate_job_storage_config = retention.validate_job_storage_config
_run_prune = retention.run_prune
_stage_result = status.stage_result
_report_cancelled = status.report_cancelled


def _progress_reporter(*args, **kwargs) -> None:
    monitoring.progress_reporter(
        *args, interval_seconds=PROGRESS_INTERVAL_SECONDS, **kwargs
    )


_JobTerminator = monitoring.JobTerminator


def _cancel_poller(*args, **kwargs) -> None:
    monitoring.cancel_poller(
        *args, interval_seconds=CANCEL_INTERVAL_SECONDS, **kwargs
    )


def _timeout_message() -> str:
    """Human-readable timeout failure reason, derived from the constant."""
    hours = MAX_BACKUP_DURATION_SECONDS / 3600
    if hours >= 1:
        return f"Backup timed out after {hours:g}h and was terminated."
    return f"Backup timed out after {MAX_BACKUP_DURATION_SECONDS:g}s and was terminated."


def _destination_timeout_message(verb: str) -> str:
    """Failure reason when a pre-backup restic command (the repo existence
    probe or repo init) outlives the probe timeout — the destination
    connection hung rather than returning a restic verdict, so this is a
    network/credentials problem, not an authentication failure.
    """
    return (
        f"Could not {verb} the backup destination — timed out after "
        f"{restic.PROBE_TIMEOUT_SECONDS}s. Check your network connection and "
        "destination credentials."
    )


def _timeout_watchdog(*args, **kwargs) -> None:
    monitoring.timeout_watchdog(
        *args,
        max_duration_seconds=MAX_BACKUP_DURATION_SECONDS,
        timeout_message=_timeout_message,
        **kwargs,
    )


def _retry_backup_after_lock(*args, **kwargs):
    """Compatibility wrapper injecting current monitoring callbacks."""
    return monitoring.retry_backup_after_lock(
        *args,
        progress_reporter=_progress_reporter,
        timeout_watchdog=_timeout_watchdog,
        cancel_poller=_cancel_poller,
        cancel_interval_seconds=CANCEL_INTERVAL_SECONDS,
        redact_secrets=_redact_secrets,
        **kwargs,
    )


def _run_files_job(
    client: api.ApiClient,
    cfg: config.Config,
    job: dict[str, Any],
    restic_env: dict[str, str],
) -> None:
    """Compatibility wrapper: filesystem jobs use the shared job lifecycle."""
    del cfg
    _run_pipeline_job(client, job, restic_env)


def _run_pipeline_job(
    client: api.ApiClient,
    job: dict[str, Any],
    restic_env: dict[str, str],
) -> None:
    """Compatibility entry point for the shared execution lifecycle."""
    execution.run_pipeline_job(
        client,
        job,
        restic_env,
        progress_interval_seconds=PROGRESS_INTERVAL_SECONDS,
        cancel_interval_seconds=CANCEL_INTERVAL_SECONDS,
        max_stage_results=MAX_STAGE_RESULTS,
        max_stage_message_chars=MAX_STAGE_MESSAGE_CHARS,
        progress_reporter=_progress_reporter,
        cancel_poller=_cancel_poller,
        timeout_watchdog=_timeout_watchdog,
        retry_backup_after_lock=_retry_backup_after_lock,
        terminator_factory=_JobTerminator,
        validate_job_storage_config=_validate_job_storage_config,
        resolve_job_env=_resolve_job_env,
        run_prune=_run_prune,
        stage_result=_stage_result,
        timeout_message=_timeout_message,
        destination_timeout_message=_destination_timeout_message,
    )


def _run_single_job(
    client: api.ApiClient,
    cfg: config.Config,
    job: dict[str, Any],
    restic_env: dict[str, str],
) -> None:
    """Run every job through the shared lifecycle, regardless of source type."""
    del cfg  # Kept in the signature for compatibility with existing callers.
    _run_pipeline_job(client, job, restic_env)


def capability_report() -> dict[str, Any]:
    """Build the safe feature and hook metadata report shared by run and CLI."""
    features: list[str] = []
    checks: dict[str, dict[str, str]] = {}
    try:
        local_hooks = hooks.load_catalog()
        hook_metadata = hooks.capability_metadata(local_hooks)
        features.append("hook_catalog_v1")
        checks["hook_catalog_v1"] = {"status": "available"}
    except hooks.HookCatalogError as exc:
        logger.error("Local hook catalog is invalid; hook capabilities are disabled: %s", exc)
        hook_metadata = []
        checks["hook_catalog_v1"] = {"status": "unavailable", "reason": "hook_catalog_invalid"}
    postgresql_issue = postgresql.postgresql_support_issue()
    if postgresql_issue is None:
        features.append("postgresql_stdin_backup")
        checks["postgresql_stdin_backup"] = {"status": "available"}
    else:
        checks["postgresql_stdin_backup"] = {"status": "unavailable", "reason": postgresql_issue}
    return {"features": features, "hooks": hook_metadata, "checks": checks}


def run_agent() -> None:
    """Single-pass agent loop (spec §6)."""
    cfg = config.load()
    client = api.ApiClient(cfg.agent_id, cfg.api_key)

    try:
        me = client.me()
        logger.info("Authenticated as %s (%s).", me.get("name"), cfg.agent_id)
    except api.AgentAuthError as exc:
        logger.error("Authentication failed: %s", exc)
        raise

    report_capabilities = getattr(client, "report_capabilities", None)
    if callable(report_capabilities):
        try:
            report_capabilities(capability_report())
        except api.AgentAuthError:
            raise
        except Exception as exc:
            # Older backend deployments may not yet implement capability
            # negotiation; filesystem jobs remain usable during rollout.
            logger.warning("Could not report local capabilities: %s", exc)

    try:
        jobs = client.fetch_jobs()
    except api.AgentAuthError as exc:
        logger.error("Authentication failed while fetching jobs: %s", exc)
        raise
    except Exception as exc:  # pragma: no cover
        logger.error("Failed to fetch jobs: %s", exc)
        return

    if not jobs:
        logger.info("No jobs due.")
        return

    config_dir = config._config_dir()
    restic_env = load_restic_env(config_dir)

    for job in jobs:
        try:
            _run_single_job(client, cfg, job, restic_env)
        except api.AgentAuthError as exc:
            logger.error("Authentication failed while running job: %s", exc)
            raise
        except Exception as exc:  # pragma: no cover
            logger.exception("Unexpected error running job %s: %s", job.get("job_id"), exc)
