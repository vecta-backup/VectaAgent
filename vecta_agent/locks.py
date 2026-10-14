"""Best-effort local locks protecting jobs and shared repositories."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from pathlib import Path

logger = logging.getLogger("vecta_agent")

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows development environment
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


class RepositoryLock(JobLock):
    """Best-effort lock shared by local jobs targeting the same repository."""

    def __init__(self, destination: str) -> None:
        digest = hashlib.sha256(destination.strip().encode("utf-8")).hexdigest()
        self.path = Path(tempfile.gettempdir()) / f"vecta-repo-{digest}.lock"
        self._fd = None


class RunLocks:
    """Hold per-job and repository locks for a complete job lifecycle."""

    def __init__(self, job_id: str, destination: str) -> None:
        self.job_lock = JobLock(job_id)
        self.repository_lock = RepositoryLock(destination)

    def acquire(self) -> bool:
        if not self.job_lock.acquire():
            return False
        if not self.repository_lock.acquire():
            self.job_lock.release()
            return False
        return True

    def release(self) -> None:
        self.repository_lock.release()
        self.job_lock.release()
