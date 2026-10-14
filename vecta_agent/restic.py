"""Run restic backup and parse its --json output."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading

from vecta_agent import restic_command, restic_progress as restic_progress, restic_repository
from vecta_agent.restic_progress import (
    compute_progress as compute_progress,
    parse_summary as parse_summary,
)
from vecta_agent.restic_types import RepoCheck, ResticResult

from vecta_agent.restic_options import (
    MAX_EXCLUDE_PATTERNS as MAX_EXCLUDE_PATTERNS,
    MAX_EXCLUDE_PATTERN_CHARS as MAX_EXCLUDE_PATTERN_CHARS,
    MAX_EXCLUDE_SERIALIZED_BYTES as MAX_EXCLUDE_SERIALIZED_BYTES,
    MAX_RETENTION_VALUE as MAX_RETENTION_VALUE,
    forget_args as forget_args,
    is_repository_lock_error as is_repository_lock_error,
    repo_options,
    validate_exclude_patterns,
    validate_retention_policy as validate_retention_policy,
)

logger = logging.getLogger("vecta_agent")

# Bounded probe/init timeout: remote storage that hangs must not stall a pass.
PROBE_TIMEOUT_SECONDS = 120
PRUNE_TIMEOUT_SECONDS = 60 * 60

# Public compatibility aliases for subprocess limits.
CANCEL_POLL_SLICE_SECONDS = restic_command.CANCEL_POLL_SLICE_SECONDS
MAX_STDERR_CHARS = restic_command.MAX_STDERR_CHARS
MAX_STDERR_LINES = 100


class ResticRunner:
    """Manages a restic backup subprocess and parses --json output."""

    def __init__(
        self,
        source: str | None,
        destination: str,
        port: int | None = None,
        env: dict[str, str] | None = None,
        stdin_filename: str | None = None,
        source_command: list[str] | None = None,
        unset_env: tuple[str, ...] = (),
        exclude_patterns: list[str] | None = None,
        *,
        job_id: str,
    ) -> None:
        self.source = source
        self.destination = destination
        self.port = port
        self.env = env or {}
        self.stdin_filename = stdin_filename
        self.source_command = list(source_command) if source_command is not None else None
        self.unset_env = tuple(unset_env)
        self.exclude_patterns = validate_exclude_patterns(exclude_patterns or [])
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("A job ID is required to tag Restic backups.")
        self.job_id = job_id
        self._proc: subprocess.Popen[str] | None = None
        self._stderr_lines: list[str] = []
        self._stderr_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _build_cmd(self) -> list[str]:
        command = [
            "restic",
            "backup",
            "--json",
            f"--tag=job:{self.job_id}",
            *(f"--exclude={pattern}" for pattern in self.exclude_patterns),
            *repo_options(self.destination, self.port),
            "--repo",
            self.destination,
        ]
        if self.source_command is not None:
            if not self.stdin_filename:
                raise ValueError("stdin_filename is required for command-backed stdin backups")
            command += ["--stdin-from-command", "--stdin-filename", self.stdin_filename, "--", *self.source_command]
        else:
            if not self.source:
                raise ValueError("source path is required for filesystem backups")
            command.append(self.source)
        return command

    def start(self) -> None:
        env = {**os.environ, **self.env}
        for key in self.unset_env:
            env.pop(key, None)
        self._proc = subprocess.Popen(
            self._build_cmd(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            shell=False,
            start_new_session=(os.name == "posix"),
        )
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        for line in self._proc.stderr:
            with self._lock:
                self._stderr_lines.append(line.rstrip("\n"))
                if len(self._stderr_lines) > MAX_STDERR_LINES:
                    del self._stderr_lines[:-MAX_STDERR_LINES]

    def stream(self):
        """Yield parsed JSON objects from restic stdout."""
        if self._proc is None or self._proc.stdout is None:
            return
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield obj

    def terminate(self, grace_seconds: float = 10.0) -> None:
        """Signal the process group, escalate, and reap Restic."""
        proc = self._proc
        if proc is None:
            return
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except ProcessLookupError:
                pass
            proc.wait()
        if self._proc.stderr is not None and self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2.0)

    def wait(self) -> int:
        """Wait for the process to finish and return exit code."""
        if self._proc is None:
            return -1
        code = self._proc.wait()
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2.0)
        return code

    def stderr_tail(self, max_lines: int = 20) -> str:
        with self._lock:
            lines = self._stderr_lines[-max_lines:]
        return "\n".join(lines)

    @property
    def returncode(self) -> int | None:
        if self._proc is None:
            return None
        return self._proc.returncode


def unlock_repo(
    destination: str,
    port: int | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: int = PROBE_TIMEOUT_SECONDS,
    cancel_event: threading.Event | None = None,
) -> ResticResult:
    return restic_repository.unlock_repo(
        destination, port, env, timeout_seconds, cancel_event, run_command=run_restic
    )


def run_forget(
    destination: str,
    policy: dict[str, int | None],
    port: int | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: int = PRUNE_TIMEOUT_SECONDS,
    cancel_event: threading.Event | None = None,
    *,
    job_id: str,
) -> ResticResult:
    return restic_repository.run_forget(
        destination, policy, port, env, timeout_seconds, cancel_event, job_id=job_id,
        run_command=run_restic, unlock_command=unlock_repo,
    )


def _timeout_result(args: list[str], timeout_seconds: int) -> ResticResult:
    return restic_command.timeout_result(args, timeout_seconds)


def _kill_process_group(proc: subprocess.Popen) -> None:
    restic_command.kill_process_group(
        proc, os_module=os, subprocess_module=subprocess
    )


def run_restic(
    args: list[str],
    env: dict[str, str] | None = None,
    timeout_seconds: int = PROBE_TIMEOUT_SECONDS,
    cancel_event: threading.Event | None = None,
) -> ResticResult:
    return restic_command.run_restic(
        args,
        env,
        timeout_seconds,
        cancel_event,
        os_module=os,
        subprocess_module=subprocess,
        kill_process_group=_kill_process_group,
    )


_looks_missing = restic_repository._looks_missing


def check_repo(
    destination: str,
    env: dict[str, str] | None = None,
    port: int | None = None,
    timeout_seconds: int = PROBE_TIMEOUT_SECONDS,
    cancel_event: threading.Event | None = None,
) -> RepoCheck:
    return restic_repository.check_repo(
        destination, env, port, timeout_seconds, cancel_event, run_command=run_restic
    )


def init_repo(
    destination: str,
    env: dict[str, str] | None = None,
    port: int | None = None,
    timeout_seconds: int = PROBE_TIMEOUT_SECONDS,
    cancel_event: threading.Event | None = None,
) -> ResticResult:
    return restic_repository.init_repo(
        destination, env, port, timeout_seconds, cancel_event, run_command=run_restic
    )
