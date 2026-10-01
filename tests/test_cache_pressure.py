"""Cache diagnostics preserve log coverage, boundedness and privacy."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from app.cache_pressure import MAX_STATE_BYTES, CacheLogSummary, read_cache_pressure
from scripts.observe_gufo_cache import log_level

from .conftest import Dash, ServerThread, make_dash

PREFIX = "2026-10-01T12:51:04.353591161Z 2026-10-01 12:51:04 [INFO] [cache] "
RAM = "event=snapshot_cache_configured sessions=2 snapshot_entries=4 capacity_bytes=14893594624"
DISK = "event=disk_cache_configured capacity_bytes=8589934592 staging_capacity_bytes=1073741824"
REMOVE = "event=snapshot action=removed reason=entry_capacity bytes=120588652 tokens=37 retained_bytes=366446316 reserved_bytes=0 capacity_bytes=14893594624"
LRU = "event=disk_cache action=removed reason=lru file_bytes=100 payload_bytes=90 tokens=37 retained_bytes=800 capacity_bytes=1000 staging_capacity_bytes=50 staging_used_bytes=0"


def test_cache_capacity_and_observed_events() -> None:
    s = CacheLogSummary("0.4.0", "info")
    for line in (RAM, DISK, REMOVE, LRU):
        s.feed(PREFIX + line)
    state = s.state
    assert state["snapshot_entry_limit"] == 4
    assert state["ram_capacity_bytes"] == 14893594624
    assert state["ram_entry_evictions"] == state["disk_lru_evictions"] == 1
    assert state["ram_skipped"] == state["disk_skipped"] == 0
    assert "snapshot_entries_used" not in state  # Gufo does not emit occupancy
    assert state["events"][0]["reason"] == "entry_capacity"
    assert state["events"][1]["reason"] == "lru"


def test_events_bounded_and_no_raw_logs() -> None:
    s = CacheLogSummary("0.4.0", "info")
    s.feed(PREFIX + RAM)
    for _ in range(25):
        s.feed(PREFIX + REMOVE + " secret=CANARY credentials=CANARY")
    s.feed(PREFIX.replace("[cache]", "[request]") + REMOVE)
    assert s.state["ram_entry_evictions"] == 25
    assert len(s.state["events"]) == 20
    assert "CANARY" not in json.dumps(s.state)
    assert "tokens" not in s.state["events"][0]


@pytest.mark.parametrize("level", ["warn", "error", None])
def test_missing_startup_logs_never_mean_zero(level: str | None) -> None:
    s = CacheLogSummary("0.4.0", level)
    assert s.state["ram_entry_evictions"] is None
    assert s.state["disk_lru_evictions"] is None
    assert s.state["snapshot_entry_limit"] is None
    # A retained warning still proves a RAM entry eviction occurred.
    s.feed(PREFIX.replace("[INFO]", "[WARN]") + REMOVE)
    assert s.state["ram_entry_evictions"] == 1
    assert s.state["ram_configured"] is True


def test_corruption_is_not_a_capacity_eviction() -> None:
    s = CacheLogSummary("0.4.0", "info")
    s.feed(PREFIX + DISK)
    s.feed(PREFIX + LRU.replace("reason=lru", "reason=corrupt"))
    assert s.state["disk_lru_evictions"] == 0
    assert s.state["events"] == []


def write_state(path: Path, now: int, **overrides: Any) -> None:
    s = CacheLogSummary("0.4.0", "info")
    s.feed(PREFIX + RAM)
    s.state.update(observer_ok=True, updated_at_ms=now, **overrides)
    path.write_text(json.dumps(s.state))


def test_reader_fresh_stale_missing(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    assert read_cache_pressure("", 1000)["status"] == "not_configured"
    assert not read_cache_pressure(str(p), 1000)["available"]
    write_state(p, 1000)
    assert read_cache_pressure(str(p), 2000)["available"]
    assert not read_cache_pressure(str(p), 31001)["available"]
    assert not read_cache_pressure(str(p), 999)["available"]
    assert read_cache_pressure(str(p), 2000)["disk_lru_evictions"] is None
    p.write_text("x" * (MAX_STATE_BYTES + 1))
    assert read_cache_pressure(str(p), 2000)["status"] == "unavailable"


def test_untrusted_snapshot_is_allowlisted(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    write_state(
        p,
        1000,
        log_level=[],
        events=[{"tier": [], "reason": [], "action": [], "ts_ms": 1}],
        ram_capacity_bytes=True,
        prompt="CANARY",
    )
    state = read_cache_pressure(str(p), 1000)
    assert state["events"] == [] and state["log_level"] is None
    assert state["ram_capacity_bytes"] is None
    assert "CANARY" not in json.dumps(state)


def test_log_level_flags() -> None:
    assert log_level(["serve", "llm"]) == "info"
    assert log_level(["serve", "--log-level", "warn"]) == "warn"
    assert log_level(["serve", "--log-level=error"]) == "error"
    assert log_level(["serve", "-v"]) == "debug"


def test_status_cache_capabilities(tmp_path: Path, fake_server: ServerThread) -> None:
    p = tmp_path / "state.json"
    write_state(p, int(time.time() * 1000))
    d: Dash = make_dash(tmp_path, fake_server.url, gufo_cache_state_path=str(p))
    try:
        with d.client() as client:
            s = client.get("/api/status").json()
            assert s["capabilities"]["cache_pressure"] is True
            assert s["cache_pressure"]["snapshot_entry_limit"] == 4
            assert s["capabilities"]["live_requests"] is False
            assert s["upstream_requests"]["deferred"] is None
    finally:
        d.server.stop()


def test_live_prefill_progress_is_transient(dash: Dash, fake: Any) -> None:
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from starlette.responses import StreamingResponse

    release_prefill = threading.Event()
    release_decode = threading.Event()
    progress = b'data: {"object":"chat.completion.chunk","choices":[{"delta":{}}],"prompt_progress":{"total":32000,"cache":24500,"processed":28000,"secret":"CANARY"}}\n\n'
    token = b'data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"hi"}}]}\n\n'

    async def body() -> Any:
        yield progress
        while not release_prefill.is_set():
            await asyncio.sleep(0.01)
        yield token
        while not release_decode.is_set():
            await asyncio.sleep(0.01)
        yield b"data: [DONE]\n\n"

    fake.override = lambda request, raw: StreamingResponse(body(), media_type="text/event-stream")

    def run_request() -> bytes:
        with dash.client() as c:
            return c.post(
                "/v1/chat/completions", json={"stream": True, "return_progress": True}
            ).content

    def wait_phase(phase: str) -> dict[str, Any]:
        deadline = time.monotonic() + 3
        with dash.client() as c:
            while time.monotonic() < deadline:
                items = c.get("/api/status").json()["in_flight"]["items"]
                if items and items[0]["phase"] == phase:
                    return items[0]
                time.sleep(0.01)
        raise AssertionError(f"No {phase} phase")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_request)
        try:
            item = wait_phase("prefill")
            assert item["prompt_progress"] == {"total": 32000, "cache": 24500, "processed": 28000}
            assert "CANARY" not in json.dumps(item)
            release_prefill.set()
            assert wait_phase("generating")["prompt_progress"] is None
        finally:
            release_prefill.set()
            release_decode.set()
        assert future.result(timeout=3) == progress + token + b"data: [DONE]\n\n"
    (row,) = dash.wait_rows(1)
    assert "prompt_progress" not in row
    with dash.client() as c:
        assert c.get("/api/status").json()["in_flight"]["items"] == []


@pytest.mark.parametrize(
    "progress",
    [
        {"total": 100, "cache": 200, "processed": 100},
        {"total": 100, "cache": 0, "processed": True},
        {"total": 100, "cache": 10, "processed": 5},
        {"total": 100, "cache": 0, "processed": 101},
    ],
)
def test_invalid_progress_is_ignored(progress: dict[str, Any]) -> None:
    from app.extract import StreamInspector

    inspector = StreamInspector("chat", False)
    assert inspector.on_data(json.dumps({"prompt_progress": progress})) == (False, False)
    assert inspector.prompt_progress is None
