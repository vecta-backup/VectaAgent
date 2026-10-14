"""Root-controlled local hook catalog and bounded hook execution."""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time as time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vecta_agent import hook_runtime
from vecta_agent.hook_runtime import HookResult

from vecta_agent.hook_schema import (
    HookCatalogError,
    _validate_schema,
    capability_metadata as capability_metadata,
    validate_parameters,
)

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 compatibility in dev tests
    import tomli as tomllib

try:
    import pwd
except ImportError:  # pragma: no cover - POSIX production target
    pwd = None  # type: ignore[assignment]

CATALOG_PATH = Path("/etc/vecta/hooks.toml")
HOOK_TIMEOUT_SECONDS = 120
HOOK_OUTPUT_LIMIT_BYTES = 16 * 1024
BUILTIN_HOOKS: dict[str, "HookDefinition"] = {}
HOOK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_PARAM_RE = re.compile(r"^\$\{([A-Za-z][A-Za-z0-9_]*)\}$")
_ALLOWED_ENTRY_KEYS = {
    "id", "name", "description", "phases", "executable", "argv",
    "parameters_schema", "run_as", "requires_root", "timeout_seconds", "output_limit_bytes",
}


@dataclass(frozen=True)
class HookDefinition:
    id: str
    name: str
    description: str
    phases: tuple[str, ...]
    executable: str
    argv: tuple[str, ...]
    parameters_schema: dict[str, Any]
    run_as: str
    requires_root: bool
    timeout_seconds: int
    output_limit_bytes: int
    catalog_path: str | None = None

    def public_metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "phases": list(self.phases),
            "parameters_schema": self.parameters_schema,
            "requires_root": self.requires_root,
        }


def _safe_root_controlled_path(path: Path, *, executable: bool = False) -> bool:
    if os.name != "posix":
        return path.is_file()
    try:
        absolute = path.absolute()
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current /= part
            info = current.lstat()
            if current.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
                return False
        info = path.lstat()
        if not path.is_file() or info.st_uid != 0 or info.st_mode & 0o022:
            return False
        if executable and not info.st_mode & 0o111:
            return False
        return True
    except OSError:
        return False


