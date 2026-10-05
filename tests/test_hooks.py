import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from vecta_agent import hooks


def _catalog_text(executable: Path, *, extra=""):
    executable_text = executable.as_posix()
    return f'''[[hooks]]
id = "safe-cleanup"
name = "Safe cleanup"
description = "Clean a selected temporary marker."
phases = ["pre", "post"]
executable = "{executable_text}"
argv = ["--target", "${{target}}"]
run_as = "vecta-hook"
requires_root = false
timeout_seconds = 15
output_limit_bytes = 128
parameters_schema = {{ type = "object", properties = {{ target = {{ type = "string", minLength = 1, maxLength = 40, pattern = "[A-Za-z0-9_-]+" }} }}, required = ["target"], additionalProperties = false }}
{extra}'''


def test_catalog_exposes_safe_metadata_without_paths(tmp_path, monkeypatch, mock_hook_account):
    executable = tmp_path / "hook"
    executable.write_text("trusted local executable")
    catalog_path = tmp_path / "hooks.toml"
    catalog_path.write_text(_catalog_text(executable))
    monkeypatch.setattr(hooks, "_safe_root_controlled_path", lambda *a, **kw: True)

    catalog = hooks.load_catalog(catalog_path)
    metadata = hooks.capability_metadata(catalog)[0]
    assert metadata == {
        "id": "safe-cleanup",
        "name": "Safe cleanup",
        "description": "Clean a selected temporary marker.",
        "phases": ["pre", "post"],
        "parameters_schema": catalog["safe-cleanup"].parameters_schema,
        "requires_root": False,
    }
    assert str(executable) not in repr(metadata)


def test_catalog_pattern_schema_is_supported_and_bounded():
    schema = {
        "type": "object",
        "properties": {
            "target": {
                "type": "string", "maxLength": 40,
                "pattern": "[A-Za-z0-9_-]+",
            },
        },
        "required": ["target"],
        "additionalProperties": False,
    }
    assert hooks._validate_schema(schema) == schema
    with pytest.raises(hooks.HookCatalogError, match="pattern"):
        hooks._validate_schema({
            **schema,
            "properties": {"target": {"type": "string", "pattern": "x" * 129}},
        })


def test_catalog_rejects_duplicate_ids_and_unsafe_paths(
    tmp_path, monkeypatch, mock_hook_account
):
    executable = tmp_path / "hook"
    executable.touch()
    catalog_path = tmp_path / "hooks.toml"
    catalog_path.write_text(_catalog_text(executable) + "\n" + _catalog_text(executable))
    monkeypatch.setattr(hooks, "_safe_root_controlled_path", lambda *a, **kw: True)
    with pytest.raises(hooks.HookCatalogError, match="unique"):
        hooks.load_catalog(catalog_path)
    monkeypatch.setattr(hooks, "_safe_root_controlled_path", lambda *a, **kw: False)
    with pytest.raises(hooks.HookCatalogError, match="unsafe"):
        hooks.load_catalog(catalog_path)


def test_catalog_rejects_paths_in_reported_display_metadata(tmp_path, monkeypatch):
    executable = tmp_path / "hook"
    executable.touch()
    catalog_path = tmp_path / "hooks.toml"
    catalog_path.write_text(_catalog_text(executable).replace(
        "Clean a selected temporary marker.", "Run /usr/local/bin/maintenance",
    ))
    monkeypatch.setattr(hooks, "_safe_root_controlled_path", lambda *a, **kw: True)

    with pytest.raises(hooks.HookCatalogError, match="display metadata"):
        hooks.load_catalog(catalog_path)


