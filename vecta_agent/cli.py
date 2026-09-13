"""CLI entry point and argument parsing."""

from __future__ import annotations

import argparse
import getpass
import logging
import secrets
import subprocess
import sys
from pathlib import Path

from vecta_agent import __version__, agent, api, config, credentials, restic, update

logger = logging.getLogger("vecta_agent")


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


def _check_sftp_connectivity(destination: str, port: int | None) -> tuple[bool, str]:
    """Non-interactive SSH key-auth probe: exit 0 means key auth works.

    BatchMode=yes disables any password prompt; accept-new records a first
    contact host key (TOFU) so the later restic run does not need a prompt,
    while changed host keys still hard-fail.
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
    cmd += [_sftp_connection(destination), "exit"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        print("Error: ssh executable not found on PATH.", file=sys.stderr)
        raise SystemExit(1)
    return result.returncode == 0, result.stderr


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
        f"  ssh -p {ssh_port} {connection}         # must log in without a password prompt\n",
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
    elif kind == "sftp":
        # SFTP auth is SSH keys only; verify connectivity instead of prompting.
        ok, stderr = _check_sftp_connectivity(destination, port)
        if not ok:
            _print_sftp_fix_commands(destination, port, stderr)
            raise SystemExit(1)
        print(f"SSH key authentication to {destination} verified.")
    else:
        prompted = _prompt_destination_credentials(destination)
        env.update(prompted)

    # Probe/init needs a repo password. When none is known for this
    # destination (stored creds, restic.env, or process env), generate one for
    # the new repository — an existing repo will simply fail to open and we
    # ask for its password below.
    password_generated = False
    if not agent._has_repo_password(env):
        env["RESTIC_PASSWORD"] = secrets.token_urlsafe(32)
        password_generated = True

    check = restic.check_repo(destination, env=env, port=port)
    if check.exists is None and password_generated:
        # A generated password cannot open an existing repository: ask for
        # the existing repo's password and re-probe (attach-existing-repo path).
        print(
            f"Could not open a repository at {destination} with a fresh password. "
            "If the repository already exists, enter its password."
        )
        env["RESTIC_PASSWORD"] = _prompt_password_twice()
        password_generated = False
        check = restic.check_repo(destination, env=env, port=port)
    if check.exists is None:
        message = (
            check.stderr_tail or "Could not verify repository at destination."
        ) + agent._auth_failure_hint(check.stderr_tail, job_id)
        print(f"Error: could not verify the repository at {destination}: {message}", file=sys.stderr)
        raise SystemExit(1)

    initialized_now = False
    if check.exists is False:
        try:
            result = restic.init_repo(destination, env=env, port=port)
        except FileNotFoundError:
            print(
                "Error: restic executable not found on PATH. Install restic and try again.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        if result.exit_code != 0:
            if _already_initialized(result.stderr_tail):
                print(f"Repository at {destination} is already initialized.")
            else:
                message = (result.stderr_tail or "Failed to initialize repository.") + (
                    agent._auth_failure_hint(result.stderr_tail, job_id)
                )
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="vecta-agent")
    parser.add_argument("--version", action="version", version=__version__)
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
        args.func(args)
    except SystemExit:
        raise
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