"""Root-controlled local hook catalog and bounded hook execution."""

from __future__ import annotations

import os
import json
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


class HookCatalogError(ValueError):
    """The local hook catalog is unsafe, malformed, or inconsistent."""


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


@dataclass
class HookResult:
    exit_code: int
    duration_seconds: int
    timed_out: bool = False
    cancelled: bool = False
    stdout: str = ""
    stderr: str = ""


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


def _validate_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise HookCatalogError("parameters_schema must be an object schema.")
    if set(schema) - {"type", "properties", "required", "additionalProperties"}:
        raise HookCatalogError("parameters_schema contains unsupported schema keywords.")
    if schema.get("additionalProperties", False) is not False:
        raise HookCatalogError("Hook parameters must reject additional properties.")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict) or len(properties) > 32 or not isinstance(required, list):
        raise HookCatalogError("parameters_schema properties/required are malformed.")
    if any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", key) for key in properties):
        raise HookCatalogError("Hook parameter names are invalid.")
    if any(any(word in key.lower() for word in ("password", "secret", "token", "credential")) for key in properties):
        raise HookCatalogError("Hook parameter schemas must not request credentials or secrets.")
    if any(not isinstance(key, str) for key in required) or any(key not in properties for key in required) or len(set(required)) != len(required):
        raise HookCatalogError("Hook required parameters must name unique declared properties.")
    for key, prop in properties.items():
        if not isinstance(prop, dict) or set(prop) - {"type", "enum", "minLength", "maxLength", "minimum", "maximum", "pattern"}:
            raise HookCatalogError(f"Unsupported schema for hook parameter {key!r}.")
        kind = prop.get("type")
        if kind not in {"string", "integer", "boolean"}:
            raise HookCatalogError(f"Unsupported type for hook parameter {key!r}.")
        if "enum" in prop and (
            not isinstance(prop["enum"], list)
            or not prop["enum"]
            or len(prop["enum"]) > 64
            or any(not isinstance(value, (str, int, float, bool)) for value in prop["enum"])
            or any(isinstance(value, str) and (len(value) > 128 or "/" in value or "\\" in value) for value in prop["enum"])
        ):
            raise HookCatalogError(f"Invalid enum for hook parameter {key!r}.")
        for field in ("minLength", "maxLength"):
            bound = prop.get(field)
            if bound is not None and (kind != "string" or isinstance(bound, bool) or not isinstance(bound, int) or not 0 <= bound <= 1024):
                raise HookCatalogError(f"Invalid string length bound for hook parameter {key!r}.")
        for field in ("minimum", "maximum"):
            bound = prop.get(field)
            if bound is not None and (kind != "integer" or isinstance(bound, bool) or not isinstance(bound, int)):
                raise HookCatalogError(f"Invalid integer bound for hook parameter {key!r}.")
        if "minLength" in prop and "maxLength" in prop and prop["minLength"] > prop["maxLength"]:
            raise HookCatalogError(f"Reversed string length bounds for hook parameter {key!r}.")
        if "minimum" in prop and "maximum" in prop and prop["minimum"] > prop["maximum"]:
            raise HookCatalogError(f"Reversed integer bounds for hook parameter {key!r}.")
        if "pattern" in prop:
            if kind != "string" or not isinstance(prop["pattern"], str) or len(prop["pattern"]) > 128:
                raise HookCatalogError(f"Invalid pattern for hook parameter {key!r}.")
            try:
                re.compile(prop["pattern"])
            except (TypeError, re.error) as exc:
                raise HookCatalogError(f"Invalid pattern for hook parameter {key!r}.") from exc
    if len(json.dumps(schema, separators=(",", ":"), ensure_ascii=True).encode("utf-8")) > 8192:
        raise HookCatalogError("parameters_schema must be at most 8 KiB.")
    return schema


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


def validate_parameters(schema: dict[str, Any], parameters: Any) -> dict[str, Any]:
    if not isinstance(parameters, dict):
        raise HookCatalogError("Hook parameters must be an object.")
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    if set(parameters) - set(properties) or required - set(parameters):
        raise HookCatalogError("Hook parameters do not match the local catalog schema.")
    result: dict[str, Any] = {}
    for key, value in parameters.items():
        spec = properties[key]
        kind = spec["type"]
        if kind == "string":
            valid = isinstance(value, str) and "\x00" not in value and len(value) <= spec.get("maxLength", 1024) and len(value) >= spec.get("minLength", 0)
            if valid and "pattern" in spec:
                valid = re.fullmatch(spec["pattern"], value) is not None
        elif kind == "integer":
            valid = isinstance(value, int) and not isinstance(value, bool) and spec.get("minimum", value) <= value <= spec.get("maximum", value)
        else:
            valid = isinstance(value, bool)
        if "enum" in spec:
            valid = valid and value in spec["enum"]
        if not valid:
            raise HookCatalogError(f"Hook parameter {key!r} does not match its declared schema.")
        result[key] = value
    return result


def capability_metadata(catalog: dict[str, HookDefinition]) -> list[dict[str, Any]]:
    metadata = [catalog[key].public_metadata() for key in sorted(catalog)]
    if len(metadata) > 100 or len(json.dumps(metadata, separators=(",", ":"), ensure_ascii=True).encode("utf-8")) > 250 * 1024:
        raise HookCatalogError("Hook capability metadata exceeds the backend report limits.")
    return metadata


def _argv(definition: HookDefinition, parameters: dict[str, Any]) -> list[str]:
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


def _signal_process(proc: subprocess.Popen, sig: int) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, sig)
        elif sig == signal.SIGTERM:
            proc.terminate()
        else:
            proc.kill()
    except ProcessLookupError:
        pass


def _execution_identity(definition: HookDefinition) -> dict[str, Any]:
    if os.name != "posix":
        return {}
    if pwd is None:
        raise HookCatalogError("POSIX account lookup is unavailable.")
    entry = pwd.getpwnam(definition.run_as)
    if definition.requires_root and os.geteuid() != 0:
        raise HookCatalogError("Root-authorized hook cannot run because the agent is not root.")
    return {"user": entry.pw_uid, "group": entry.pw_gid, "extra_groups": ()}


def execute_hook(
    definition: HookDefinition,
    parameters: dict[str, Any],
    *,
    cancel_event: threading.Event | None = None,
    timeout_seconds: int | None = None,
) -> HookResult:
    """Run one hook without a shell, inherited secrets, or unbounded output."""
    if definition.catalog_path:
        current = load_catalog(Path(definition.catalog_path)).get(definition.id)
        if current != definition:
            raise HookCatalogError("Hook catalog entry changed or became unsafe before execution.")
    parameters = validate_parameters(definition.parameters_schema, parameters)
    timeout_seconds = min(timeout_seconds or definition.timeout_seconds, definition.timeout_seconds, HOOK_TIMEOUT_SECONDS)
    start = time.monotonic()
    identity = _execution_identity(definition)
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
    kwargs: dict[str, Any] = {}
    if identity:
        kwargs.update(identity)
    proc = subprocess.Popen(
        _argv(definition, parameters), shell=False, cwd="/", env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=(os.name == "posix"), **kwargs,
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
        _signal_process(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _signal_process(proc, signal.SIGKILL)
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
