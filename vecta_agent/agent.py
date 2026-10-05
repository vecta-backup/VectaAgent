"""Core agent runtime: fetch jobs, run restic, report status."""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from vecta_agent import api, config, credentials, hooks, postgresql, restic

logger = logging.getLogger("vecta_agent")

PROGRESS_INTERVAL_SECONDS = 10
CANCEL_INTERVAL_SECONDS = 10

# Hard cap on a single restic backup invocation. A hung restic process (or a
# suspended machine) would otherwise keep the job "running" forever. When the
# cap is hit the process is terminated via the same SIGTERM-then-SIGKILL path
# used for user cancellation and the run is reported as a distinct timeout
# failure. Fixed on purpose — not user-configurable for now.
MAX_BACKUP_DURATION_SECONDS = 12 * 60 * 60
MAX_STAGE_RESULTS = 8
MAX_STAGE_MESSAGE_CHARS = 500
STAGE_RESULT_STAGES = frozenset({
    "repository", "validation", "pipeline", "pre_hook", "source_capture",
    "database_dump", "restic", "post_hook",
})
STAGE_RESULT_STATUSES = frozenset({"success", "failed", "skipped", "cancelled", "warning"})
_SECRET_ENV_MARKERS = ("PASSWORD", "SECRET", "TOKEN", "ACCESS_KEY", "ACCOUNT_KEY", "CREDENTIAL", "AUTH")

# Substrings (case-insensitive) in restic's stderr that indicate an
# authentication failure rather than e.g. a network problem. The hint is
# appended to failure reports, so a false positive only adds advice.
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


try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore


_SAFE_LOCK_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def _safe_lock_name(job_id: str) -> str:
    return _SAFE_LOCK_CHARS.sub("_", job_id)


class JobLock:
    """Best-effort per-job lockfile. Uses fcntl on Linux, no-op elsewhere."""

    def __init__(self, job_id: str) -> None:
        self.path = Path(tempfile.gettempdir()) / f"vecta-{_safe_lock_name(job_id)}.lock"
        self._fd: int | None = None

    def acquire(self) -> bool:
        try:
            self._fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR)
        except OSError:
            logger.warning("Cannot create lockfile %s; skipping job.", self.path)
            return False

        if fcntl is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError):
                logger.warning(
                    "Another process appears to be running job %s; skipping.",
                    self.path.stem.replace("vecta-", ""),
                )
                os.close(self._fd)
                self._fd = None
                return False
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
        finally:
            self._fd = None

    def __enter__(self) -> "JobLock":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        self.release()


def load_restic_env(config_dir: Path) -> dict[str, str]:
    """Load optional key=value restic.env file."""
    env_path = config_dir / "restic.env"
    if not env_path.exists():
        return {}

    result: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
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
    """Write key=value entries into restic.env, preserving existing content."""
    env_path = config_dir / "restic.env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    for key, value in entries.items():
        marker = f"{key}="
        for i, line in enumerate(lines):
            if line.strip().startswith(marker):
                lines[i] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(env_path, 0o600)
    return env_path


def _has_repo_password(restic_env: dict[str, str]) -> bool:
    """Whether restic can obtain a repo password from env or restic.env."""
    merged = {**os.environ, **restic_env}
    return bool(
        merged.get("RESTIC_PASSWORD")
        or merged.get("RESTIC_PASSWORD_FILE")
        or merged.get("RESTIC_PASSWORD_COMMAND")
    )


def _auth_failure_hint(stderr: str, job_id: str) -> str:
    """Hint appended to failure reports when restic's stderr looks like an
    authentication failure (missing/wrong stored credentials, SSH keys, ...).
    """
    low = stderr.lower()
    if any(marker in low for marker in _AUTH_ERROR_MARKERS):
        return (
            " This looks like an authentication failure. Run "
            f"'vecta-agent setup {job_id}' on this machine to configure the "
            "credentials for this destination."
        )
    return ""


def _redact_secrets(message: str, env: dict[str, str] | None) -> str:
    """Remove known local secret values before logs/status or stage summaries."""
    result = message
    for key, value in (env or {}).items():
        if value and any(marker in key.upper() for marker in _SECRET_ENV_MARKERS):
            result = result.replace(value, "[REDACTED]")
    return result[:MAX_STAGE_MESSAGE_CHARS]


