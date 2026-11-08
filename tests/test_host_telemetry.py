from __future__ import annotations

from pathlib import Path

from app.host_telemetry import HostTelemetry
from tests.conftest import Dash


def test_host_snapshot_and_cpu_delta(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    drm = tmp_path / "drm"
    proc.mkdir()
    device = drm / "card0" / "device"
    device.mkdir(parents=True)
    (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 250 kB\n")
    (proc / "stat").write_text("cpu  10 0 10 80 0 0 0 0\n")
    (device / "gpu_busy_percent").write_text("45\n")
    (device / "mem_info_vram_used").write_text("100\n")
    (device / "mem_info_vram_total").write_text("400\n")
    monitor = HostTelemetry(proc, drm, min_cpu_interval=0)
    first = monitor.snapshot()
    assert first["cpu_pct"] is None
    assert first["memory"] == {"total_bytes": 1024000, "used_bytes": 768000}
    assert first["gpu"]["busy_pct"] == 45
    assert first["gpu"]["vram_used_bytes"] == 100

    (proc / "stat").write_text("cpu  30 0 30 140 0 0 0 0\n")
    assert monitor.snapshot()["cpu_pct"] == 40.0


def test_missing_gpu_is_unavailable(tmp_path: Path) -> None:
    monitor = HostTelemetry(tmp_path / "no-proc", tmp_path / "no-drm")
    assert monitor.snapshot() == {
        "cpu_pct": None,
        "memory": {"total_bytes": None, "used_bytes": None},
        "gpu": None,
    }


def test_host_status_route(dash: Dash) -> None:
    with dash.client() as client:
        response = client.get("/api/host-status")
    assert response.status_code == 200
    assert set(response.json()) == {"cpu_pct", "memory", "gpu"}


def test_cpu_samples_are_shared_within_interval(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text("cpu  10 0 10 80 0 0 0 0\n")
    monitor = HostTelemetry(proc, tmp_path / "no-drm", min_cpu_interval=3600)
    assert monitor.snapshot()["cpu_pct"] is None
    # A second client polling right away must not reset the baseline.
    (proc / "stat").write_text("cpu  30 0 30 140 0 0 0 0\n")
    assert monitor.snapshot()["cpu_pct"] is None
    assert monitor._previous_cpu == (100, 80)
