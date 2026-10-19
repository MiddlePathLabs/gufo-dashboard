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
    "ram_automatic_bytes",
    "ram_max_bytes",
    "snapshot_entry_limit",
    "sessions",
    "disk_capacity_bytes",
    "disk_staging_capacity_bytes",
    "backing_committed_bytes",
    "backing_pinned_bytes",
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
            schema=2,
            gufo_version=as_str(version),
            log_level=level,
            ram_configured=False,
            disk_configured=False,
            config_source=None,
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
        schema = fields.get("schema")
        component = schema == "component-cache-v1"
        if event not in {
            "snapshot_cache_configured",
            "disk_cache_configured",
            "snapshot",
            "disk_cache",
        } and not (component and event in {"configured", "backing_prepared"}):
            return

        def number(key: str) -> int | None:
            value = fields.get(key, "")
            return as_int(int(value)) if value.isdecimal() and len(value) <= 19 else None

        s = self.state
        if component and event == "configured":
            # Gufo 0.13+ replaces the two startup events with one merged
            # component-cache-v1 line covering both tiers.
            s.update(
                ram_configured=True,
                ram_capacity_bytes=number("capacity_bytes"),
                ram_automatic_bytes=number("automatic_bytes"),
                ram_max_bytes=number("max_bytes"),
                snapshot_entry_limit=number("snapshot_entries"),
                sessions=number("sessions"),
                disk_configured=True,
                disk_capacity_bytes=number("disk_capacity_bytes"),
                disk_staging_capacity_bytes=number("staging_capacity_bytes"),
                config_source="window",
            )
            s["ram_entry_evictions"] = 0 if self.level in {"info", "debug", "warn"} else None
            s["ram_skipped"] = 0 if self.level in {"info", "debug", "warn"} else None
            s["disk_lru_evictions"] = 0 if self.level in {"info", "debug"} else None
            s["disk_skipped"] = 0 if self.level in {"info", "debug", "warn"} else None
            return
        if component and event == "backing_prepared":
            s.update(
                backing_committed_bytes=number("committed_bytes"),
                backing_pinned_bytes=number("long_term_pinned_bytes"),
            )
            return
        if event == "snapshot_cache_configured":
            s.update(
                ram_configured=True,
                ram_capacity_bytes=number("capacity_bytes"),
                # Gufo 0.7 also emits the automatic budget an unset
                # --cache-ram-bytes would pick and the explicit-limit maximum.
                ram_automatic_bytes=number("automatic_bytes"),
                ram_max_bytes=number("max_bytes"),
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


_CARRIED_FIELDS = (
    "ram_configured",
    "disk_configured",
    "ram_capacity_bytes",
    "ram_automatic_bytes",
    "ram_max_bytes",
    "snapshot_entry_limit",
    "sessions",
    "disk_capacity_bytes",
    "disk_staging_capacity_bytes",
    "backing_committed_bytes",
    "backing_pinned_bytes",
)


def carry_forward_config(state: dict[str, Any], previous: Any) -> None:
    """Reuse the previous snapshot's config when startup events scrolled out.

    The config startup lines exist once per container; once request logs push
    them past the observer's tail window, event counts stay current but the
    capacities would blank. Reuse them from the previous snapshot while the
    Gufo version is unchanged, so a container swap cannot stitch configs.
    """
    if state.get("ram_configured") or state.get("disk_configured"):
        return
    if not isinstance(previous, dict) or previous.get("gufo_version") != state.get("gufo_version"):
        return
    if not previous.get("ram_configured") and not previous.get("disk_configured"):
        return
    for key in _CARRIED_FIELDS:
        value = previous.get(key)
        if value is not None:
            state[key] = value
    state["config_source"] = "carried"


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
        # Schema 1 predates Gufo 0.7's automatic/max RAM budget fields; both
        # remain readable so an upgrade cannot blank the panel for a tick.
        if not isinstance(obj, dict) or obj.get("schema") not in (1, 2):
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
        config_source=obj.get("config_source")
        if obj.get("config_source") in ("window", "carried")
        else None,
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
