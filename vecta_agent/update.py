"""Self-update: fetch and install a newer vecta-agent binary from GitHub releases.

The agent ships as a single PyInstaller binary installed at /usr/local/bin/vecta-agent
(see install.sh). This module re-implements that install step in-process so
`vecta-agent update` can replace the running binary: it resolves the latest
release tag via GitHub's /releases/latest redirect, downloads the binary and
SHA256SUMS assets, verifies the checksum, and atomically swaps the file into
place (safe even while the running binary is executing, on Linux).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx

from vecta_agent import __version__

GITHUB_URL = "https://github.com/vecta-backup/VectaAgent"
RELEASES_LATEST_URL = f"{GITHUB_URL}/releases/latest"
DOWNLOAD_BASE_URL = f"{GITHUB_URL}/releases/download"
BINARY_NAME = "vecta-agent"

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_TAG_IN_REDIRECT_RE = re.compile(r"/releases/tag/(v?\d[\w.-]*?)/?$")


class UpdateError(Exception):
    """Raised when a self-update cannot be performed."""


def parse_version(tag: str) -> tuple[int, int, int] | None:
    """Parses 'v0.3.0' / '0.3.0' into a comparable tuple; None if not semver-like."""
    match = _VERSION_RE.match(tag.strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def is_newer(candidate_tag: str, current_version: str) -> bool:
    candidate = parse_version(candidate_tag)
    current = parse_version(current_version)
    if candidate is None or current is None:
        return False
    return candidate > current


def latest_version() -> str:
    """Resolves the latest release tag (e.g. 'v0.3.0') without the GitHub API.

    GitHub redirects /releases/latest to /releases/tag/<tag>; following only the
    redirect Location avoids the unauthenticated API rate limit entirely.
    """
    try:
        with httpx.Client(timeout=30.0, follow_redirects=False) as client:
            response = client.request("GET", RELEASES_LATEST_URL)
    except httpx.HTTPError as exc:
        raise UpdateError(f"Could not reach {RELEASES_LATEST_URL}: {exc}") from exc

    location = response.headers.get("location", "")
    match = _TAG_IN_REDIRECT_RE.search(location)
    if response.status_code // 100 != 3 or match is None:
        raise UpdateError("Could not determine the latest vecta-agent release from GitHub.")
    tag = match.group(1)
    if parse_version(tag) is None:
        raise UpdateError(f"Latest release tag '{tag}' is not a valid version.")
    return tag


def target_binary_path() -> str:
    """Path of the installed binary when running from the PyInstaller bundle."""
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    raise UpdateError(
        "vecta-agent is running from source, not as an installed binary; "
        "update it with 'git pull && pip install .' instead."
    )


def _require_root() -> None:
    if os.name == "posix" and os.geteuid() != 0:
        raise UpdateError(
            "Updating requires root privileges (the binary lives in /usr/local/bin). "
            "Re-run with: sudo vecta-agent update"
        )


def _download(client: Any, url: str) -> bytes:
    try:
        response = client.request("GET", url)
    except httpx.HTTPError as exc:
        raise UpdateError(f"Could not download {url}: {exc}") from exc
    if response.status_code != 200:
        raise UpdateError(f"Download failed ({response.status_code}): {url}")
    return response.content


def _expected_sha256(sums_text: str) -> str:
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].strip("*") == BINARY_NAME:
            return parts[0].lower()
    raise UpdateError(f"No checksum entry for '{BINARY_NAME}' in SHA256SUMS.")


def install(version_tag: str, target: str) -> None:
    """Downloads, checksum-verifies and atomically installs the binary at target."""
    base_url = f"{DOWNLOAD_BASE_URL}/{version_tag}"
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        binary = _download(client, f"{base_url}/{BINARY_NAME}")
        sums_bytes = _download(client, f"{base_url}/SHA256SUMS")

    expected = _expected_sha256(sums_bytes.decode("utf-8", errors="replace"))
    actual = hashlib.sha256(binary).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise UpdateError(
            "Checksum mismatch between the downloaded binary and SHA256SUMS; aborting."
        )

    tmp_path = f"{target}.new"
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(binary)
        os.chmod(tmp_path, 0o755)
        # Same-directory rename keeps this atomic on one filesystem.
        os.replace(tmp_path, target)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise UpdateError(f"Could not replace {target}: {exc}") from exc


def run_update(
    requested_version: str | None = None,
    check_only: bool = False,
) -> None:
    """Entry point for the `update` CLI subcommand."""
    current = __version__

    explicit = requested_version is not None
    if explicit:
        if parse_version(requested_version) is None:
            raise UpdateError(f"Invalid version '{requested_version}' (expected vX.Y.Z).")
        target_tag = requested_version if requested_version.startswith("v") else f"v{requested_version}"
    else:
        target_tag = latest_version()

    target_ver = parse_version(target_tag)
    current_ver = parse_version(current)
    up_to_date = (
        not explicit
        and current_ver is not None
        and target_ver is not None
        and target_ver <= current_ver
    )

    if check_only:
        if up_to_date:
            print(f"vecta-agent {current} is up to date (latest release: {target_tag.lstrip('v')}).")
        else:
            print(f"Update available: {current} -> {target_tag.lstrip('v')}.")
            print("Run 'sudo vecta-agent update' to install it.")
        return

    if up_to_date:
        print(f"vecta-agent {current} is already up to date (latest release: {target_tag.lstrip('v')}).")
        return

    target = target_binary_path()
    _require_root()

    print(f"Downloading vecta-agent {target_tag} ...")
    install(target_tag, target)
    print(f"Updated vecta-agent: {current} -> {target_tag.lstrip('v')} ({target}).")
    print("The cron job will pick up the new version on its next run.")