"""Interactive password and destination credential prompts."""

from __future__ import annotations

import getpass
import sys

from vecta_agent.sftp import _destination_kind

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


def prompt_destination_credentials(destination: str) -> dict[str, str]:
    """Prompt for the credential fields required by a destination kind."""
    prompts = _DESTINATION_PROMPTS.get(_destination_kind(destination), ())
    entries: dict[str, str] = {}
    for key, label in prompts:
        value = getpass.getpass(f"{label} ({key}): ")
        if not value:
            print(f"Error: {key} is required for this destination type.", file=sys.stderr)
            raise SystemExit(1)
        entries[key] = value
    return entries


def prompt_password_twice() -> str:
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


def confirm_password_saved() -> None:
    """Require acknowledgment that a newly generated repo password was saved."""
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
