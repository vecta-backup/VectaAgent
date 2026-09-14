"""Run restic backup and parse its --json output."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("vecta_agent")

# Hard cap on one-shot restic commands: the repository-existence probe
# (`cat config`) and `init`. Both read or create a single small file, so they
# finish in seconds on a reachable destination; anything longer means the
# connection is hung (e.g. a firewall silently dropping packets) and the
# command would otherwise block the agent pass indefinitely. Not
# user-configurable, like the 12h backup cap in agent.py.
PROBE_TIMEOUT_SECONDS = 120


@dataclass
class ResticResult:
    exit_code: int
    snapshot_id: str | None = None
    files_processed: int | None = None
    bytes_processed: int | None = None
    transferred_bytes: int | None = None
    stderr_tail: str = ""
    # True when the command was killed after `timeout_seconds` rather than
    # finishing on its own; exit_code is then always 124 (the agent's
    # timeout convention, matching the backup watchdog).
    timed_out: bool = False


class ResticRunner:
    """Manages a restic backup subprocess and parses --json output."""

    def __init__(
        self,
        source: str,
        destination: str,
        port: int | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.source = source
        self.destination = destination
        self.port = port
        self.env = env or {}
        self._proc: subprocess.Popen[str] | None = None
        self._stderr_lines: list[str] = []
        self._stderr_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _build_cmd(self) -> list[str]:
        return [
            "restic",
            "backup",
            "--json",
            *repo_options(self.destination, self.port),
            "--repo",
            self.destination,
            self.source,
        ]

    def start(self) -> None:
        env = {**os.environ, **self.env}
        self._proc = subprocess.Popen(
            self._build_cmd(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        for line in self._proc.stderr:
            with self._lock:
                self._stderr_lines.append(line.rstrip("\n"))

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
        """Send SIGTERM, then SIGKILL after grace."""
        proc = self._proc
        if proc is None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

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


def repo_options(destination: str, port: int | None) -> list[str]:
    """Extended restic options for a destination.

    SFTP with a non-default port: restic's `sftp.command` option replaces the
    default ssh invocation ENTIRELY and restic does not append the sftp
    subsystem for it, so the command must carry `-s sftp` itself (restic
    sftp.go buildSSHCommand; no %h/%r placeholders exist). Only the legacy
    `sftp:user@host:/path` format is overridden — with the
    `sftp://user@host:port/path` URL format restic reads the port from the URL
    and needs no override. Ignored for non-sftp destinations — their locations
    carry ports in their URL syntax.
    """
    if port is None or not destination.startswith("sftp:"):
        return []
    if destination.startswith("sftp://"):
        return []
    connection = destination[len("sftp:"):].split(":", 1)[0]
    if not connection:
        logger.warning(
            "Could not parse the SFTP destination %r; ignoring SSH port %s. "
            "Use the sftp:user@host:/path format.",
            destination,
            port,
        )
        return []
    return ["-o", f"sftp.command=ssh -p {port} {connection} -s sftp"]


def run_restic(
    args: list[str],
    env: dict[str, str] | None = None,
    timeout_seconds: int = PROBE_TIMEOUT_SECONDS,
) -> ResticResult:
    """Run a one-shot restic command and capture exit code + stderr tail.

    Bounded by `timeout_seconds`: on expiry the process is killed (no grace
    period — a one-shot command past its deadline is already hung) and the
    result carries `timed_out=True` with exit code 124.
    """
    full_env = {**os.environ, **(env or {})}
    proc = subprocess.Popen(
        ["restic", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=full_env,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()  # reap the killed process
        return ResticResult(
            exit_code=124,
            stderr_tail=f"restic {args[0]} timed out after {timeout_seconds}s and was killed",
            timed_out=True,
        )
    return ResticResult(exit_code=proc.returncode, stderr_tail=stderr.strip())


class RepoCheck:
    """Result of a repository existence probe.

    `exists` is True when the repo is present, False when restic reports it
    definitively missing, and None when the check is inconclusive (e.g. the
    storage backend is unreachable or the password is wrong).

    `timed_out` is True when the probe was killed by the timeout instead of
    finishing with a restic verdict — `exists` is then None, but the caller
    should report a network-timeout failure rather than suggesting setup.
    """

    def __init__(
        self,
        exists: bool | None,
        stderr_tail: str = "",
        timed_out: bool = False,
    ) -> None:
        self.exists = exists
        self.stderr_tail = stderr_tail
        self.timed_out = timed_out


def _looks_missing(stderr: str) -> bool:
    """Whether a non-zero `restic cat config` stderr means the repo is absent.

    `unable to open config file` also prefixes permission errors on an existing
    repo, so it only counts as missing when the stderr names a missing-path
    cause rather than a permission or credentials problem.
    """
    low = stderr.lower()
    if "repository does not exist" in low:
        return True
    if "unable to open config file" in low:
        return any(m in low for m in ("no such file", "not found", "does not exist"))
    return False


def check_repo(
    destination: str,
    env: dict[str, str] | None = None,
    port: int | None = None,
    timeout_seconds: int = PROBE_TIMEOUT_SECONDS,
) -> RepoCheck:
    """Probe whether a restic repository exists at `destination`.

    Uses `restic cat config`, whose exit code since restic 0.17 is 0 when the
    repo exists and 10 when it definitively does not. Any other exit code means
    the check is inconclusive and the caller must NOT initialize.

    Bounded by `timeout_seconds`: a probe that outlives it (a hung connection,
    not a restic verdict) returns `exists=None, timed_out=True`.
    """
    args = [*repo_options(destination, port), "cat", "config", "--repo", destination]
    result = run_restic(args, env=env, timeout_seconds=timeout_seconds)
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
    env: dict[str, str] | None = None,
    port: int | None = None,
    timeout_seconds: int = PROBE_TIMEOUT_SECONDS,
) -> ResticResult:
    """Initialize a new restic repository at `destination`."""
    args = [*repo_options(destination, port), "init", "--repo", destination]
    return run_restic(args, env=env, timeout_seconds=timeout_seconds)


def parse_summary(obj: dict[str, Any]) -> dict[str, Any]:
    """Extract fields from a restic summary JSON object."""
    result: dict[str, Any] = {}
    result["snapshot_id"] = obj.get("snapshot_id")
    result["files_processed"] = obj.get("total_files_processed")
    result["bytes_processed"] = obj.get("total_bytes_processed")
    # transferred_bytes: post-dedup upload size
    transferred = obj.get("data_added_packed")
    if transferred is None:
        transferred = obj.get("data_added")
    result["transferred_bytes"] = transferred
    return result


def compute_progress(obj: dict[str, Any]) -> int | None:
    """Compute 0-99 progress from a restic status object."""
    percent = obj.get("percent_done")
    if isinstance(percent, (int, float)):
        val = int(percent * 100)
        return max(0, min(val, 99))
    total = obj.get("total_bytes")
    done = obj.get("bytes_done")
    if isinstance(total, (int, float)) and isinstance(done, (int, float)) and total > 0:
        val = int((done / total) * 100)
        return max(0, min(val, 99))
    return None
