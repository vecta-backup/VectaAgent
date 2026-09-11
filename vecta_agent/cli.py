"""CLI entry point and argument parsing."""

from __future__ import annotations

import argparse
import getpass
import logging
import secrets
import sys
from pathlib import Path

from vecta_agent import __version__, agent, api, config, restic
from vecta_agent import secrets as secret_store

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
    """Shared password acquisition for `repo init` and `secret set`.

    Returns None when no password was requested/provided (e.g. --env only).
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
    profile = args.profile
    password = _resolve_password(args)
    if password is None:
        password = getpass.getpass("Repository password: ")
        confirm = getpass.getpass("Confirm repository password: ")
        if password != confirm:
            print("Error: passwords do not match.", file=sys.stderr)
            raise SystemExit(1)

    config_dir = config._config_dir()
    env = {**agent.load_restic_env(config_dir), "RESTIC_PASSWORD": password}
    if profile:
        try:
            existing = secret_store.load_profile(profile)
        except secret_store.SecretsError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        if existing:
            env.update(existing)

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
            if profile:
                secret_store.save_profile(profile, {"RESTIC_PASSWORD": password})
                saved_to = f"{config_dir / secret_store.SECRETS_FILENAME} (profile '{profile}')"
            else:
                agent.save_restic_env({"RESTIC_PASSWORD": password}, config_dir)
                saved_to = str(config_dir / "restic.env")
        except (OSError, secret_store.SecretsError) as exc:
            target = f"profile '{profile}'" if profile else str(config_dir / "restic.env")
            print(
                "Error: repository initialized, but the password could not be saved to "
                f"{target}: {exc}. Save RESTIC_PASSWORD there manually.",
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
        if profile:
            print(
                "To use an existing repository from this machine, set RESTIC_PASSWORD in "
                f"profile '{profile}' (vecta-agent secret set {profile})."
            )
        else:
            print(
                "To use an existing repository from this machine, set RESTIC_PASSWORD in "
                "restic.env to that repository's password."
            )
        return
    print(f"Error: restic init failed: {result.stderr_tail}", file=sys.stderr)
    raise SystemExit(1)


def run_secret_set(args: argparse.Namespace) -> None:
    name = args.profile
    entries: dict[str, str] = {}
    for kv in args.env or []:
        key, sep, value = kv.partition("=")
        if not key or not sep or not value:
            print(
                f"Error: --env expects KEY=VALUE (got {kv!r}).", file=sys.stderr
            )
            raise SystemExit(1)
        entries[key] = value

    password = _resolve_password(args)
    if password is None and not entries:
        password = getpass.getpass("Repository password: ")
        confirm = getpass.getpass("Confirm repository password: ")
        if password != confirm:
            print("Error: passwords do not match.", file=sys.stderr)
            raise SystemExit(1)
    if password is not None:
        entries["RESTIC_PASSWORD"] = password

    if not entries:
        print(
            "Error: provide a password or at least one --env KEY=VALUE.", file=sys.stderr
        )
        raise SystemExit(1)

    try:
        path = secret_store.save_profile(name, entries)
    except secret_store.SecretsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"Profile '{name}' saved to {path}.")
    print(f"Keys: {', '.join(sorted(entries))}")
    if "RESTIC_PASSWORD" in entries:
        print(
            "WARNING: This password is stored only on this machine and cannot be recovered. "
            "Store it in a password manager now - losing it permanently locks your backups."
        )


def run_secret_list(_args: argparse.Namespace) -> None:
    profiles = secret_store.list_profiles()
    if not profiles:
        print("No credential profiles configured. Use 'vecta-agent secret set <NAME>'.")
        return
    for name, keys in sorted(profiles.items()):
        print(f"{name}: {', '.join(keys) if keys else '(empty)'}")


def run_secret_remove(args: argparse.Namespace) -> None:
    try:
        existed = secret_store.remove_profile(args.profile)
    except secret_store.SecretsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if existed:
        print(f"Profile '{args.profile}' removed.")
    else:
        print(f"Error: profile '{args.profile}' not found.", file=sys.stderr)
        raise SystemExit(1)


_ALREADY_INITIALIZED_MARKERS = ("already initialized", "already exists")


def _already_initialized(stderr: str) -> bool:
    low = stderr.lower()
    return any(marker in low for marker in _ALREADY_INITIALIZED_MARKERS)


def run_version(_args: argparse.Namespace) -> None:
    print(__version__)


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
    repo_init_parser.add_argument(
        "--profile",
        help="Store the repository password in secrets.toml under this profile name "
        "instead of the global restic.env",
    )
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

    secret_parser = subparsers.add_parser(
        "secret", help="Manage local credential profiles (secrets never leave this machine)"
    )
    secret_sub = secret_parser.add_subparsers(dest="secret_command", required=True)

    secret_set_parser = secret_sub.add_parser(
        "set", help="Create or update a credential profile referenced by jobs"
    )
    secret_set_parser.add_argument(
        "profile", help="Profile name (matches the job's credential_profile)"
    )
    secret_set_parser.add_argument(
        "--env",
        action="append",
        metavar="KEY=VALUE",
        help="Additional secret env var for restic (e.g. AWS_ACCESS_KEY_ID=...), repeatable",
    )
    secret_password_group = secret_set_parser.add_mutually_exclusive_group()
    secret_password_group.add_argument(
        "--password",
        help="Repository password (avoid: visible in shell history; prefer the prompt or --generate)",
    )
    secret_password_group.add_argument(
        "--password-file", help="Read the repository password from a file"
    )
    secret_password_group.add_argument(
        "--generate", action="store_true", help="Generate a strong random password"
    )
    secret_set_parser.set_defaults(func=run_secret_set)

    secret_sub.add_parser("list", help="List profile names and their keys (never values)").set_defaults(
        func=run_secret_list
    )
    secret_remove_parser = secret_sub.add_parser("remove", help="Delete a credential profile")
    secret_remove_parser.add_argument("profile", help="Profile name to remove")
    secret_remove_parser.set_defaults(func=run_secret_remove)

    version_parser = subparsers.add_parser("version", help="Show version")
    version_parser.set_defaults(func=run_version)

    args = parser.parse_args(argv)
    _setup_logging()
    try:
        args.func(args)
    except SystemExit:
        raise
    except config.ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except secret_store.SecretsError as exc:
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
    except Exception as exc:  # pragma: no cover
        logger.exception("Unexpected error")
        print(f"Error: unexpected error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":  # pragma: no cover
    main()
