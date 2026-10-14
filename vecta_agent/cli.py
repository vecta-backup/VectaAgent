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


from vecta_agent.repository_cli import (
    _already_initialized,
    _resolve_password,
    run_repo_init,
)
from vecta_agent.sftp import (
    _check_sftp_connectivity,
    _destination_kind,
    _print_sftp_fix_commands,
    _sftp_connection,
    _validate_sftp_destination,
)
from vecta_agent.setup import (
    _DESTINATION_PROMPTS,
    _confirm_password_saved,
    _prompt_destination_credentials,
    _prompt_password_twice,
    run_setup,
)


# ---------------------------------------------------------------------------


def run_version(_args: argparse.Namespace) -> None:
    print(__version__)


def run_update(args: argparse.Namespace) -> None:
    update.run_update(requested_version=args.version, check_only=args.check)


from vecta_agent.hook_cli import (
    _hook_entry_toml,
    _prompt_value,
    _prompt_yes_no,
    _toml_value,
    run_hooks_add,
    run_hooks_list,
    run_hooks_publish,
    run_hooks_validate,
)


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
