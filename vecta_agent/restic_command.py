"""Bounded one-shot Restic command execution and process-group termination."""

from __future__ import annotations

import signal
import threading
import time
from typing import Any, Callable

from vecta_agent.restic_types import ResticResult

CANCEL_POLL_SLICE_SECONDS = 1.0
MAX_STDERR_CHARS = 4000


def timeout_result(args: list[str], timeout_seconds: int) -> ResticResult:
    return ResticResult(
        exit_code=124,
        stderr_tail=f"restic {args[0]} timed out after {timeout_seconds}s and was killed",
        timed_out=True,
    )


def kill_process_group(proc: Any, *, os_module, subprocess_module) -> None:
    try:
        if os_module.name == "posix":
            os_module.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass


def run_restic(
    args: list[str],
    env: dict[str, str] | None,
    timeout_seconds: int,
    cancel_event: threading.Event | None,
    *,
    os_module,
    subprocess_module,
    kill_process_group: Callable[[Any], None],
) -> ResticResult:
    """Run a one-shot command with bounded timeout and cooperative cancel."""
    full_env = {**os_module.environ, **(env or {})}
    proc = subprocess_module.Popen(
        ["restic", *args],
        stdout=subprocess_module.PIPE,
        stderr=subprocess_module.PIPE,
        text=True,
        env=full_env,
        shell=False,
        start_new_session=(os_module.name == "posix"),
    )
    if cancel_event is None:
        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
        except subprocess_module.TimeoutExpired:
            kill_process_group(proc)
            proc.communicate()
            return timeout_result(args, timeout_seconds)
        return ResticResult(
            exit_code=proc.returncode,
            stdout=stdout,
            stderr_tail=stderr.strip()[-MAX_STDERR_CHARS:],
        )

    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            kill_process_group(proc)
            proc.communicate()
            return timeout_result(args, timeout_seconds)
        try:
            stdout, stderr = proc.communicate(
                timeout=min(CANCEL_POLL_SLICE_SECONDS, remaining)
            )
            return ResticResult(
                exit_code=proc.returncode,
                stdout=stdout,
                stderr_tail=stderr.strip()[-MAX_STDERR_CHARS:],
            )
        except subprocess_module.TimeoutExpired:
            if cancel_event.is_set():
                kill_process_group(proc)
                proc.communicate()
                return ResticResult(
                    exit_code=130,
                    stderr_tail=f"restic {args[0]} was cancelled by user",
                    cancelled=True,
                )
