"""SFTP destination validation and non-interactive connectivity checks."""

from __future__ import annotations

import subprocess
import sys


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


