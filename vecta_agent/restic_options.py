"""Validated Restic argv construction for repositories and backup policy."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Any

logger = logging.getLogger("vecta_agent")


def repo_options(destination: str, port: int | None) -> list[str]:
    """Extended restic options for a destination.

    SFTP with a non-default port: restic's `sftp.command` option replaces the
    default ssh invocation ENTIRELY and restic does not append the sftp
    subsystem for it, so the command must carry `-s sftp` itself (restic
    sftp.go buildSSHCommand; no %h/%r placeholders exist). Only the legacy
    `sftp:user@host:/path` format is overridden — with the
    `sftp://user@host:port/path` URL format restic reads the port from the URL
    and needs no override. Ignored for non-sftp destinations — their locations
    carry ports in their URL syntax.
    """
    if port is None or not destination.startswith("sftp:"):
        return []
    if destination.startswith("sftp://"):
        return []
    connection = destination[len("sftp:"):].split(":", 1)[0]
    if not connection:
        logger.warning(
            "Could not parse the SFTP destination %r; ignoring SSH port %s. "
            "Use the sftp:user@host:/path format.",
            destination,
            port,
        )
        return []
    return ["-o", f"sftp.command=ssh -p {port} {connection} -s sftp"]


MAX_EXCLUDE_PATTERNS = 100
MAX_EXCLUDE_PATTERN_CHARS = 256
MAX_EXCLUDE_SERIALIZED_BYTES = 16 * 1024
MAX_RETENTION_VALUE = 3650


def validate_exclude_patterns(value: Any) -> list[str]:
    """Validate untrusted filesystem excludes and preserve first-seen order."""
    if not isinstance(value, list) or len(value) > MAX_EXCLUDE_PATTERNS:
        raise ValueError("exclude_patterns must be a list of at most 100 patterns.")
    patterns: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            raise ValueError("exclude_patterns must contain only strings.")
        pattern = raw.strip()
        if not pattern or len(pattern) > MAX_EXCLUDE_PATTERN_CHARS:
            raise ValueError("Each exclude pattern must be non-empty and at most 256 characters.")
        if pattern.startswith(("-", "!")) or any(unicodedata.category(char) == "Cc" for char in pattern):
            raise ValueError("Exclude patterns cannot begin with '-' or '!' or contain control characters.")
        if pattern not in seen:
            patterns.append(pattern)
            seen.add(pattern)
    if len(json.dumps(patterns, ensure_ascii=False).encode("utf-8")) > MAX_EXCLUDE_SERIALIZED_BYTES:
        raise ValueError("Serialized exclude patterns exceed 16 KiB.")
    return patterns


def validate_retention_policy(value: Any) -> dict[str, int | None] | None:
    """Validate the supported strict retention policy wire format."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("retention_policy must be an object or null.")
    allowed = {"keep_within_days", "keep_daily", "keep_weekly", "keep_monthly"}
    if set(value) - allowed:
        raise ValueError("retention_policy contains unsupported fields.")
    normalized: dict[str, int | None] = {}
    for key in allowed:
        item = value.get(key, 0 if key != "keep_within_days" else None)
        if item is None and key == "keep_within_days":
            normalized[key] = None
            continue
        if type(item) is not int or item < (1 if key == "keep_within_days" else 0) or item > MAX_RETENTION_VALUE:
            raise ValueError("Retention values must be strict integers in the allowed range.")
        normalized[key] = item
    if normalized["keep_within_days"] is not None:
        if any(normalized[f"keep_{period}"] for period in ("daily", "weekly", "monthly")):
            raise ValueError("Age-window and calendar retention rules cannot be combined.")
    elif not any(normalized[f"keep_{period}"] for period in ("daily", "weekly", "monthly")):
        raise ValueError("At least one retention rule must be positive.")
    return normalized


def forget_args(
    destination: str,
    policy: dict[str, int | None],
    port: int | None = None,
    *,
    job_id: str,
) -> list[str]:
    """Build argv from the validated, structured policy only."""
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("A job ID is required to scope retention pruning.")
    policy = validate_retention_policy(policy)
    if policy is None:
        raise ValueError("A retention policy is required to prune.")
    args = [*repo_options(destination, port), "forget", "--json", "--prune", f"--tag=job:{job_id}"]
    within = policy.get("keep_within_days")
    if within is not None:
        args += ["--keep-within", f"{within}d"]
    else:
        for period in ("daily", "weekly", "monthly"):
            count = policy.get(f"keep_{period}", 0) or 0
            if count > 0:
                args += [f"--keep-{period}", str(count)]
    args += ["--repo", destination]
    return args


_LOCK_FAILURE_PHRASES = ("already locked", "unable to create lock", "repository is locked")


def is_repository_lock_error(stderr: str) -> bool:
    """Recognize only known Restic lock acquisition failures."""
    low = stderr.lower()
    if any(phrase in low for phrase in _LOCK_FAILURE_PHRASES):
        return True
    return bool(re.search(r"lock file[^\n]*\bexists\b", low))


