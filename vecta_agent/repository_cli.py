"""Repository initialization command and shared repository diagnostics."""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys
from pathlib import Path

from vecta_agent import agent, config, restic


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


_ALREADY_INITIALIZED_MARKERS = ("already initialized", "already exists")


def _already_initialized(stderr: str) -> bool:
    low = stderr.lower()
    return any(marker in low for marker in _ALREADY_INITIALIZED_MARKERS)
