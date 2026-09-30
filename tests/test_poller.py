"""Prometheus parsing, status polling, counter resets and unattributed tokens."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import httpx
from starlette.responses import Response

from app import db, extract, prom, stats
from app.db import StatsWriter
from app.poller import Poller, compute_unattributed

from .conftest import MODEL, Dash, FakeGufo, ServerThread, fixture_bytes, fixture_json, make_dash


def test_prometheus_parser() -> None:
    text = fixture_bytes("metrics_after").decode() + (
        "garbage line here\n"
        'bad_value{a="1"} notanumber\n'
        'labelled{a="1",b="x y"} 3 1700000000000\n'
        "nan_metric NaN\n"
        "new_metric_total 1e3\n"
    )
    m = prom.parse_prometheus(text)
    assert m[prom.PROMPT_TOTAL] == 14049
    assert m[prom.PREDICTED_TOTAL] == 2758
    assert m[prom.PROMPT_RATE] == 14.5427
    assert m['labelled{a="1",b="x y"}'] == 3
    assert m["new_metric_total"] == 1000
    assert "nan_metric" not in m and "garbage" not in m and 'bad_value{a="1"}' not in m


# The 9 requests captured between metrics_before and metrics_after, as the
# proxy would record them (the no-usage stream gets usage via injection).
WINDOW_REQUESTS = [
    ("chat_nonstream", "chat"),
    ("chat_nonstream_cachehit", "chat"),
    ("chat_stream_include_usage", "chat_sse"),  # chat_stream_no_usage + injected usage
    ("chat_stream_include_usage", "chat_sse"),
    ("chat_vision_nonstream", "chat"),
    ("completions_stream_include_usage", "completions_sse"),
    ("responses_nonstream", "responses"),
    ("responses_stream", "responses_sse"),
    ("messages_nonstream", "messages"),
]


def _fields(name: str, kind: str) -> dict[str, Any]:
    if kind.endswith("_sse"):
        from app.sse import SSEParser

        ins = extract.StreamInspector(kind[:-4], False)
        for ev in SSEParser().feed(fixture_bytes(name)):
            if ev.data is not None:
                ins.on_data(ev.data)
        return ins.fields
    return extract.BODY_EXTRACTORS[kind](fixture_json(name))


def _record(fields: dict[str, Any], completed: int) -> dict[str, Any]:
    return extract.finalize_record(
        fields,
        endpoint="/v1/chat/completions",
        request_model=MODEL,
        is_streaming=False,
        image_count=0,
        http_status=200,
        gufo_request_id=None,
        started_at_ms=completed,
        completed_at_ms=completed,
        proxy_ttfb_ms=None,
        proxy_first_token_ms=None,
        total_request_ms=1.0,
        context_lengths={},
        cancelled=False,
    )


def _unattributed_with(tmp_path: Path, skip: int | None) -> dict[str, Any]:
    path = str(tmp_path / f"u{skip}.sqlite")
    db.init_db(path)

    async def run() -> dict[str, Any]:
        w = StatsWriter(path, 100, 90)
        p = Poller("http://x", httpx.AsyncClient(), w, lambda: 0, 5)
        p._on_metrics(prom.parse_prometheus(fixture_bytes("metrics_before").decode()), 1000)
        assert p.baseline is not None and p.baseline.ts == 1000
        conn = db.connect(path)
        for i, (name, kind) in enumerate(WINDOW_REQUESTS):
            if i != skip:
                db.insert_request(conn, _record(_fields(name, kind), 1500 + i))
        for code in ("parse_error", "model_not_found", "invalid_request", "invalid_request"):
            db.insert_request(conn, _record({"error_code": code}, 1600))
        conn.commit()
        p._on_metrics(prom.parse_prometheus(fixture_bytes("metrics_after").decode()), 2000)
        recorded = stats.unattributed_window(conn, p.baseline.ts, p.baseline.window_end)
        conn.close()
        return compute_unattributed(p.baseline, recorded, 0, 64)

    return asyncio.run(run())


def test_unattributed_zero_when_all_recorded(tmp_path: Path) -> None:
    u = _unattributed_with(tmp_path, None)
    assert (u["counter_prompt_delta"], u["counter_completion_delta"]) == (406, 144)
    assert (u["prompt_tokens"], u["completion_tokens"]) == (0, 0)
    assert u["visible"] is False


def test_unattributed_nonzero_when_row_missing(tmp_path: Path) -> None:
    u = _unattributed_with(tmp_path, 4)  # drop the vision request (111 / 16)
    assert (u["prompt_tokens"], u["completion_tokens"]) == (111, 16)
    assert u["visible"] is True
    hidden = compute_unattributed(
        type("B", (), {"prompt": 406.0, "predicted": 144.0, "ts": 0, "window_end": 1})(),  # type: ignore[arg-type]
        {"prompt_tokens": 0, "completion_tokens": 0, "rows": 0, "cancelled": 0},
        1,  # a request in flight hides the figure
        64,
    )
    assert hidden["visible"] is False


def test_baseline_waits_for_idle(tmp_path: Path) -> None:
    path = str(tmp_path / "b.sqlite")
    db.init_db(path)
    inflight = [1]

    async def run() -> None:
        p = Poller(
            "http://x", httpx.AsyncClient(), StatsWriter(path, 10, 90), lambda: inflight[0], 5
        )
        metrics = prom.parse_prometheus(fixture_bytes("metrics_before").decode())
        p._on_metrics(metrics, 1)
        assert p.baseline is None
        inflight[0] = 0
        p._on_metrics(metrics, 2)
        assert p.baseline is not None and p.baseline.ts == 2

    asyncio.run(run())


def _events(path: str) -> list[tuple[str, Any, Any]]:
    c = sqlite3.connect(path)
    try:
        return [tuple(r) for r in c.execute("SELECT kind, model, detail FROM events ORDER BY id")]
    finally:
        c.close()


def test_poller_events(tmp_path: Path, fake: FakeGufo, fake_server: ServerThread) -> None:
    path = str(tmp_path / "p.sqlite")
    db.init_db(path)
    state = {"model": MODEL}

    def override(request: Any, body: bytes) -> Any:
        if request.url.path == "/ready":
            if not fake.ready:
                return Response(b"", status_code=503)
            return Response(f'{{"status":"ready","model":"{state["model"]}"}}'.encode())
        if request.url.path == "/v1/models":
            return Response(
                f'{{"data":[{{"id":"{state["model"]}","context_length":32768}}]}}'.encode()
            )
        return None

    fake.override = override

    async def run() -> Poller:
        w = StatsWriter(path, 100, 90)
        w.start()
        async with httpx.AsyncClient() as client:
            p = Poller(fake_server.url, client, w, lambda: 0, 5, api_key="poll-key")
            await p.poll_once()
            assert p.online and p.model == MODEL and p.context_lengths[MODEL] == 32768
            assert p.ready_since_ms is not None
            fake.metrics_text = fixture_bytes("metrics_after")
            await p.poll_once()
            fake.metrics_text = fixture_bytes("metrics_before")  # lower: restart
            await p.poll_once()
            state["model"] = "other-model"
            await p.poll_once()
            fake.ready = False
            await p.poll_once()
            assert not p.online and p.ready_since_ms is None
            fake.ready = True
            await p.poll_once()
        await w.stop(5)
        return p

    p = asyncio.run(run())
    kinds = [e[0] for e in _events(path)]
    assert kinds == ["gufo_up", "counter_reset", "model_changed", "gufo_down", "gufo_up"]
    assert _events(path)[2] == ("model_changed", "other-model", MODEL)
    assert p.status()["gauges"]["last_request_decode_tps"] == 44.5378
    polled = [r for r in fake.requests if r.path in ("/ready", "/v1/models", "/metrics")]
    assert all(("authorization", "Bearer poll-key") in r.headers for r in polled)
    c = sqlite3.connect(path)
    assert c.execute("SELECT COUNT(*) FROM metrics_snapshots").fetchone()[0] >= 3


def test_status_api(tmp_path: Path, fake_server: ServerThread) -> None:
    d: Dash = make_dash(tmp_path, fake_server.url, enable_poller=True)
    try:
        import time

        deadline = time.time() + 5
        with d.client() as c:
            while True:
                s = c.get("/api/status").json()
                if s["context_length"] or time.time() > deadline:
                    break
                time.sleep(0.05)
            assert s["online"] and s["model"] == MODEL and s["context_length"] == 65536
            assert s["observed_ready_ms"] is not None
            assert s["in_flight"]["count"] == 0
            assert set(s["pipeline"]) >= {
                "stats_written",
                "stats_dropped_queue_full",
                "stats_extraction_errors",
                "stats_write_errors",
            }
            c.post("/v1/chat/completions", json={"model": MODEL, "messages": [], "max_tokens": 16})
            (row,) = d.wait_rows(1)
            assert row["context_length"] == 65536
            assert row["context_used_pct"] == 100 * 58 / 65536
            models = c.get("/api/models").json()
            assert models["current"] == MODEL and MODEL in models["models"]
    finally:
        d.server.stop()
