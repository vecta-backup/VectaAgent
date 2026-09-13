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

from vecta_agent import api, config, credentials, restic

logger = logging.getLogger("vecta_agent")

PROGRESS_INTERVAL_SECONDS = 10
CANCEL_INTERVAL_SECONDS = 10

# Hard cap on a single restic backup invocation. A hung restic process (or a
# suspended machine) would otherwise keep the job "running" forever. When the
# cap is hit the process is terminated via the same SIGTERM-then-SIGKILL path
# used for user cancellation and the run is reported as a distinct timeout
# failure. Fixed on purpose — not user-configurable for now.
MAX_BACKUP_DURATION_SECONDS = 12 * 60 * 60

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


def _progress_reporter(
    client: api.ApiClient,
    job_id: str,
    run_id: str,
    start_time: float,
    stats: dict[str, Any],
    done_event: threading.Event,
) -> None:
    """POST running status every ~10 seconds while a backup is active."""
    while not done_event.wait(PROGRESS_INTERVAL_SECONDS):
        with threading.Lock():
            payload: dict[str, Any] = {
                "job_id": job_id,
                "run_id": run_id,
                "status": "running",
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


def _cancel_poller(
    client: api.ApiClient,
    job_id: str,
    cancel_event: threading.Event,
    done_event: threading.Event,
    runner: restic.ResticRunner,
) -> None:
    """Poll backend cancel flag every ~10 seconds and terminate restic if set."""
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
            runner.terminate()
            logger.info("Cancellation requested for job %s; terminating restic.", job_id)
            break


def _timeout_message() -> str:
    """Human-readable timeout failure reason, derived from the constant."""
    hours = MAX_BACKUP_DURATION_SECONDS / 3600
    if hours >= 1:
        return f"Backup timed out after {hours:g}h and was terminated."
    return f"Backup timed out after {MAX_BACKUP_DURATION_SECONDS:g}s and was terminated."


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


def _resolve_job_env(
    client: api.ApiClient,
    job_id: str,
    job: dict[str, Any],
    restic_env: dict[str, str],
    run_id: str,
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
                "exit_code": 1,
                "message": f"Invalid local credentials file: {exc}",
            },
        )
        logger.error("Job %s failed: invalid local credentials file: %s", job_id, exc)
        return None
    if stored:
        job_env.update(stored)
    return job_env


def _run_single_job(
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

    try:
        client.report_status(
            job_id, {"job_id": job_id, "run_id": run_id, "status": "running"}
        )
        logger.info("Starting job %s: %s -> %s", job_id, source, destination)

        job_env = _resolve_job_env(client, job_id, job, restic_env, run_id)
        if job_env is None:
            return

        if not _has_repo_password(job_env):
            client.report_status(
                job_id,
                {
                    "job_id": job_id,
                    "run_id": run_id,
                    "status": "failed",
                    "exit_code": 1,
                    "message": (
                        "No repository password configured for this destination. "
                        f"Run 'vecta-agent setup {job_id}' on this machine to "
                        "configure it."
                    ),
                },
            )
            logger.error("Job %s failed: no repository password configured.", job_id)
            return

        try:
            repo_check = restic.check_repo(destination, env=job_env, port=port)
            if repo_check.exists is False:
                logger.info("Repository at %s not found; initializing.", destination)
                init_result = restic.init_repo(destination, env=job_env, port=port)
                if init_result.exit_code != 0:
                    client.report_status(
                        job_id,
                        {
                            "job_id": job_id,
                            "run_id": run_id,
                            "status": "failed",
                            "exit_code": init_result.exit_code,
"message": (init_result.stderr_tail or "Failed to initialize repository.")
                    + _auth_failure_hint(init_result.stderr_tail, job_id),
                        },
                    )
                    logger.error(
                        "Job %s failed: could not initialize repository at %s: %s",
                        job_id,
                        destination,
                        init_result.stderr_tail,
                    )
                    return
                logger.info("Repository at %s initialized.", destination)
            elif repo_check.exists is None:
                client.report_status(
                    job_id,
                    {
                        "job_id": job_id,
                        "run_id": run_id,
                        "status": "failed",
                        "exit_code": 1,
                        "message": (repo_check.stderr_tail or "Could not verify repository at destination.")
                        + _auth_failure_hint(repo_check.stderr_tail, job_id),
                    },
                )
                logger.error(
                    "Job %s failed: could not verify repository at %s: %s",
                    job_id,
                    destination,
                    repo_check.stderr_tail,
                )
                return
        except FileNotFoundError:
            client.report_status(
                job_id,
                {
                    "job_id": job_id,
                    "run_id": run_id,
                    "status": "failed",
                    "exit_code": 127,
                    "message": "restic binary not found in PATH",
                },
            )
            logger.error("Job %s failed: restic binary not found in PATH.", job_id)
            return

        start_time = time.monotonic()
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
                    "exit_code": 127,
                    "message": "restic executable not found on PATH; install restic to run backups",
                },
            )
            logger.error("Job %s failed: restic executable not found on PATH.", job_id)
            return

        stats: dict[str, Any] = {}
        cancel_event = threading.Event()
        timeout_event = threading.Event()
        done_event = threading.Event()

        progress_thread = threading.Thread(
            target=_progress_reporter,
            args=(client, job_id, run_id, start_time, stats, done_event),
            daemon=True,
        )
        cancel_thread = threading.Thread(
            target=_cancel_poller,
            args=(client, job_id, cancel_event, done_event, runner),
            daemon=True,
        )
        timeout_thread = threading.Thread(
            target=_timeout_watchdog,
            args=(runner, timeout_event, done_event),
            daemon=True,
        )
        progress_thread.start()
        cancel_thread.start()
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
            done_event.set()
            progress_thread.join(timeout=CANCEL_INTERVAL_SECONDS + 2)
            cancel_thread.join(timeout=CANCEL_INTERVAL_SECONDS + 2)
            timeout_thread.join(timeout=CANCEL_INTERVAL_SECONDS + 2)

        duration = int(time.monotonic() - start_time)

        if cancel_event.is_set():
            payload = {
                "job_id": job_id,
                "run_id": run_id,
                "status": "failed",
                "exit_code": 130,
                "message": "Cancelled by user",
                "duration_seconds": duration,
            }
            client.report_status(job_id, payload)
            logger.info("Job %s cancelled.", job_id)
            return

        if timeout_event.is_set():
            payload = {
                "job_id": job_id,
                "run_id": run_id,
                "status": "failed",
                "exit_code": 124,
                "message": _timeout_message(),
                "duration_seconds": duration,
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
                "exit_code": exit_code,
                "message": (runner.stderr_tail() or f"restic exited with code {exit_code}")
                + _auth_failure_hint(runner.stderr_tail(), job_id),
                "duration_seconds": duration,
            }
            client.report_status(job_id, payload)
            logger.error("Job %s failed with exit code %s.", job_id, exit_code)
    finally:
        lock.release()


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
