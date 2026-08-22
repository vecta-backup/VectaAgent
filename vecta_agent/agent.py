"""Core agent runtime: fetch jobs, run restic, report status."""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from vecta_agent import api, config, restic

logger = logging.getLogger("vecta_agent")

PROGRESS_INTERVAL_SECONDS = 10
CANCEL_INTERVAL_SECONDS = 10


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


def _progress_reporter(
    client: api.ApiClient,
    job_id: str,
    start_time: float,
    stats: dict[str, Any],
    done_event: threading.Event,
) -> None:
    """POST running status every ~10 seconds while a backup is active."""
    while not done_event.wait(PROGRESS_INTERVAL_SECONDS):
        with threading.Lock():
            payload: dict[str, Any] = {
                "job_id": job_id,
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


def _run_single_job(
    client: api.ApiClient,
    cfg: config.Config,
    job: dict[str, Any],
    restic_env: dict[str, str],
) -> None:
    job_id = job["job_id"]
    source = job["source"]
    destination = job["destination"]

    lock = JobLock(job_id)
    if not lock.acquire():
        return

    try:
        client.report_status(job_id, {"job_id": job_id, "status": "running"})
        logger.info("Starting job %s: %s -> %s", job_id, source, destination)

        if not _has_repo_password(restic_env):
            client.report_status(
                job_id,
                {
                    "job_id": job_id,
                    "status": "failed",
                    "exit_code": 1,
                    "message": (
                        "No repository password configured. Run 'vecta-agent repo init "
                        f"{destination}' on this machine, or add RESTIC_PASSWORD to restic.env."
                    ),
                },
            )
            logger.error("Job %s failed: no repository password configured.", job_id)
            return

        try:
            repo_check = restic.check_repo(destination, env=restic_env)
            if repo_check.exists is False:
                logger.info("Repository at %s not found; initializing.", destination)
                init_result = restic.init_repo(destination, env=restic_env)
                if init_result.exit_code != 0:
                    client.report_status(
                        job_id,
                        {
                            "job_id": job_id,
                            "status": "failed",
                            "exit_code": init_result.exit_code,
                            "message": init_result.stderr_tail
                            or "Failed to initialize repository.",
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
                        "status": "failed",
                        "exit_code": 1,
                        "message": repo_check.stderr_tail
                        or "Could not verify repository at destination.",
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
                    "status": "failed",
                    "exit_code": 127,
                    "message": "restic binary not found in PATH",
                },
            )
            logger.error("Job %s failed: restic binary not found in PATH.", job_id)
            return

        start_time = time.monotonic()
        runner = restic.ResticRunner(source, destination, env=restic_env)
        try:
            runner.start()
        except FileNotFoundError:
            client.report_status(
                job_id,
                {
                    "job_id": job_id,
                    "status": "failed",
                    "exit_code": 127,
                    "message": "restic executable not found on PATH; install restic to run backups",
                },
            )
            logger.error("Job %s failed: restic executable not found on PATH.", job_id)
            return

        stats: dict[str, Any] = {}
        cancel_event = threading.Event()
        done_event = threading.Event()

        progress_thread = threading.Thread(
            target=_progress_reporter,
            args=(client, job_id, start_time, stats, done_event),
            daemon=True,
        )
        cancel_thread = threading.Thread(
            target=_cancel_poller,
            args=(client, job_id, cancel_event, done_event, runner),
            daemon=True,
        )
        progress_thread.start()
        cancel_thread.start()

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
        finally:
            exit_code = runner.wait()
            done_event.set()
            progress_thread.join(timeout=CANCEL_INTERVAL_SECONDS + 2)
            cancel_thread.join(timeout=CANCEL_INTERVAL_SECONDS + 2)

        duration = int(time.monotonic() - start_time)

        if cancel_event.is_set():
            payload = {
                "job_id": job_id,
                "status": "failed",
                "exit_code": 130,
                "message": "Cancelled by user",
                "duration_seconds": duration,
            }
            client.report_status(job_id, payload)
            logger.info("Job %s cancelled.", job_id)
            return

        if exit_code == 0:
            payload = {
                "job_id": job_id,
                "status": "success",
                "exit_code": 0,
                "message": "Backup completed",
                "duration_seconds": duration,
                "snapshot_id": stats.get("snapshot_id"),
                "files_processed": stats.get("files_processed"),
                "bytes_processed": stats.get("bytes_processed"),
                "transferred_bytes": stats.get("transferred_bytes"),
            }
            client.report_status(job_id, payload)
            logger.info("Job %s succeeded (snapshot %s).", job_id, stats.get("snapshot_id"))
        else:
            payload = {
                "job_id": job_id,
                "status": "failed",
                "exit_code": exit_code,
                "message": runner.stderr_tail() or f"restic exited with code {exit_code}",
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
