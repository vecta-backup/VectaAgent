"""Bounded, shell-free execution for locally installed hooks."""

from __future__ import annotations

import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vecta_agent.hook_schema import HookCatalogError, validate_parameters

_PARAM_RE = re.compile(r"^\$\{([A-Za-z][A-Za-z0-9_]*)\}$")


@dataclass
class HookResult:
    exit_code: int
    duration_seconds: int
    timed_out: bool = False
    cancelled: bool = False
    stdout: str = ""
    stderr: str = ""


def _argv(definition: Any, parameters: dict[str, Any]) -> list[str]:
    args: list[str] = []
    for token in definition.argv:
        match = _PARAM_RE.fullmatch(token)
        if match:
            value = parameters[match.group(1)]
            args.append(str(value).lower() if isinstance(value, bool) else str(value))
        else:
            args.append(token)
    return [definition.executable, *args]


def _bounded_reader(pipe, box: list[str], limit: int) -> None:
    kept = bytearray()
    while True:
        block = pipe.read(4096)
        if not block:
            break
        if len(kept) < limit:
            kept.extend(block[:limit - len(kept)])
    box.append(kept.decode("utf-8", errors="replace"))
    pipe.close()


def _signal_process(proc: subprocess.Popen, sig: int, os_module) -> None:
    try:
        if os_module.name == "posix":
            os_module.killpg(proc.pid, sig)
        elif sig == signal.SIGTERM:
            proc.terminate()
        else:
            proc.kill()
    except ProcessLookupError:
        pass


def execution_identity(definition: Any, *, os_module, pwd_module) -> dict[str, Any]:
    if os_module.name != "posix":
        return {}
    if pwd_module is None:
        raise HookCatalogError("POSIX account lookup is unavailable.")
    entry = pwd_module.getpwnam(definition.run_as)
    if definition.requires_root and os_module.geteuid() != 0:
        raise HookCatalogError("Root-authorized hook cannot run because the agent is not root.")
    return {"user": entry.pw_uid, "group": entry.pw_gid, "extra_groups": ()}


def execute_hook(
    definition: Any,
    parameters: dict[str, Any],
    *,
    cancel_event: threading.Event | None,
    timeout_seconds: int | None,
    load_catalog,
    os_module,
    pwd_module,
    timeout_ceiling: int,
) -> HookResult:
    """Run a validated hook with bounded output and process lifetime."""
    if definition.catalog_path:
        current = load_catalog(Path(definition.catalog_path)).get(definition.id)
        if current != definition:
            raise HookCatalogError("Hook catalog entry changed or became unsafe before execution.")
    parameters = validate_parameters(definition.parameters_schema, parameters)
    timeout_seconds = min(timeout_seconds or definition.timeout_seconds, definition.timeout_seconds, timeout_ceiling)
    start = time.monotonic()
    identity = execution_identity(definition, os_module=os_module, pwd_module=pwd_module)
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
    kwargs: dict[str, Any] = {}
    if identity:
        kwargs.update(identity)
    proc = subprocess.Popen(
        _argv(definition, parameters), shell=False, cwd="/", env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=(os_module.name == "posix"), **kwargs,
    )
    out: list[str] = []
    err: list[str] = []
    readers = [
        threading.Thread(target=_bounded_reader, args=(proc.stdout, out, definition.output_limit_bytes), daemon=True),
        threading.Thread(target=_bounded_reader, args=(proc.stderr, err, definition.output_limit_bytes), daemon=True),
    ]
    for reader in readers:
        reader.start()
    deadline = start + timeout_seconds
    cancelled = timed_out = False
    while proc.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.1)
    if cancelled or timed_out:
        _signal_process(proc, signal.SIGTERM, os_module)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _signal_process(proc, signal.SIGKILL, os_module)
            proc.wait()
    else:
        proc.wait()
    for reader in readers:
        reader.join(timeout=3)
    return HookResult(
        exit_code=proc.returncode if proc.returncode is not None else 1,
        duration_seconds=int(time.monotonic() - start), timed_out=timed_out,
        cancelled=cancelled, stdout=out[0] if out else "", stderr=err[0] if err else "",
    )
