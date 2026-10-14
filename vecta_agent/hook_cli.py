"""Interactive hook-catalog CLI commands."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from vecta_agent import agent, api, config, hooks


def _prompt_value(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or (default or "")


def _prompt_yes_no(label: str, *, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    answer = input(f"{label} [{suffix}]: ").strip().lower()
    if not answer:
        return default
    if answer in {"y", "yes"}:
        return True
    if answer in {"n", "no"}:
        return False
    raise hooks.HookCatalogError("Please answer yes or no.")


def _toml_value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(
            f"{key} = {_toml_value(item)}" for key, item in value.items()
        ) + " }"
    raise TypeError(f"Cannot write {type(value).__name__} as a TOML value.")


def _hook_entry_toml(entry: dict[str, object]) -> str:
    lines = ["[[hooks]]"]
    for key, value in entry.items():
        lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def run_hooks_add(_args: argparse.Namespace) -> None:
    """Interactively append a validated hook definition to the local catalog."""
    catalog_path = hooks.CATALOG_PATH
    if catalog_path.is_symlink():
        raise hooks.HookCatalogError("Hook catalog path must not be a symlink.")
    catalog = hooks.load_catalog(catalog_path)

    print("Add a locally installed executable to the Vecta hook catalog.")
    print("The executable must already exist and be protected from untrusted modification.")
    hook_id = _prompt_value("Hook ID")
    name = _prompt_value("Display name")
    description = _prompt_value("Description")
    phase_choice = _prompt_value("Allowed phase (pre, post, both)").lower()
    phase_map = {"pre": ["pre"], "post": ["post"], "both": ["pre", "post"]}
    if phase_choice not in phase_map:
        raise hooks.HookCatalogError("Phase must be pre, post, or both.")
    executable = _prompt_value("Absolute executable path")

    requires_root = _prompt_yes_no("Allow this hook to run as root?", default=False)
    run_as = "root" if requires_root else _prompt_value("Run as account", "vecta-hook")

    properties: dict[str, dict[str, str]] = {}
    required: list[str] = []
    print("Add parameters used by argv templates; leave the name blank when finished.")
    while True:
        parameter = _prompt_value("Parameter name (blank to finish)")
        if not parameter:
            break
        kind = _prompt_value("Type (string, integer, boolean)", "string").lower()
        if kind not in {"string", "integer", "boolean"}:
            raise hooks.HookCatalogError("Parameter type must be string, integer, or boolean.")
        properties[parameter] = {"type": kind}
        if _prompt_yes_no(f"Is {parameter} required?", default=True):
            required.append(parameter)

    schema: dict[str, object] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    hooks._validate_schema(schema)

    argv: list[str] = []
    print("Add fixed argv items or whole-item placeholders such as ${target}.")
    while True:
        item = _prompt_value("argv item (blank to finish)")
        if not item:
            break
        argv.append(item)

    entry: dict[str, object] = {
        "id": hook_id,
        "name": name,
        "description": description,
        "phases": phase_map[phase_choice],
        "executable": executable,
        "argv": argv,
        "parameters_schema": schema,
        "run_as": run_as,
        "requires_root": requires_root,
    }
    block = _hook_entry_toml(entry)

    if hook_id in catalog:
        raise hooks.HookCatalogError(f"Hook ID {hook_id!r} is already registered.")
    catalog_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if not hooks._safe_root_controlled_directory(catalog_path.parent):
        raise hooks.HookCatalogError("Hook catalog directory must be root-owned and not writable by group or others.")

    existing = catalog_path.read_text(encoding="utf-8") if catalog_path.exists() else ""
    contents = existing.rstrip() + ("\n\n" if existing.strip() else "") + block
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".hooks-", suffix=".toml", dir=catalog_path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as stream:
            stream.write(contents)
        os.chmod(temporary_path, 0o644)
        # Validate the full updated catalog before replacing the installed one.
        hooks.load_catalog(temporary_path)
        os.replace(temporary_path, catalog_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    print(f"Registered hook {hook_id!r} in {catalog_path}.")
    print("Run 'sudo vecta-agent hooks publish' to show it in the dashboard now.")


def run_hooks_list(_args: argparse.Namespace) -> None:
    catalog = hooks.load_catalog(hooks.CATALOG_PATH)
    if not catalog:
        print(f"No hooks registered in {hooks.CATALOG_PATH}.")
        return
    for hook_id in sorted(catalog):
        definition = catalog[hook_id]
        privilege = "root" if definition.requires_root else definition.run_as
        print(f"{hook_id} — {definition.name} ({'/'.join(definition.phases)}, runs as {privilege})")


def run_hooks_validate(_args: argparse.Namespace) -> None:
    catalog = hooks.load_catalog(hooks.CATALOG_PATH)
    print(f"Hook catalog is valid ({len(catalog)} hook{'s' if len(catalog) != 1 else ''}).")


def run_hooks_publish(_args: argparse.Namespace) -> None:
    cfg = config.load()
    client = api.ApiClient(cfg.agent_id, cfg.api_key)
    report = agent.capability_report()
    client.report_capabilities(report)
    hook_check = report["checks"]["hook_catalog_v1"]
    if hook_check["status"] == "available":
        print(f"Published {len(report['hooks'])} hook(s) and agent capabilities to Vecta.")
    else:
        print("Published agent capabilities; hooks are unavailable because the local catalog is invalid.")
