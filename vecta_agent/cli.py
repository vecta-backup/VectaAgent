"""CLI entry point and argument parsing."""

from __future__ import annotations

import argparse
import getpass
import logging
import secrets
import sys
from pathlib import Path

from vecta_agent import __version__, agent, api, config, restic

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


def run_repo_init(args: argparse.Namespace) -> None:
    destination = args.destination
    password = args.password
    if args.password_file:
        password = Path(args.password_file).read_text(encoding="utf-8").strip()
    elif args.password is None and not args.generate:
        password = getpass.getpass("Repository password: ")
        confirm = getpass.getpass("Confirm repository password: ")
        if password != confirm:
            print("Error: passwords do not match.", file=sys.stderr)
            raise SystemExit(1)
    if args.generate:
        password = secrets.token_urlsafe(32)
        print(f"Generated repository password: {password}")
    if not password:
        print(
            "Error: a non-empty password is required (restic rejects empty passwords).",
            file=sys.stderr,
        )
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
        except OSError as exc:
            print(
                "Error: repository initialized, but the password could not be saved to "
                f"{config_dir / 'restic.env'}: {exc}. Add RESTIC_PASSWORD to that file manually.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        print(f"Repository initialized at {destination}.")
        print(
            f"Password saved to {config_dir / 'restic.env'} (this one password is used "
            "for every repository on this machine; new destinations are auto-initialized)."
        )
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
