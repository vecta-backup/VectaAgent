"""CLI entry point and argument parsing."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from vecta_agent import __version__, agent, api, config, credentials, hooks, postgresql, restic, update

logger = logging.getLogger("vecta_agent")
_IS_POSIX = os.name == "posix"


def _setup_logging() -> None:
    # httpx logs a noisy "HTTP Request: ..." INFO line per call. The backend
    # URLs are not user-meaningful, so quiet those loggers; vecta_agent's own
    # INFO messages are the meaningful progress the user should see.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
    )


def run_register(args: argparse.Namespace) -> None:
    token: str = args.token
    client = api.ApiClient(agent_id="", api_key="")
    result = client.register(token)
    cfg = config.Config(
        agent_id=result["agent_id"],
        api_key=result["api_key"],
        name=result.get("name"),
    )
    config.save(cfg)
    print(f"Agent registered: {cfg.name or cfg.agent_id}")
    print(f"Config written to {config.config_path()}")


def run_run(_args: argparse.Namespace) -> None:
    agent.run_agent()


def _resolve_password(args) -> str | None:
    """Password acquisition for `repo init`.

    Returns None when no password was requested/provided (prompt instead).
    """
    password = args.password
    if args.password_file:
        password = Path(args.password_file).read_text(encoding="utf-8").strip()
    elif args.password is None and not args.generate:
        return None
    if args.generate:
        password = secrets.token_urlsafe(32)
        print(f"Generated repository password: {password}")
    if password is not None and not password:
        print(
            "Error: a non-empty password is required (restic rejects empty passwords).",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return password


def run_repo_init(args: argparse.Namespace) -> None:
    destination = args.destination
    password = _resolve_password(args)
    if password is None:
        password = getpass.getpass("Repository password: ")
        confirm = getpass.getpass("Confirm repository password: ")
        if password != confirm:
            print("Error: passwords do not match.", file=sys.stderr)
            raise SystemExit(1)

    config_dir = config._config_dir()
    env = {**agent.load_restic_env(config_dir), "RESTIC_PASSWORD": password}

    try:
        result = restic.init_repo(destination, env=env)
    except FileNotFoundError:
        print(
            "Error: restic executable not found on PATH. Install restic and try again.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if result.exit_code == 0:
        try:
            agent.save_restic_env({"RESTIC_PASSWORD": password}, config_dir)
            saved_to = str(config_dir / "restic.env")
        except OSError as exc:
            print(
                "Error: repository initialized, but the password could not be saved to "
                f"{config_dir / 'restic.env'}: {exc}. Save RESTIC_PASSWORD there manually.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        print(f"Repository initialized at {destination}.")
        print(f"Password saved to {saved_to}.")
        print(
            "WARNING: This password is stored only on this machine and cannot be recovered. "
            "Store it in a password manager now - losing it permanently locks your backups."
        )
        return
    if _already_initialized(result.stderr_tail):
        print(f"Repository at {destination} is already initialized.")
        print(
            "To use an existing repository from this machine, set RESTIC_PASSWORD in "
            "restic.env to that repository's password."
        )
        return
    if result.timed_out:
        print(
            "Error: repository initialization timed out after "
            f"{restic.PROBE_TIMEOUT_SECONDS}s. Check your network connection and "
            "destination credentials.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(f"Error: restic init failed: {result.stderr_tail}", file=sys.stderr)
    raise SystemExit(1)


# --- `vecta-agent setup` ---------------------------------------------------

# Prompted credential fields per destination kind: (env var, prompt label).
# SFTP is absent on purpose: restic shells out to ssh and can only use SSH
# keys/agent auth — there is no restic-consumable SFTP password to store.
_DESTINATION_PROMPTS: dict[str, tuple[tuple[str, str], ...]] = {
    "s3": (
        ("AWS_ACCESS_KEY_ID", "S3 access key ID"),
        ("AWS_SECRET_ACCESS_KEY", "S3 secret access key"),
    ),
    "b2": (
        ("B2_ACCOUNT_ID", "B2 account ID"),
        ("B2_ACCOUNT_KEY", "B2 account key"),
    ),
}


def _destination_kind(destination: str) -> str:
    scheme = destination.strip().split(":", 1)[0]
    if scheme in ("s3", "b2", "sftp"):
        return scheme
    return "other"


def _sftp_connection(destination: str) -> str:
    """The user@host part of a legacy-format sftp destination.

    Raises SystemExit when the destination cannot be parsed — surfaced loudly
    instead of silently dropping the SSH port.
    """
    connection = destination.strip()[len("sftp:"):].split(":", 1)[0]
    if not connection:
        print(
            f"Error: could not parse the SFTP destination {destination!r}. "
            "Use the sftp:user@host:/path format.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return connection


def _validate_sftp_destination(destination: str) -> None:
    """Fail fast on destinations restic's parser would reject.

    restic requires a directory for both formats: the legacy format must have
    a colon before the path (sftp:user@host:/path), the URL format a path
    (sftp://user@host[:port]/path). Without it the connectivity probe would
    pass and the later restic run would die with a cryptic parse error.
    """
    d = destination.strip()
    if d.startswith("sftp://"):
        if "/" not in d[len("sftp://"):]:
            print(
                f"Error: invalid SFTP destination {destination!r}. Use the "
                "sftp://user@host[:port]/path format — a path after the host "
                "is required.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return
    if ":" not in d[len("sftp:"):]:
        print(
            f"Error: invalid SFTP destination {destination!r}. Use the "
            "sftp:user@host:/path format — a colon before the path is "
            "required.",
            file=sys.stderr,
        )
        raise SystemExit(1)


def _check_sftp_connectivity(destination: str, port: int | None) -> tuple[bool, str]:
    """Non-interactive SSH key-auth probe: exit 0 means key auth works.

    Requests the sftp subsystem — exactly what restic uses — instead of an
    exec session, so sftp-only servers (ForceCommand internal-sftp) pass.
    The subsystem name is the trailing argument: ssh's -s flag is boolean,
    so `ssh -s sftp host` would try to connect to a host named "sftp".
    BatchMode=yes disables any password prompt; accept-new records a first
    contact host key (TOFU) so the later restic run does not need a prompt,
    while changed host keys still hard-fail. stdin is closed immediately so
    the sftp server sees EOF and exits; the timeout guards against servers
    that neither refuse nor close the session.
    """
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    if port is not None:
        cmd += ["-p", str(port)]
    cmd += ["-s", _sftp_connection(destination), "sftp"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=20,
        )
    except FileNotFoundError:
        print("Error: ssh executable not found on PATH.", file=sys.stderr)
        raise SystemExit(1)
    except subprocess.TimeoutExpired:
        return False, "SSH session timed out after 20 seconds."
    return result.returncode == 0, (result.stderr or "") + (result.stdout or "")


def _print_sftp_fix_commands(destination: str, port: int | None, stderr: str) -> None:
    connection = _sftp_connection(destination)
    ssh_port = str(port) if port is not None else "22"
    print("SSH key authentication to the SFTP destination failed:", file=sys.stderr)
    if stderr.strip():
        print(stderr.strip(), file=sys.stderr)
    print(
        "\nSet up SSH keys on this machine, then re-run this command:\n"
        "  ssh-keygen -t ed25519                  # skip if you already have a key\n"
        f"  ssh-copy-id -p {ssh_port} {connection}\n"
        f"  sftp -P {ssh_port} {connection}        # must connect without a password prompt\n",
        file=sys.stderr,
    )
    print("Then re-run: vecta-agent setup <job-id>", file=sys.stderr)


def _prompt_destination_credentials(destination: str) -> dict[str, str]:
    """Prompt (hidden input) for the credential fields the destination needs."""
    prompts = _DESTINATION_PROMPTS.get(_destination_kind(destination), ())
    entries: dict[str, str] = {}
    for key, label in prompts:
        value = getpass.getpass(f"{label} ({key}): ")
        if not value:
            print(f"Error: {key} is required for this destination type.", file=sys.stderr)
            raise SystemExit(1)
        entries[key] = value
    return entries


def _prompt_password_twice() -> str:
    password = getpass.getpass("Repository password: ")
    confirm = getpass.getpass("Confirm repository password: ")
    if password != confirm:
        print("Error: passwords do not match.", file=sys.stderr)
        raise SystemExit(1)
    if not password:
        print(
            "Error: a non-empty password is required (restic rejects empty passwords).",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return password


def _confirm_password_saved() -> None:
    """Blocking gate: the generated password must be acknowledged as stored."""
    while True:
        answer = input(
            "Type SAVED to confirm you have stored this password in a password manager: "
        )
        if answer.strip() == "SAVED":
            return
        print(
            "The repository cannot be recovered without this password. "
            "Type SAVED to continue."
        )


def run_setup(args: argparse.Namespace) -> None:
    job_id = args.job_id
    cfg = config.load()
    client = api.ApiClient(cfg.agent_id, cfg.api_key)
    job = client.get_job(job_id)
    destination = job["destination"]
    backup_type = job.get("backup_type", "files")
    database_config = None
    database_password: str | None = None
    if backup_type == "database":
        try:
            database_config = postgresql.validate_source_config(job.get("source_config"))
        except postgresql.PostgreSQLConfigError as exc:
            print(f"Error: invalid PostgreSQL setup configuration: {exc}", file=sys.stderr)
            raise SystemExit(1)
        if job.get("source") is not None:
            print("Error: database setup must not include a filesystem source.", file=sys.stderr)
            raise SystemExit(1)
        if postgresql.pg_dump_version() is None:
            print(
                "Error: pg_dump 9.0 or newer is required and must be available on PATH.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        if not postgresql.probe_postgresql_support():
            print(
                "Error: PostgreSQL backups require Restic 0.17.0 or newer with "
                "--stdin-from-command and --stdin-filename support.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        try:
            existing_pgpass = postgresql.validate_pgpass_file(database_config)
        except postgresql.PostgreSQLConfigError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        if existing_pgpass is None:
            database_password = getpass.getpass("PostgreSQL database password: ")
            confirm = getpass.getpass("Confirm PostgreSQL database password: ")
            if not database_password or database_password != confirm:
                print("Error: PostgreSQL passwords must be non-empty and match.", file=sys.stderr)
                raise SystemExit(1)
        print(
            "Testing PostgreSQL connection "
            f"(up to {postgresql.PG_CONNECTION_TIMEOUT_SECONDS} seconds)..."
        )
        pg_ok, pg_message = postgresql.test_connection(
            database_config,
            password=database_password,
            password_file=existing_pgpass,
        )
        if not pg_ok:
            print(f"Error: PostgreSQL connection test failed: {pg_message}", file=sys.stderr)
            raise SystemExit(1)
        print("PostgreSQL connection successful.")
    elif backup_type != "files":
        print("Error: unsupported backup type.", file=sys.stderr)
        raise SystemExit(1)
    port = job.get("port")
    kind = _destination_kind(destination)
    fingerprint = credentials.destination_fingerprint(destination)
    config_dir = config._config_dir()

    env = {**agent.load_restic_env(config_dir)}
    stored = None
    try:
        stored = credentials.load_credentials(fingerprint)
    except credentials.CredentialsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)

    prompted: dict[str, str] = {}
    if stored:
        env.update(stored)
        print(
            f"Credentials for {destination} are already configured "
            f"({credentials.credentials_path()}) - skipping credential entry."
        )
    elif kind != "sftp":
        prompted = _prompt_destination_credentials(destination)
        env.update(prompted)

    if kind == "sftp":
        # SFTP auth is SSH keys only; verify connectivity instead of prompting.
        # Always probe, including when a repository password is already stored.
        _validate_sftp_destination(destination)
        print("Testing SFTP SSH connection (20-second timeout)...")
        ok, stderr = _check_sftp_connectivity(destination, port)
        if not ok:
            _print_sftp_fix_commands(destination, port, stderr)
            raise SystemExit(1)
        print(f"SSH key authentication to {destination} verified.")

    # Probe/init needs a repo password. When none is known for this
    # destination (stored creds, restic.env, or process env), generate one for
    # the new repository — an existing repo will simply fail to open and we
    # ask for its password below.
    password_generated = False
    if not agent._has_repo_password(env):
        env["RESTIC_PASSWORD"] = secrets.token_urlsafe(32)
        password_generated = True

    destination_label = {"s3": "S3", "b2": "B2", "sftp": "SFTP"}.get(kind, "repository")
    print(
        f"Testing {destination_label} repository connection "
        f"(up to {restic.PROBE_TIMEOUT_SECONDS} seconds)..."
    )
    check = restic.check_repo(
        destination,
        env=env,
        port=port,
        timeout_seconds=restic.PROBE_TIMEOUT_SECONDS,
    )
    if check.exists is None and password_generated:
        # A generated password cannot open an existing repository: ask for
        # the existing repo's password and re-probe (attach-existing-repo path).
        print(
            f"Could not open a repository at {destination} with a fresh password. "
            "If the repository already exists, enter its password."
        )
        env["RESTIC_PASSWORD"] = _prompt_password_twice()
        password_generated = False
        print("Testing repository connection with the supplied repository password...")
        check = restic.check_repo(
            destination,
            env=env,
            port=port,
            timeout_seconds=restic.PROBE_TIMEOUT_SECONDS,
        )
    if check.exists is None:
        if check.timed_out:
            print(
                f"Error: could not verify the repository at {destination}: timed out "
                f"after {restic.PROBE_TIMEOUT_SECONDS}s. Check your network connection "
                "and destination credentials.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        message = (
            check.stderr_tail or "Could not verify repository at destination."
        ) + agent._auth_failure_hint(check.stderr_tail, job_id)
        message = agent._redact_secrets(message, env)
        print(f"Error: could not verify the repository at {destination}: {message}", file=sys.stderr)
        raise SystemExit(1)

    initialized_now = False
    if check.exists is False:
        print(
            f"Repository not initialized; initializing {destination} "
            f"(up to {restic.PROBE_TIMEOUT_SECONDS} seconds)..."
        )
        try:
            result = restic.init_repo(
                destination,
                env=env,
                port=port,
                timeout_seconds=restic.PROBE_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            print(
                "Error: restic executable not found on PATH. Install restic and try again.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        if result.exit_code != 0:
            if _already_initialized(result.stderr_tail):
                print(f"Repository at {destination} is already initialized.")
            elif result.timed_out:
                print(
                    "Error: repository initialization timed out after "
                    f"{restic.PROBE_TIMEOUT_SECONDS}s. Check your network connection "
                    "and destination credentials.",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            else:
                message = (result.stderr_tail or "Failed to initialize repository.") + (
                    agent._auth_failure_hint(result.stderr_tail, job_id)
                )
                message = agent._redact_secrets(message, env)
                print(f"Error: restic init failed: {message}", file=sys.stderr)
                raise SystemExit(1)
        else:
            initialized_now = True

    # Persist what this destination needs so future runs find it by fingerprint.
    entries = dict(prompted)
    if "RESTIC_PASSWORD" in env and (stored is None or "RESTIC_PASSWORD" not in stored):
        entries["RESTIC_PASSWORD"] = env["RESTIC_PASSWORD"]
    if entries:
        try:
            credentials.save_credentials(fingerprint, destination, entries)
        except (OSError, credentials.CredentialsError) as exc:
            print(
                "Error: the repository is ready, but the credentials could not be saved to "
                f"{credentials.credentials_path()}: {exc}. Add them there manually.",
                file=sys.stderr,
            )
            raise SystemExit(1)

    if database_config is not None:
        if database_password is not None:
            try:
                pgpass_file = postgresql.save_pgpass(database_config, database_password)
            except (OSError, postgresql.PostgreSQLConfigError) as exc:
                print(
                    "Error: destination setup completed, but PostgreSQL credentials could not be "
                    f"saved to {postgresql.pgpass_path(database_config)}: {exc}",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            print(f"PostgreSQL credentials saved locally to {pgpass_file} (mode 0600).")
        else:
            print(f"PostgreSQL credentials are already configured in {postgresql.pgpass_path(database_config)}.")

    if initialized_now and password_generated:
        print()
        print(f"Generated repository password: {env['RESTIC_PASSWORD']}")
        print(
            "WARNING: This password is stored only on this machine and cannot be recovered. "
            "Store it in a password manager now - losing it permanently locks your backups."
        )
        _confirm_password_saved()

    print()
    if initialized_now:
        print(f"Repository initialized at {destination}.")
    else:
        print(f"Repository at {destination} is ready (already existed).")
    print(f"Job {job_id} is configured and will run whenever its schedule next triggers.")


# ---------------------------------------------------------------------------


_ALREADY_INITIALIZED_MARKERS = ("already initialized", "already exists")


def _already_initialized(stderr: str) -> bool:
    low = stderr.lower()
    return any(marker in low for marker in _ALREADY_INITIALIZED_MARKERS)


def run_version(_args: argparse.Namespace) -> None:
    print(__version__)


def run_update(args: argparse.Namespace) -> None:
    update.run_update(requested_version=args.version, check_only=args.check)


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


def _require_root(args: argparse.Namespace) -> None:
    """Require root for commands that can access arbitrary backup data."""
    if not _IS_POSIX or os.geteuid() == 0 or args.allow_non_root:
        return
    raise SystemExit(
        "vecta-agent must be run as root on Linux. Re-run with sudo, or explicitly "
        "use --allow-non-root if this invocation should run as the current user."
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="vecta-agent")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--allow-non-root",
        action="store_true",
        help="Allow operational commands to run without root privileges (Linux only)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    register_parser = subparsers.add_parser("register", help="Register this machine with Vecta")
    register_parser.add_argument("--token", required=True, help="Registration token from the dashboard")
    register_parser.set_defaults(func=run_register)

    run_parser = subparsers.add_parser("run", help="Run backup jobs (single pass)")
    run_parser.set_defaults(func=run_run)

    repo_parser = subparsers.add_parser("repo", help="Manage the local restic repository")
    repo_sub = repo_parser.add_subparsers(dest="repo_command", required=True)
    repo_init_parser = repo_sub.add_parser(
        "init", help="Initialize the restic repository at DESTINATION and set its password"
    )
    repo_init_parser.add_argument("destination", help="The restic repository location")
    password_group = repo_init_parser.add_mutually_exclusive_group()
    password_group.add_argument(
        "--password",
        help="Repository password (avoid: visible in shell history; prefer the prompt or --generate)",
    )
    password_group.add_argument(
        "--password-file", help="Read the repository password from a file"
    )
    password_group.add_argument(
        "--generate", action="store_true", help="Generate a strong random password"
    )
    repo_init_parser.set_defaults(func=run_repo_init)

    setup_parser = subparsers.add_parser(
        "setup",
        help="Configure credentials for a job's destination and initialize its repository",
    )
    setup_parser.add_argument("job_id", help="The job ID from the Vecta dashboard")
    setup_parser.set_defaults(func=run_setup)

    hooks_parser = subparsers.add_parser("hooks", help="Manage the local hook catalog")
    hooks_sub = hooks_parser.add_subparsers(dest="hooks_command", required=True)
    hooks_add_parser = hooks_sub.add_parser("add", help="Interactively add a local hook")
    hooks_add_parser.set_defaults(func=run_hooks_add)
    hooks_list_parser = hooks_sub.add_parser("list", help="List locally registered hooks")
    hooks_list_parser.set_defaults(func=run_hooks_list)
    hooks_validate_parser = hooks_sub.add_parser("validate", help="Validate catalog entries and executable safety")
    hooks_validate_parser.set_defaults(func=run_hooks_validate)
    hooks_publish_parser = hooks_sub.add_parser("publish", help="Report capabilities to Vecta immediately")
    hooks_publish_parser.set_defaults(func=run_hooks_publish)

    version_parser = subparsers.add_parser("version", help="Show version")
    version_parser.set_defaults(func=run_version)

    update_parser = subparsers.add_parser(
        "update", help="Update vecta-agent to the latest GitHub release"
    )
    update_parser.add_argument(
        "--version",
        metavar="vX.Y.Z",
        help="Install a specific release instead of the latest one",
    )
    update_parser.add_argument(
        "--check",
        action="store_true",
        help="Only report whether an update is available; do not install",
    )
    update_parser.set_defaults(func=run_update)

    args = parser.parse_args(argv)
    _setup_logging()
    try:
        is_read_only_hooks_command = (
            args.command == "hooks" and args.hooks_command in {"list", "validate"}
        )
        if args.command != "version" and not is_read_only_hooks_command:
            _require_root(args)
        args.func(args)
    except SystemExit:
        raise
    except hooks.HookCatalogError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except config.ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except credentials.CredentialsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except api.AgentDeactivatedError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except api.AgentAuthError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except api.RegisterError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except api.ApiError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except update.UpdateError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except Exception as exc:  # pragma: no cover
        logger.exception("Unexpected error")
        print(f"Error: unexpected error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":  # pragma: no cover
    main()
