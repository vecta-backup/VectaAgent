"""Repository probing, initialization, unlocking, and retention execution."""

from __future__ import annotations

import threading
from typing import Callable

from vecta_agent.restic_options import (
    forget_args,
    is_repository_lock_error,
    repo_options,
)
from vecta_agent.restic_types import RepoCheck, ResticResult

PROBE_TIMEOUT_SECONDS = 120
PRUNE_TIMEOUT_SECONDS = 60 * 60


def unlock_repo(
    destination: str,
    port: int | None,
    env: dict[str, str] | None,
    timeout_seconds: int,
    cancel_event: threading.Event | None,
    *,
    run_command: Callable[..., ResticResult],
) -> ResticResult:
    args = [*repo_options(destination, port), "unlock", "--repo", destination]
    return run_command(args, env=env, timeout_seconds=timeout_seconds, cancel_event=cancel_event)


def run_forget(
    destination: str,
    policy: dict[str, int | None],
    port: int | None,
    env: dict[str, str] | None,
    timeout_seconds: int,
    cancel_event: threading.Event | None,
    *,
    job_id: str,
    run_command: Callable[..., ResticResult],
    unlock_command: Callable[..., ResticResult],
) -> ResticResult:
    """Run forget --prune, retrying once only after safe lock recovery."""
    args = forget_args(destination, policy, port, job_id=job_id)
    result = run_command(args, env=env, timeout_seconds=timeout_seconds, cancel_event=cancel_event)
    if result.exit_code == 0 or result.timed_out or result.cancelled or not is_repository_lock_error(result.stderr_tail):
        return result
    unlock = unlock_command(destination, port, env, cancel_event=cancel_event)
    recovery = f"Lock recovery: unlock exited {unlock.exit_code}."
    if unlock.exit_code != 0 or unlock.timed_out or unlock.cancelled:
        result.recovery_message = recovery + (f" {unlock.stderr_tail[:300]}" if unlock.stderr_tail else "")
        return result
    retry = run_command(args, env=env, timeout_seconds=timeout_seconds, cancel_event=cancel_event)
    retry.recovery_message = recovery + f" Retry exited {retry.exit_code}."
    return retry


def _looks_missing(stderr: str) -> bool:
    low = stderr.lower()
    if "repository does not exist" in low:
        return True
    if "unable to open config file" in low:
        return any(marker in low for marker in ("no such file", "not found", "does not exist"))
    return False


def check_repo(
    destination: str,
    env: dict[str, str] | None,
    port: int | None,
    timeout_seconds: int,
    cancel_event: threading.Event | None,
    *,
    run_command: Callable[..., ResticResult],
) -> RepoCheck:
    args = [*repo_options(destination, port), "cat", "config", "--repo", destination]
    result = run_command(args, env=env, timeout_seconds=timeout_seconds, cancel_event=cancel_event)
    if result.cancelled:
        return RepoCheck(None, result.stderr_tail, cancelled=True)
    if result.timed_out:
        return RepoCheck(None, result.stderr_tail, timed_out=True)
    if result.exit_code == 0:
        return RepoCheck(True)
    if result.exit_code == 10:
        return RepoCheck(False, result.stderr_tail)
    if result.exit_code == 1 and _looks_missing(result.stderr_tail):
        return RepoCheck(False, result.stderr_tail)
    return RepoCheck(None, result.stderr_tail)


def init_repo(
    destination: str,
    env: dict[str, str] | None,
    port: int | None,
    timeout_seconds: int,
    cancel_event: threading.Event | None,
    *,
    run_command: Callable[..., ResticResult],
) -> ResticResult:
    args = [*repo_options(destination, port), "init", "--repo", destination]
    return run_command(args, env=env, timeout_seconds=timeout_seconds, cancel_event=cancel_event)