def _progress_reporter(
    client: api.ApiClient,
    job_id: str,
    run_id: str,
    start_time: float,
    stats: dict[str, Any],
    done_event: threading.Event,
    metadata: dict[str, Any] | None = None,
) -> None:
    """POST running status every ~10 seconds while a backup is active."""
    while not done_event.wait(PROGRESS_INTERVAL_SECONDS):
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


class _JobTerminator:
    """Terminates whichever restic invocation is currently active for a job.

    During the repo probe/init phase the active command is a one-shot
    `run_restic` call that watches a cancel event; during the backup phase it
    is the ResticRunner subprocess. The cancel poller calls terminate()
    without needing to know which phase is active.
    """

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


def _cancel_poller(
    client: api.ApiClient,
    job_id: str,
    cancel_event: threading.Event,
    done_event: threading.Event,
    terminator: _JobTerminator,
) -> None:
    """Poll backend cancel flag every ~10 seconds and terminate the active
    restic invocation (repo probe, init, or backup) if set."""
    while not done_event.wait(CANCEL_INTERVAL_SECONDS):
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


def _timeout_watchdog(
    runner: restic.ResticRunner,
    timeout_event: threading.Event,
    done_event: threading.Event,
) -> None:
    """Terminate restic if a single backup invocation exceeds the max duration.

    Reuses the cancellation terminate path (SIGTERM, then SIGKILL); the main
    loop notices via timeout_event and reports a distinct timeout failure.
    """
    if done_event.wait(MAX_BACKUP_DURATION_SECONDS):
        return
    timeout_event.set()
    runner.terminate()
    logger.warning("Job exceeded the maximum duration; terminating restic. %s", _timeout_message())


def _report_cancelled(
    client: api.ApiClient,
    job_id: str,
    run_id: str,
    start_time: float,
    metadata: dict[str, Any] | None = None,
    stage_results: list[dict[str, Any]] | None = None,
    stage: str = "restic",
) -> None:
    """Report the standard user-cancellation failure for a job."""
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
            "duration_seconds": int(time.monotonic() - start_time),
            "stage_results": (stage_results or [_stage_result(stage, "cancelled", 130, int(time.monotonic() - start_time), "Cancelled by user")])[-MAX_STAGE_RESULTS:],
        },
    )
    logger.info("Job %s cancelled.", job_id)


def _resolve_job_env(
    client: api.ApiClient,
    job_id: str,
    job: dict[str, Any],
    restic_env: dict[str, str],
    run_id: str,
    status_metadata: dict[str, Any] | None = None,
) -> dict[str, str] | None:
    """Merge the global restic env with the destination's stored credentials.

    Stored credentials (keyed by destination fingerprint) win over the global
    env. Destinations that need nothing stored — global restic.env setups,
    SFTP with SSH keys, cloud instance roles — run without them. Returns None
    after reporting a failure when the credentials file is malformed.
    """
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
                "stage_results": [_stage_result("validation", "failed", 1, None, "Invalid local credentials file.")],
            },
        )
        logger.error("Job %s failed: invalid local credentials file: %s", job_id, exc)
        return None
    if stored:
        job_env.update(stored)
    return job_env


