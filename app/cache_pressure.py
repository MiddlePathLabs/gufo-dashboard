"""Allowlisted Gufo cache diagnostics; raw logs never enter the dashboard."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .extract import as_int, as_str

MAX_STATE_BYTES = 64 * 1024
STALE_MS = 30_000
_FIELDS = re.compile(r"(?:^|\s)([a-z_]+)=([A-Za-z0-9_.-]+)(?=\s|$)")
NUMBERS = (
    "ram_capacity_bytes",
    "snapshot_entry_limit",
    "sessions",
    "disk_capacity_bytes",
    "disk_staging_capacity_bytes",
    "ram_entry_evictions",
    "disk_lru_evictions",
    "ram_skipped",
    "disk_skipped",
    "ram_last_retained_bytes",
    "disk_last_retained_bytes",
    "ram_last_sample_ms",
    "disk_last_sample_ms",
    "window_start_ms",
    "updated_at_ms",
)
REASONS = {
    "entry_capacity",
    "byte_capacity",
    "staging_capacity",
    "lru",
    "capture_failure",
    "reservation_mismatch",
    "corrupt",
    "checksum_mismatch",
    "unsafe_file",
    "io_failure",
    "serialization_failure",
    "restore_failure",
    "unsupported",
}


class CacheLogSummary:
    """Counts within a bounded retained-log window, never inferred occupancy."""

    def __init__(self, version: str | None, level: str | None) -> None:
        self.state: dict[str, Any] = dict.fromkeys(NUMBERS)
        self.state.update(
            schema=1,
            gufo_version=as_str(version),
            log_level=level,
            ram_configured=False,
            disk_configured=False,
            events=[],
        )
        self.level = level

    def feed(self, line: str) -> None:
        try:
            stamp = line.split(" ", 1)[0]
            ts = int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000)
        except (ValueError, OverflowError):
            return
        if self.state["window_start_ms"] is None:
            self.state["window_start_ms"] = ts
        if not re.match(
            r"^\S+ \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[(?:INFO|WARN|DEBUG|ERROR)\] \[cache\] ",
            line,
        ):
            return
        fields = dict(_FIELDS.findall(line))
        event = fields.get("event")
        if event not in {
            "snapshot_cache_configured",
            "disk_cache_configured",
            "snapshot",
            "disk_cache",
        }:
            return

        def number(key: str) -> int | None:
            value = fields.get(key, "")
            return as_int(int(value)) if value.isdecimal() and len(value) <= 19 else None

        s = self.state
        if event == "snapshot_cache_configured":
            s.update(
                ram_configured=True,
                ram_capacity_bytes=number("capacity_bytes"),
                snapshot_entry_limit=number("snapshot_entries"),
                sessions=number("sessions"),
            )
            # Only the emitted entry-pressure removals can be counted.
            s["ram_entry_evictions"] = 0 if self.level in {"info", "debug", "warn"} else None
            s["ram_skipped"] = 0 if self.level in {"info", "debug", "warn"} else None
            return
        if event == "disk_cache_configured":
            s.update(
                disk_configured=True,
                disk_capacity_bytes=number("capacity_bytes"),
                disk_staging_capacity_bytes=number("staging_capacity_bytes"),
            )
            s["disk_lru_evictions"] = 0 if self.level in {"info", "debug"} else None
            s["disk_skipped"] = 0 if self.level in {"info", "debug", "warn"} else None
            return
        reason, action = fields.get("reason"), fields.get("action")
        if reason not in REASONS or action not in {"removed", "skipped"}:
            return
        ram = event == "snapshot"
        prefix = "ram" if ram else "disk"
        s[f"{prefix}_configured"] = True
        s[f"{prefix}_last_retained_bytes"] = number("retained_bytes")
        s[f"{prefix}_last_sample_ms"] = ts
        s[f"{prefix}_capacity_bytes"] = number("capacity_bytes")
        if action == "removed":
            if (ram and reason != "entry_capacity") or (not ram and reason != "lru"):
                return
            key = "ram_entry_evictions" if ram else "disk_lru_evictions"
        else:
            key = f"{prefix}_skipped"
        s[key] = (s[key] or 0) + 1
        s["events"].append(
            {
                "ts_ms": ts,
                "tier": prefix,
                "action": action,
                "reason": reason,
                "bytes": number("bytes" if ram else "file_bytes"),
            }
        )
        s["events"] = s["events"][-20:]


def read_cache_pressure(path: str, now: int) -> dict[str, Any]:
    result: dict[str, Any] = {"available": False, "status": "not_configured"}
    if not path:
        return result
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise ValueError
        obj = json.loads(raw)
        if not isinstance(obj, dict) or obj.get("schema") != 1:
            raise ValueError
    except (OSError, ValueError, RecursionError):
        return {"available": False, "status": "unavailable"}
    data = {key: as_int(obj.get(key)) for key in NUMBERS}
    updated = data["updated_at_ms"]
    fresh = updated is not None and 0 <= now - updated <= STALE_MS
    result.update(
        data,
        available=fresh and obj.get("observer_ok") is True,
        status="ready" if fresh and obj.get("observer_ok") is True else "stale",
        gufo_version=as_str(obj.get("gufo_version")),
        log_level=obj.get("log_level")
        if obj.get("log_level") in ("debug", "info", "warn", "error")
        else None,
        ram_configured=obj.get("ram_configured") is True,
        disk_configured=obj.get("disk_configured") is True,
        events=[],
    )
    events = obj.get("events")
    if isinstance(events, list):
        for event in events[-20:]:
            if (
                not isinstance(event, dict)
                or event.get("tier") not in ("ram", "disk")
                or event.get("action") not in ("removed", "skipped")
                or not isinstance(event.get("reason"), str)
                or event.get("reason") not in REASONS
            ):
                continue
            ts = as_int(event.get("ts_ms"))
            if ts is not None:
                result["events"].append(
                    {
                        "ts_ms": ts,
                        "tier": event["tier"],
                        "action": event["action"],
                        "reason": event["reason"],
                        "bytes": as_int(event.get("bytes")),
                    }
                )
    return result