def _safe_root_controlled_directory(path: Path) -> bool:
    """Whether a directory and all of its parents are controlled by root."""
    if os.name != "posix":
        return path.is_dir()
    try:
        absolute = path.absolute()
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current /= part
            info = current.lstat()
            if current.is_symlink() or not current.is_dir() or info.st_uid != 0 or info.st_mode & 0o022:
                return False
        return True
    except OSError:
        return False


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, HookDefinition]:
    if not path.exists():
        return {}
    if not _safe_root_controlled_path(path):
        raise HookCatalogError("Hook catalog path is unsafe; expected root ownership and non-writable parents.")
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise HookCatalogError("Hook catalog is unreadable or invalid TOML.") from exc
    entries = raw.get("hooks", [])
    if not isinstance(entries, list):
        raise HookCatalogError("Hook catalog must contain [[hooks]] entries.")
    if len(entries) > 100:
        raise HookCatalogError("Hook catalog may contain at most 100 entries.")
    catalog: dict[str, HookDefinition] = dict(BUILTIN_HOOKS)
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - _ALLOWED_ENTRY_KEYS:
            raise HookCatalogError("Hook entry has unsupported fields.")
        hook_id, name, description = entry.get("id"), entry.get("name"), entry.get("description")
        phases = entry.get("phases")
        executable = entry.get("executable")
        argv = entry.get("argv", [])
        run_as = entry.get("run_as", "vecta-hook")
        requires_root = entry.get("requires_root", False)
        timeout = entry.get("timeout_seconds", HOOK_TIMEOUT_SECONDS)
        output_limit = entry.get("output_limit_bytes", HOOK_OUTPUT_LIMIT_BYTES)
        if not isinstance(hook_id, str) or not HOOK_ID_RE.fullmatch(hook_id) or hook_id in catalog:
            raise HookCatalogError("Hook IDs must be valid and unique.")
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise HookCatalogError(f"Hook {hook_id!r} has invalid display name.")
        if not isinstance(description, str) or not description.strip() or len(description) > 500:
            raise HookCatalogError(f"Hook {hook_id!r} has invalid display description.")
        unsafe_display = re.compile(r"(?:^|\s)/(?:[^\s]+)|[A-Za-z]:\\[^\s]+|://")
        if unsafe_display.search(name) or unsafe_display.search(description):
            raise HookCatalogError(f"Hook {hook_id!r} display metadata must not include paths or connection URIs.")
        if (
            not isinstance(phases, list)
            or not phases
            or any(not isinstance(phase, str) or phase not in {"pre", "post"} for phase in phases)
            or len(set(phases)) != len(phases)
        ):
            raise HookCatalogError(f"Hook {hook_id!r} has invalid phases.")
        if not isinstance(executable, str) or not Path(executable).is_absolute():
            raise HookCatalogError(f"Hook {hook_id!r} executable must be an absolute local path.")
        executable_path = Path(executable)
        if not _safe_root_controlled_path(executable_path, executable=True):
            raise HookCatalogError(f"Hook {hook_id!r} executable path is unsafe.")
        if not isinstance(argv, list) or any(not isinstance(arg, str) or "\x00" in arg for arg in argv):
            raise HookCatalogError(f"Hook {hook_id!r} argv template is invalid.")
        schema = _validate_schema(entry.get("parameters_schema", {"type": "object", "properties": {}}))
        for arg in argv:
            if "${" in arg and not _PARAM_RE.fullmatch(arg):
                raise HookCatalogError(f"Hook {hook_id!r} argv parameters must occupy a whole argument.")
            match = _PARAM_RE.fullmatch(arg)
            if match and match.group(1) not in schema.get("properties", {}):
                raise HookCatalogError(f"Hook {hook_id!r} references an undeclared parameter.")
        if not isinstance(run_as, str) or not run_as or len(run_as) > 32:
            raise HookCatalogError(f"Hook {hook_id!r} has invalid execution identity.")
        if not isinstance(requires_root, bool) or requires_root != (run_as == "root"):
            raise HookCatalogError(f"Hook {hook_id!r} root privilege selection is inconsistent.")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= HOOK_TIMEOUT_SECONDS:
            raise HookCatalogError(f"Hook {hook_id!r} timeout must be between 1 and {HOOK_TIMEOUT_SECONDS} seconds.")
        if isinstance(output_limit, bool) or not isinstance(output_limit, int) or not 1 <= output_limit <= HOOK_OUTPUT_LIMIT_BYTES:
            raise HookCatalogError(f"Hook {hook_id!r} output limit is invalid.")
        if os.name == "posix":
            try:
                assert pwd is not None
                pwd.getpwnam(run_as)
            except KeyError as exc:
                raise HookCatalogError(f"Hook {hook_id!r} execution account is not provisioned.") from exc
        catalog[hook_id] = HookDefinition(
            hook_id, name, description, tuple(phases), str(executable_path), tuple(argv),
            schema, run_as, requires_root, timeout, output_limit, str(path),
        )
    return catalog


def validate_selection(selection: Any, catalog: dict[str, HookDefinition], phase: str) -> tuple[HookDefinition, dict[str, Any]] | None:
    if selection is None:
        return None
    if not isinstance(selection, dict) or set(selection) != {"hook_id", "parameters"}:
        raise HookCatalogError(f"{phase}_hook must contain only hook_id and parameters.")
    hook_id, parameters = selection.get("hook_id"), selection.get("parameters")
    if not isinstance(hook_id, str) or hook_id not in catalog:
        raise HookCatalogError(f"Selected {phase}-hook is unknown or no longer installed.")
    definition = catalog[hook_id]
    if phase not in definition.phases:
        raise HookCatalogError(f"Hook {hook_id!r} is not allowed in the {phase} phase.")
    values = validate_parameters(definition.parameters_schema, parameters)
    return definition, values




def _signal_process(proc: subprocess.Popen, sig: int) -> None:
    hook_runtime._signal_process(proc, sig, os)


def _execution_identity(definition: HookDefinition) -> dict[str, Any]:
    return hook_runtime.execution_identity(definition, os_module=os, pwd_module=pwd)


def execute_hook(
    definition: HookDefinition,
    parameters: dict[str, Any],
    *,
    cancel_event: threading.Event | None = None,
    timeout_seconds: int | None = None,
) -> HookResult:
    """Run one hook without a shell, inherited secrets, or unbounded output."""
    return hook_runtime.execute_hook(
        definition,
        parameters,
        cancel_event=cancel_event,
        timeout_seconds=timeout_seconds,
        load_catalog=load_catalog,
        os_module=os,
        pwd_module=pwd,
        timeout_ceiling=HOOK_TIMEOUT_SECONDS,
    )