def _run_files_job(
    client: api.ApiClient,
    cfg: config.Config,
    job: dict[str, Any],
    restic_env: dict[str, str],
) -> None:
    job_id = job["job_id"]
    source = job["source"]
    destination = job["destination"]
    port = job.get("port")

    # Identifies this invocation end-to-end: the backend keys the in-flight
    # "running" log row (and its final terminal update) on it, so two runs of
    # the same job can never be conflated into one log entry.
    run_id = uuid.uuid4().hex

    lock = JobLock(job_id)
    if not lock.acquire():
        return

    start_time = time.monotonic()
    cancel_event = threading.Event()
    done_event = threading.Event()
    terminator = _JobTerminator()
    cancel_thread: threading.Thread | None = None
    status_metadata = {"backup_type": "files", "stdin_filename": None}

    try:
        client.report_status(
            job_id,
            {"job_id": job_id, "run_id": run_id, "status": "running", "stage": "restic", **status_metadata},
        )
        logger.info("Starting job %s: %s -> %s", job_id, source, destination)

        job_env = _resolve_job_env(client, job_id, job, restic_env, run_id, {"backup_type": "files", "stdin_filename": None})
        if job_env is None:
            return

        if not _has_repo_password(job_env):
            client.report_status(
                job_id,
                {
                    "job_id": job_id,
                    "run_id": run_id,
                    "status": "failed",
                    **status_metadata,
                    "exit_code": 1,
                    "message": (
                        "No repository password configured for this destination. "
                        f"Run 'vecta-agent setup {job_id}' on this machine to "
                        "configure it."
                    ),
                    "stage_results": [_stage_result("validation", "failed", 1, None, "Repository credentials are not configured.")],
                },
            )
            logger.error("Job %s failed: no repository password configured.", job_id)
            return

        # The cancel poller starts before the repo probe: a hung destination
        # connection (wrong S3/SFTP credentials, firewalled host) would
        # otherwise be uncancellable until the probe timeout fires.
        terminator.set_probe(cancel_event)
        cancel_thread = threading.Thread(
            target=_cancel_poller,
            args=(client, job_id, cancel_event, done_event, terminator),
            daemon=True,
        )
        cancel_thread.start()

        try:
            repo_check = restic.check_repo(
                destination, env=job_env, port=port, cancel_event=cancel_event
            )
            if repo_check.cancelled or cancel_event.is_set():
                _report_cancelled(client, job_id, run_id, start_time, status_metadata, stage="restic")
                return
            if repo_check.exists is False:
                logger.info("Repository at %s not found; initializing.", destination)
                init_result = restic.init_repo(
                    destination, env=job_env, port=port, cancel_event=cancel_event
                )
                if init_result.cancelled or cancel_event.is_set():
                    _report_cancelled(client, job_id, run_id, start_time, status_metadata, stage="restic")
                    return
                if init_result.exit_code != 0:
                    if init_result.timed_out:
                        message = _destination_timeout_message("initialize")
                    else:
                        message = _redact_secrets((
                            init_result.stderr_tail or "Failed to initialize repository."
                        ) + _auth_failure_hint(init_result.stderr_tail, job_id), job_env)
                    client.report_status(
                        job_id,
                        {
                            "job_id": job_id,
                            "run_id": run_id,
                            "status": "failed",
                            **status_metadata,
                            "stage": "restic",
                            "exit_code": init_result.exit_code,
                            "message": message,
                            "stage_results": [_stage_result("repository", "failed", init_result.exit_code, None, message)],
                        },
                    )
                    logger.error(
                        "Job %s failed: could not initialize repository at %s: %s",
                        job_id,
                        destination,
                        _redact_secrets(init_result.stderr_tail, job_env),
                    )
                    return
                logger.info("Repository at %s initialized.", destination)
            elif repo_check.exists is None:
                if repo_check.timed_out:
                    message = _destination_timeout_message("verify")
                else:
                    message = _redact_secrets((
                        repo_check.stderr_tail
                        or "Could not verify repository at destination."
                    ) + _auth_failure_hint(repo_check.stderr_tail, job_id), job_env)
                client.report_status(
                    job_id,
                    {
                        "job_id": job_id,
                        "run_id": run_id,
                        "status": "failed",
                        **status_metadata,
                        "stage": "restic",
                        "exit_code": 124 if repo_check.timed_out else 1,
                        "message": message,
                        "stage_results": [_stage_result("repository", "failed", 124 if repo_check.timed_out else 1, None, message)],
                    },
                )
                logger.error(
                    "Job %s failed: could not verify repository at %s: %s",
                    job_id,
                    destination,
                    _redact_secrets(repo_check.stderr_tail, job_env),
                )
                return
        except FileNotFoundError:
            client.report_status(
                job_id,
                {
                    "job_id": job_id,
                    "run_id": run_id,
                    "status": "failed",
                    **status_metadata,
                    "stage": "restic",
                    "exit_code": 127,
                    "message": "restic binary not found in PATH",
                    "stage_results": [_stage_result("repository", "failed", 127, None, "Restic executable not found on PATH.")],
                },
            )
            logger.error("Job %s failed: restic binary not found in PATH.", job_id)
            return

        runner = restic.ResticRunner(source, destination, port=port, env=job_env)
        try:
            runner.start()
        except FileNotFoundError:
            client.report_status(
                job_id,
                {
                    "job_id": job_id,
                    "run_id": run_id,
                    "status": "failed",
                    **status_metadata,
                    "stage": "restic",
                    "exit_code": 127,
                    "message": "restic executable not found on PATH; install restic to run backups",
                    "stage_results": [_stage_result("restic", "failed", 127, None, "Restic executable not found on PATH.")],
                },
            )
            logger.error("Job %s failed: restic executable not found on PATH.", job_id)
            return

        terminator.set_runner(runner)
        if cancel_event.is_set():
            # Cancellation landed between the probe phase and backup start;
            # the poller has already stopped, so terminate the backup process
            # here and let the cancel check below report it.
            runner.terminate()

        stats: dict[str, Any] = {}
        stats["stage"] = "restic"
        timeout_event = threading.Event()

        progress_thread = threading.Thread(
            target=_progress_reporter,
            args=(client, job_id, run_id, start_time, stats, done_event, status_metadata),
            daemon=True,
        )
        timeout_thread = threading.Thread(
            target=_timeout_watchdog,
            args=(runner, timeout_event, done_event),
            daemon=True,
        )
        progress_thread.start()
        timeout_thread.start()

        try:
            for obj in runner.stream():
                msg_type = obj.get("message_type")
                if msg_type == "status":
                    stats["progress"] = restic.compute_progress(obj)
                    stats["files_processed"] = obj.get("files_done")
                    stats["bytes_processed"] = obj.get("bytes_done")
                    stats["transferred_bytes"] = obj.get("bytes_done")
                elif msg_type == "summary":
                    summary = restic.parse_summary(obj)
                    stats.update(summary)
                    stats["_summary_seen"] = True
        finally:
            exit_code = runner.wait()
            terminator.set_probe(cancel_event)
            done_event.set()
            progress_thread.join(timeout=CANCEL_INTERVAL_SECONDS + 2)
            timeout_thread.join(timeout=CANCEL_INTERVAL_SECONDS + 2)

        duration = int(time.monotonic() - start_time)

        if cancel_event.is_set():
            _report_cancelled(client, job_id, run_id, start_time, status_metadata)
            return

        if timeout_event.is_set():
            payload = {
                "job_id": job_id,
                "run_id": run_id,
                "status": "failed",
                **status_metadata,
                "stage": "restic",
                "exit_code": 124,
                "message": _timeout_message(),
                "duration_seconds": duration,
                "stage_results": [{"stage": "restic", "status": "failed", "exit_code": 124, "duration_seconds": duration, "message": _timeout_message()}],
            }
            client.report_status(job_id, payload)
            logger.error("Job %s %s", job_id, _timeout_message())
            return

        if exit_code == 0:
            # Zero files scanned means the source path was empty, missing, or
            # matched nothing. Zero NEW files with a nonzero scan count is
            # normal dedup behavior and must NOT be flagged — hence this
            # checks only total files processed, and only when restic's
            # summary event actually reported it.
            zero_files = bool(stats.get("_summary_seen")) and stats.get("files_processed") == 0
            payload = {
                "job_id": job_id,
                "run_id": run_id,
                "status": "warning" if zero_files else "success",
                **status_metadata,
                "stage": "restic",
                "exit_code": 0,
                "message": (
                    "Backup completed but no files were processed. The source "
                    "path may be empty, missing, or wrong — check the job's "
                    "source path."
                    if zero_files
                    else "Backup completed"
                ),
                "duration_seconds": duration,
                "snapshot_id": stats.get("snapshot_id"),
                "files_processed": stats.get("files_processed"),
                "bytes_processed": stats.get("bytes_processed"),
                "transferred_bytes": stats.get("transferred_bytes"),
                "stage_results": [{"stage": "restic", "status": "success", "exit_code": 0, "duration_seconds": duration, "message": None}],
            }
            client.report_status(job_id, payload)
            if zero_files:
                logger.warning("Job %s completed with no files processed.", job_id)
            else:
                logger.info("Job %s succeeded (snapshot %s).", job_id, stats.get("snapshot_id"))
        else:
            payload = {
                "job_id": job_id,
                "run_id": run_id,
                "status": "failed",
                **status_metadata,
                "stage": "restic",
                "exit_code": exit_code,
                "message": _redact_secrets((runner.stderr_tail() or f"restic exited with code {exit_code}")
                + _auth_failure_hint(runner.stderr_tail(), job_id), job_env),
                "duration_seconds": duration,
                "stage_results": [{"stage": "restic", "status": "failed", "exit_code": exit_code, "duration_seconds": duration, "message": _redact_secrets(runner.stderr_tail() or f"restic exited with code {exit_code}", job_env)}],
            }
            client.report_status(job_id, payload)
            logger.error("Job %s failed with exit code %s.", job_id, exit_code)
    finally:
        done_event.set()
        if cancel_thread is not None:
            cancel_thread.join(timeout=CANCEL_INTERVAL_SECONDS + 2)
        lock.release()