def test_hook_selection_rejects_unknown_phase_and_invalid_parameters():
    definition = hooks.HookDefinition(
        id="h1", name="hook", description="description", phases=("pre",),
        executable="/trusted/hook", argv=("${target}",),
        parameters_schema={"type": "object", "properties": {"target": {"type": "string", "pattern": "[a-z]+"}}, "required": ["target"]},
        run_as="vecta-hook", requires_root=False, timeout_seconds=5, output_limit_bytes=100,
    )
    catalog = {"h1": definition}
    with pytest.raises(hooks.HookCatalogError, match="unknown"):
        hooks.validate_selection({"hook_id": "removed", "parameters": {}}, catalog, "pre")
    with pytest.raises(hooks.HookCatalogError, match="not allowed"):
        hooks.validate_selection({"hook_id": "h1", "parameters": {"target": "ok"}}, catalog, "post")
    with pytest.raises(hooks.HookCatalogError, match="schema"):
        hooks.validate_selection({"hook_id": "h1", "parameters": {"target": "--bad"}}, catalog, "pre")
    with pytest.raises(hooks.HookCatalogError, match="only hook_id"):
        hooks.validate_selection({"hook_id": "h1", "parameters": {}, "argv": ["rm"]}, catalog, "pre")


def test_posix_privilege_drop_clears_supplementary_groups(monkeypatch):
    monkeypatch.setattr(hooks, "os", SimpleNamespace(name="posix", geteuid=lambda: 0))
    monkeypatch.setattr(hooks, "pwd", SimpleNamespace(getpwnam=lambda _: SimpleNamespace(pw_uid=120, pw_gid=130)))
    definition = hooks.HookDefinition(
        "h", "hook", "desc", ("pre",), "/trusted/hook", (),
        {"type": "object", "properties": {}}, "vecta-hook", False, 10, 20,
    )
    assert hooks._execution_identity(definition) == {"user": 120, "group": 130, "extra_groups": ()}


class _FakeProcess:
    def __init__(self, **kwargs):
        self.returncode = 0
        self.stdout = io.BytesIO(b"o" * 500)
        self.stderr = io.BytesIO(b"e" * 500)
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def test_execution_is_argv_only_has_clean_environment_and_bounds_output(
    monkeypatch, mock_hook_account
):
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return _FakeProcess(**kwargs)

    monkeypatch.setattr(hooks.subprocess, "Popen", fake_popen)
    definition = hooks.HookDefinition(
        "h", "hook", "desc", ("pre",), "/trusted/hook", ("${target}",),
        {"type": "object", "properties": {"target": {"type": "string"}}},
        "vecta-hook", False, 10, 32,
    )
    result = hooks.execute_hook(definition, {"target": "$(touch nope)"})
    assert captured["argv"] == ["/trusted/hook", "$(touch nope)"]
    assert captured["shell"] is False
    assert "PGPASSWORD" not in captured["env"]
    assert result.stdout == "o" * 32
    assert result.stderr == "e" * 32


def test_cancellation_terminates_and_reaps_direct_hook(monkeypatch):
    process = _FakeProcess()
    process.returncode = None
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(hooks.os, "name", "nt")
    definition = hooks.HookDefinition(
        "h", "hook", "desc", ("pre",), "hook", (),
        {"type": "object", "properties": {}}, "vecta-hook", False, 10, 20,
    )
    import threading

    cancelled = threading.Event()
    cancelled.set()
    result = hooks.execute_hook(definition, {}, cancel_event=cancelled)
    assert result.cancelled
    assert process.terminated


def test_hook_timeout_is_reported_after_kill_and_wait(monkeypatch):
    process = _FakeProcess()
    process.returncode = None
    terminated = []
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(hooks.os, "name", "nt")
    process.poll = lambda: None
    process.terminate = lambda: terminated.append("term")
    process.wait = lambda timeout=None: setattr(process, "returncode", -15) or process.returncode
    definition = hooks.HookDefinition(
        "h", "hook", "desc", ("pre",), "hook", (),
        {"type": "object", "properties": {}}, "vecta-hook", False, 1, 20,
    )
    monkeypatch.setattr(hooks.time, "monotonic", lambda: 0)
    monkeypatch.setattr(hooks.time, "sleep", lambda _: None)
    # Advance monotonic on its successive reads so the fixed deadline is reached.
    clock = iter([0, 2, 2, 2])
    monkeypatch.setattr(hooks.time, "monotonic", lambda: next(clock, 2))

    result = hooks.execute_hook(definition, {})

    assert result.timed_out
    assert terminated == ["term"]
    assert process.returncode == -15
