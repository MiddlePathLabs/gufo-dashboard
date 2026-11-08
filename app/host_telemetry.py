"""Best-effort Linux host and AMD GPU readings for the live view.

These are point-in-time diagnostics, never persisted with request statistics.
"""

from __future__ import annotations

import contextlib
import threading
import time
from pathlib import Path
from typing import Any


def _number(path: Path) -> int | None:
    try:
        value = int(path.read_text().strip())
        return value if value >= 0 else None
    except (OSError, ValueError):
        return None


def _memory(path: Path) -> dict[str, int | None]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {"total_bytes": None, "used_bytes": None}
    values: dict[str, int] = {}
    for line in lines:
        key, sep, rest = line.partition(":")
        if sep and key in {"MemTotal", "MemAvailable"}:
            with contextlib.suppress(ValueError, IndexError):
                values[key] = int(rest.strip().split()[0]) * 1024
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    return {
        "total_bytes": total,
        "used_bytes": max(0, total - available)
        if total is not None and available is not None
        else None,
    }


def _cpu_ticks(path: Path) -> tuple[int, int] | None:
    try:
        first = path.read_text().splitlines()[0].split()
        ticks = [int(value) for value in first[1:]]
    except (OSError, ValueError, IndexError):
        return None
    if not first or first[0] != "cpu" or len(ticks) < 4:
        return None
    return sum(ticks), ticks[3] + (ticks[4] if len(ticks) > 4 else 0)


class HostTelemetry:
    """Host readings shared by every dashboard client.

    CPU usage is a delta between two /proc/stat samples. Samples are taken at
    most once per ``min_cpu_interval`` so several polling browsers see the same
    reading over a meaningful window instead of splitting it between them.
    """

    def __init__(
        self,
        proc: Path = Path("/proc"),
        drm: Path = Path("/sys/class/drm"),
        min_cpu_interval: float = 1.0,
    ) -> None:
        self.proc = proc
        self.drm = drm
        self.min_cpu_interval = min_cpu_interval
        self._lock = threading.Lock()
        self._previous_cpu: tuple[int, int] | None = None
        self._sampled_at: float | None = None
        self._cpu_pct: float | None = None

    def _cpu(self) -> float | None:
        with self._lock:
            now = time.monotonic()
            if self._sampled_at is not None and now - self._sampled_at < self.min_cpu_interval:
                return self._cpu_pct
            current = _cpu_ticks(self.proc / "stat")
            cpu_pct = None
            if current is not None and self._previous_cpu is not None:
                total = current[0] - self._previous_cpu[0]
                idle = current[1] - self._previous_cpu[1]
                if total > 0 and 0 <= idle <= total:
                    cpu_pct = round(100 * (total - idle) / total, 1)
            self._previous_cpu = current
            self._sampled_at = now
            self._cpu_pct = cpu_pct
            return cpu_pct

    def snapshot(self) -> dict[str, Any]:
        cpu_pct = self._cpu()

        gpu = None
        try:
            cards = sorted(self.drm.glob("card[0-9]*"))
        except OSError:
            cards = []
        for card in cards:
            device = card / "device"
            if not (device / "gpu_busy_percent").exists():
                continue
            gpu = {
                "card": card.name,
                "busy_pct": _number(device / "gpu_busy_percent"),
                "vram_used_bytes": _number(device / "mem_info_vram_used"),
                "vram_total_bytes": _number(device / "mem_info_vram_total"),
                "gtt_used_bytes": _number(device / "mem_info_gtt_used"),
                "gtt_total_bytes": _number(device / "mem_info_gtt_total"),
            }
            break
        return {"cpu_pct": cpu_pct, "memory": _memory(self.proc / "meminfo"), "gpu": gpu}