def _stage_result(
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


def _run_pipeline_job(
    client: api.ApiClient,
    job: dict[str, Any],
    restic_env: dict[str, str],
) -> None:
    """Run database and hook-enabled jobs with a unified pipeline lifecycle."""
    job_id = job.get("job_id")
    run_id = uuid.uuid4().hex
    if not isinstance(job_id, str) or not job_id:
        logger.error("Ignoring job with invalid job_id.")
        return
    backup_type = job.get("backup_type", "files")
    stdin_name: str | None = None
    metadata: dict[str, Any] = {"backup_type": backup_type if backup_type in {"files", "database"} else "files", "stdin_filename": None}
    destination = job.get("destination")
    port = job.get("port")
    lock = JobLock(job_id)
    if not lock.acquire():
        return
    start_time = time.monotonic()
    cancel_event = threading.Event()
    done_event = threading.Event()
    terminator = _JobTerminator()
    cancel_thread: threading.Thread | None = None
    stage_results: list[dict[str, Any]] = []
    pipeline_started = False
    primary_failure: tuple[int, str, str] | None = None
    snapshot_stats: dict[str, Any] = {}

    def report_terminal(status: str, code: int | None, message: str, stage: str) -> None:
        payload: dict[str, Any] = {
            "job_id": job_id,
            "run_id": run_id,
            "status": status,
            "exit_code": code,
            "message": message[:MAX_STAGE_MESSAGE_CHARS],
            "duration_seconds": int(time.monotonic() - start_time),
            **metadata,
            "stage_results": stage_results[-MAX_STAGE_RESULTS:],
        }
        if stage in {"pre_hook", "source_capture", "database_dump", "restic", "post_hook"}:
            payload["stage"] = stage
        elif stage == "repository":
            payload["stage"] = "restic"
        payload.update({key: snapshot_stats.get(key) for key in ("snapshot_id", "files_processed", "bytes_processed", "transferred_bytes") if key in snapshot_stats})
        client.report_status(job_id, payload)

    try:
        client.report_status(
            job_id,
            {"job_id": job_id, "run_id": run_id, "status": "running", "stage": "restic", **metadata},
        )

        if backup_type not in {"files", "database"}:
            raise ValueError("backup_type must be files or database.")
        if not isinstance(destination, str) or not destination:
            raise ValueError("destination is required.")
        if backup_type == "files":
            source = job.get("source")
            if not isinstance(source, str) or not source or job.get("source_config") is not None:
                raise ValueError("Filesystem jobs require a source path and no source_config.")
        else:
            if job.get("source") is not None:
                raise ValueError("Database jobs cannot include a filesystem source.")
            source_config = postgresql.validate_source_config(job.get("source_config"))
            stdin_name = postgresql.stdin_filename(source_config)
            metadata["stdin_filename"] = stdin_name
            source = None

        source_description = (
            source
            if backup_type == "files"
            else (
                f"PostgreSQL {source_config['host']}:{source_config['port']}"
                f"/{source_config['database']}"
            )
        )
        logger.info("Starting job %s: %s -> %s", job_id, source_description, destination)

        catalog = hooks.load_catalog()
        pre_selection = hooks.validate_selection(job.get("pre_hook"), catalog, "pre")
        post_selection = hooks.validate_selection(job.get("post_hook"), catalog, "post")
        job_env = _resolve_job_env(client, job_id, job, restic_env, run_id, metadata)
        if job_env is None:
            return
        run_env = dict(job_env)
        if backup_type == "database":
            if not postgresql.probe_postgresql_support():
                raise postgresql.PostgreSQLConfigError(
                    "PostgreSQL backups require Restic with stdin-from-command support and pg_dump 9.0 or newer."
                )
            run_env.update(postgresql.child_environment(source_config))
        if not _has_repo_password(job_env):
            raise ValueError(
                "No repository password configured for this destination. "
                f"Run 'vecta-agent setup {job_id}' on this machine to configure it."
            )

        # Repository preparation is intentionally outside the user pipeline.
        terminator.set_probe(cancel_event)
        cancel_thread = threading.Thread(
            target=_cancel_poller,
            args=(client, job_id, cancel_event, done_event, terminator),
            daemon=True,
        )
        cancel_thread.start()
        repo_started = time.monotonic()
        try:
            check = restic.check_repo(destination, env=job_env, port=port, cancel_event=cancel_event)
            if check.cancelled or cancel_event.is_set():
                stage_results.append(_stage_result("repository", "cancelled", 130, int(time.monotonic() - repo_started), "Cancelled by user"))
                report_terminal("failed", 130, "Cancelled by user", "repository")
                logger.info("Job %s cancelled.", job_id)
                return
            if check.exists is False:
                logger.info("Repository at %s not found; initializing.", destination)
                init = restic.init_repo(destination, env=job_env, port=port, cancel_event=cancel_event)
                if init.cancelled or cancel_event.is_set():
                    stage_results.append(_stage_result("repository", "cancelled", 130, int(time.monotonic() - repo_started), "Cancelled by user"))
                    report_terminal("failed", 130, "Cancelled by user", "repository")
                    logger.info("Job %s cancelled.", job_id)
                    return
                if init.exit_code != 0:
                    message = _destination_timeout_message("initialize") if init.timed_out else _redact_secrets((init.stderr_tail or "Failed to initialize repository.") + _auth_failure_hint(init.stderr_tail, job_id), job_env)
                    stage_results.append(_stage_result("repository", "failed", init.exit_code, int(time.monotonic() - repo_started), message))
                    report_terminal("failed", init.exit_code, message, "repository")
                    logger.error(
                        "Job %s failed: could not initialize repository at %s: %s",
                        job_id,
                        destination,
                        message,
                    )
                    return
                logger.info("Repository at %s initialized.", destination)
            elif check.exists is None:
                message = _destination_timeout_message("verify") if check.timed_out else _redact_secrets((check.stderr_tail or "Could not verify repository at destination.") + _auth_failure_hint(check.stderr_tail, job_id), job_env)
                code = 124 if check.timed_out else 1
                stage_results.append(_stage_result("repository", "failed", code, int(time.monotonic() - repo_started), message))
                report_terminal("failed", code, message, "repository")
                logger.error(
                    "Job %s failed: could not verify repository at %s: %s",
                    job_id,
                    destination,
                    message,
                )
                return
        except FileNotFoundError:
            message = "restic executable not found on PATH; install Restic to run backups."
            stage_results.append(_stage_result("repository", "failed", 127, int(time.monotonic() - repo_started), message))
            report_terminal("failed", 127, message, "repository")
            logger.error("Job %s failed: restic executable not found on PATH.", job_id)
            return
        stage_results.append(_stage_result("repository", "success", 0, int(time.monotonic() - repo_started)))

        # Pipeline begins immediately before its first hook or source stage.
        pipeline_started = True
        if pre_selection is not None:
            definition, parameters = pre_selection
            stage_started = time.monotonic()
            hook_done = threading.Event()
            hook_stats = {"stage": "pre_hook"}
            client.report_status(job_id, {"job_id": job_id, "run_id": run_id, "status": "running", "stage": "pre_hook", **metadata})
            hook_progress = threading.Thread(
                target=_progress_reporter,
                args=(client, job_id, run_id, start_time, hook_stats, hook_done, metadata),
                daemon=True,
            )
            hook_progress.start()
            try:
                result = hooks.execute_hook(definition, parameters, cancel_event=cancel_event)
            except Exception:
                result = None
                primary_failure = (1, "pre_hook", "Pre-hook could not be started safely.")
            finally:
                hook_done.set()
                hook_progress.join(timeout=PROGRESS_INTERVAL_SECONDS + 2)
            duration = int(time.monotonic() - stage_started)
            if result is None:
                stage_results.append(_stage_result("pre_hook", "failed", 1, duration, "Pre-hook could not be started safely."))
            else:
                outcome = "cancelled" if result.cancelled else "failed" if result.exit_code or result.timed_out else "success"
                message = "Cancelled by user" if result.cancelled else "Pre-hook timed out." if result.timed_out else f"Pre-hook failed with exit code {result.exit_code}." if result.exit_code else None
                stage_results.append(_stage_result("pre_hook", outcome, result.exit_code, duration, message))
                if outcome != "success":
                    primary_failure = (130 if result.cancelled else 124 if result.timed_out else result.exit_code, "pre_hook", message or "Pre-hook failed.")

        if primary_failure is None and cancel_event.is_set():
            cancelled_stage = "source_capture" if backup_type == "database" else "restic"
            primary_failure = (130, cancelled_stage, "Cancelled by user")
            stage_results.append(_stage_result(cancelled_stage, "cancelled", 130, 0, "Cancelled by user"))

        if primary_failure is None:
            stage = "source_capture" if backup_type == "database" else "restic"
            stats: dict[str, Any] = {"stage": stage}
            client.report_status(job_id, {"job_id": job_id, "run_id": run_id, "status": "running", "stage": stage, **metadata})
            timeout_event = threading.Event()
            if backup_type == "database":
                runner = restic.ResticRunner(
                    None, destination, port=port, env=run_env,
                    stdin_filename=stdin_name,
                    source_command=postgresql.build_pg_dump_argv(source_config),
                    unset_env=postgresql.PG_ENV_UNSET,
                )
            else:
                runner = restic.ResticRunner(source, destination, port=port, env=run_env)
            try:
                runner.start()
            except FileNotFoundError:
                primary_failure = (127, stage, "Restic executable not found on PATH.")
                stage_results.append(_stage_result(stage, "failed", 127, 0, "Restic executable not found on PATH."))
            except Exception:
                primary_failure = (1, stage, "Backup process could not be started safely.")
                stage_results.append(_stage_result(stage, "failed", 1, 0, "Backup process could not be started safely."))
            if primary_failure is None:
                terminator.set_runner(runner)
                if cancel_event.is_set():
                    runner.terminate()
                progress_thread = threading.Thread(
                    target=_progress_reporter,
                    args=(client, job_id, run_id, start_time, stats, done_event, metadata),
                    daemon=True,
                )
                timeout_thread = threading.Thread(target=_timeout_watchdog, args=(runner, timeout_event, done_event), daemon=True)
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
                    progress_thread.join(timeout=CANCEL_INTERVAL_SECONDS + 2)
                    timeout_thread.join(timeout=CANCEL_INTERVAL_SECONDS + 2)
                duration = int(time.monotonic() - stage_started)
                snapshot_stats.update({key: value for key, value in stats.items() if not key.startswith("_") and key not in {"stage", "progress"}})
                if stream_error:
                    primary_failure = (exit_code if exit_code else 1, stage, "Restic progress stream failed; backup outcome is unverified.")
                    stage_results.append(_stage_result("restic", "failed", exit_code if exit_code else 1, duration, "Backup outcome could not be verified."))
                    snapshot_stats.pop("snapshot_id", None)
                elif cancel_event.is_set():
                    primary_failure = (130, stage, "Cancelled by user")
                    stage_results.append(_stage_result(stage, "cancelled", 130, duration, "Cancelled by user"))
                elif timeout_event.is_set():
                    primary_failure = (124, stage, _timeout_message())
                    stage_results.append(_stage_result(stage, "failed", 124, duration, _timeout_message()))
                elif exit_code == 0:
                    stage_results.append(_stage_result(stage, "success", 0, duration))
                    if backup_type == "database":
                        stage_results.append(_stage_result("restic", "success", 0, duration))
                else:
                    stderr = runner.stderr_tail()
                    producer_status = re.search(r"failed:\s*exit status\s+(\d+)", stderr, re.IGNORECASE)
                    if backup_type == "database" and producer_status:
                        stage_results.append(_stage_result("source_capture", "failed", int(producer_status.group(1)), duration, "pg_dump exited unsuccessfully; Restic did not create a snapshot."))
                        stage_results.append(_stage_result("restic", "failed", exit_code, duration, "Restic rejected the failed pg_dump stream."))
                        primary_failure = (exit_code, "source_capture", "pg_dump failed; Restic did not create a snapshot.")
                    else:
                        msg = _redact_secrets((stderr or f"Restic exited with code {exit_code}") + _auth_failure_hint(stderr, job_id), run_env)
                        stage_results.append(_stage_result("restic", "failed", exit_code, duration, msg))
                        primary_failure = (exit_code, "restic", msg)
                    # Never trust a snapshot summary when Restic reports failure.
                    snapshot_stats.pop("snapshot_id", None)

        if pipeline_started and post_selection is not None:
            definition, parameters = post_selection
            stage_started = time.monotonic()
            cleanup_mode = primary_failure is not None
            hook_done = threading.Event()
            hook_stats = {"stage": "post_hook"}
            client.report_status(job_id, {"job_id": job_id, "run_id": run_id, "status": "running", "stage": "post_hook", **metadata})
            hook_progress = threading.Thread(
                target=_progress_reporter,
                args=(client, job_id, run_id, start_time, hook_stats, hook_done, metadata),
                daemon=True,
            )
            hook_progress.start()
            try:
                # A post-hook after a prior failure/cancellation is cleanup and
                # receives a full bounded opportunity. On the success path it
                # remains cancellable like every other active pipeline stage.
                result = hooks.execute_hook(
                    definition, parameters,
                    cancel_event=None if cleanup_mode else cancel_event,
                )
            except Exception:
                result = None
            finally:
                hook_done.set()
                hook_progress.join(timeout=PROGRESS_INTERVAL_SECONDS + 2)
            duration = int(time.monotonic() - stage_started)
            if result is None:
                stage_results.append(_stage_result("post_hook", "failed", 1, duration, "Post-hook could not be started safely."))
                if primary_failure is None:
                    primary_failure = (1, "post_hook", "Post-hook could not be started safely.")
            else:
                failed = bool(result.exit_code or result.timed_out or result.cancelled)
                message = "Cancelled by user" if result.cancelled else "Post-hook timed out." if result.timed_out else f"Post-hook failed with exit code {result.exit_code}." if result.exit_code else None
                stage_results.append(_stage_result("post_hook", "cancelled" if result.cancelled else "failed" if failed else "success", result.exit_code, duration, message))
                if failed and primary_failure is None:
                    primary_failure = (130 if result.cancelled else 124 if result.timed_out else result.exit_code, "post_hook", message or "Post-hook failed.")

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
        else:
            zero_files = backup_type == "files" and any(
                result["stage"] == "restic" and result["status"] == "success"
                for result in stage_results
            ) and snapshot_stats.get("files_processed") == 0
            message = "Backup completed but no files were processed. Check the source path." if zero_files else "Backup completed"
            report_terminal("warning" if zero_files else "success", 0, message, "post_hook" if post_selection else ("source_capture" if backup_type == "database" else "restic"))
            if zero_files:
                logger.warning("Job %s completed with no files processed.", job_id)
            else:
                logger.info("Job %s succeeded (snapshot %s).", job_id, snapshot_stats.get("snapshot_id"))
    except (ValueError, hooks.HookCatalogError, postgresql.PostgreSQLConfigError) as exc:
        safe_message = str(exc)[:MAX_STAGE_MESSAGE_CHARS]
        stage_results.append(_stage_result("validation", "failed", 1, None, safe_message))
        report_terminal("failed", 1, safe_message, "validation")
        logger.error("Job %s failed validation: %s", job_id, safe_message)
    except Exception:
        logger.exception("Unexpected pipeline error for job %s.", job_id)
        stage_results.append(_stage_result("pipeline", "failed", 1, int(time.monotonic() - start_time), "Unexpected pipeline error."))
        report_terminal("failed", 1, "Unexpected pipeline error; inspect the local agent log.", "pipeline")
    finally:
        done_event.set()
        if cancel_thread is not None:
            cancel_thread.join(timeout=CANCEL_INTERVAL_SECONDS + 2)
        lock.release()


def _run_single_job(
    client: api.ApiClient,
    cfg: config.Config,
    job: dict[str, Any],
    restic_env: dict[str, str],
) -> None:
    """Dispatch legacy file jobs unchanged and route new jobs through pipeline."""
    backup_type = job.get("backup_type", "files")
    if (
        backup_type == "files"
        and job.get("pre_hook") is None
        and job.get("post_hook") is None
        and isinstance(job.get("source"), str)
        and bool(job.get("source"))
        and job.get("source_config") is None
        and isinstance(job.get("destination"), str)
        and bool(job.get("destination"))
    ):
        _run_files_job(client, cfg, job, restic_env)
        return
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
