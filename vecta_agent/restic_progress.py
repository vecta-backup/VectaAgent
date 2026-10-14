"""Restic JSON summary and progress event parsing."""

from __future__ import annotations

from typing import Any


def parse_summary(obj: dict[str, Any]) -> dict[str, Any]:
    """Extract snapshot and transfer fields from a Restic summary event."""
    result: dict[str, Any] = {
        "snapshot_id": obj.get("snapshot_id"),
        "files_processed": obj.get("total_files_processed"),
        "bytes_processed": obj.get("total_bytes_processed"),
    }
    transferred = obj.get("data_added_packed")
    if transferred is None:
        transferred = obj.get("data_added")
    result["transferred_bytes"] = transferred
    return result


def compute_progress(obj: dict[str, Any]) -> int | None:
    """Compute 0–99 progress from a Restic status event."""
    percent = obj.get("percent_done")
    if isinstance(percent, (int, float)):
        return max(0, min(int(percent * 100), 99))
    total = obj.get("total_bytes")
    done = obj.get("bytes_done")
    if isinstance(total, (int, float)) and isinstance(done, (int, float)) and total > 0:
        return max(0, min(int((done / total) * 100), 99))
    return None
