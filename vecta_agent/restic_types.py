"""Result models shared by Restic subprocess and repository operations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ResticResult:
    exit_code: int
    snapshot_id: str | None = None
    files_processed: int | None = None
    bytes_processed: int | None = None
    transferred_bytes: int | None = None
    stderr_tail: str = ""
    timed_out: bool = False
    cancelled: bool = False
    recovery_message: str | None = None
    stdout: str = ""


class RepoCheck:
    """Result of a Restic repository-existence probe."""

    def __init__(
        self,
        exists: bool | None,
        stderr_tail: str = "",
        timed_out: bool = False,
        cancelled: bool = False,
    ) -> None:
        self.exists = exists
        self.stderr_tail = stderr_tail
        self.timed_out = timed_out
        self.cancelled = cancelled
