"""Interactive setup workflow for job destinations and credentials."""

from __future__ import annotations

import argparse
import secrets
import sys

from vecta_agent import agent, api, config, credentials, postgresql, restic
from vecta_agent.database_setup import prepare_database_setup
from vecta_agent.repository_cli import _already_initialized
from vecta_agent.sftp import (
    _check_sftp_connectivity,
    _destination_kind,
    _print_sftp_fix_commands,
    _validate_sftp_destination,
)
from vecta_agent.setup_prompts import (
    confirm_password_saved as _confirm_password_saved,
    prompt_destination_credentials as _prompt_destination_credentials,
    prompt_password_twice as _prompt_password_twice,
)

# --- `vecta-agent setup` ---------------------------------------------------


def run_setup(args: argparse.Namespace) -> None:
    job_id = args.job_id
    cfg = config.load()
    client = api.ApiClient(cfg.agent_id, cfg.api_key)
    job = client.get_job(job_id)
    destination = job["destination"]
    backup_type = job.get("backup_type", "files")
    database_config, database_password = prepare_database_setup(job, backup_type)
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
