"""Gufo 0.4.0 captures: live counters, cached work and streaming progress."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.responses import Response

from app import db, extract, prom, stats
from app.db import StatsWriter
from app.poller import Poller, compute_unattributed
from app.sse import SSEParser

from .conftest import Dash, FakeGufo
from .test_poller import WINDOW_REQUESTS, _record

FIXTURES = Path(__file__).parent / "fixtures" / "gufo" / "v0.4.0"


def fields(name: str, kind: str) -> dict[str, Any]:
    if kind.endswith("_sse"):
        inspector = extract.StreamInspector(kind[:-4], False)
        for event in SSEParser().feed((FIXTURES / f"{name}.sse").read_bytes()):
            if event.data is not None:
                inspector.on_data(event.data)
        return inspector.fields
    return extract.BODY_EXTRACTORS[kind](json.loads((FIXTURES / f"{name}.json").read_bytes()))


@pytest.mark.parametrize("skip", [None, 4])
def test_prefill_counter_reconciliation(tmp_path: Path, skip: int | None) -> None:
    path = str(tmp_path / "stats.sqlite")
    db.init_db(path)
    conn = db.connect(path)
    for i, (name, kind) in enumerate(WINDOW_REQUESTS):
        if i != skip:
            db.insert_request(conn, _record(fields(name, kind), 1500 + i))
    conn.commit()
    recorded = stats.unattributed_window(conn, 1000, 2000, prompt_excludes_cached=True)
    conn.close()

    async def run() -> None:
        async with httpx.AsyncClient() as client:
            p = Poller("http://unused", client, StatsWriter(path, 100, 90), lambda: 0, 5)
            for name, ts in (("metrics_before", 1000), ("metrics_after", 2000)):
                text = (FIXTURES / f"{name}.txt").read_text()
                assert prom.prompt_excludes_cached(text)
                p._on_metrics(prom.parse_prometheus(text), ts, prompt_excludes_cached=True)
            assert p.baseline is not None and p.baseline.prompt_excludes_cached
            result = compute_unattributed(p.baseline, recorded, 0, 64)
            assert result["counter_prompt_delta"] == 154
            assert result["counter_completion_delta"] == 144
            missing = fields("chat_vision_nonstream", "chat") if skip is not None else {}
            assert result["prompt_tokens"] == missing.get("prefill_tokens", 0)
            assert result["completion_tokens"] == missing.get("completion_tokens", 0)
            assert result["visible"] == (skip is not None)

    asyncio.run(run())


def test_prefill_fallback_and_legacy_units(tmp_path: Path) -> None:
    path = str(tmp_path / "fallback.sqlite")
    db.init_db(path)
    conn = db.connect(path)
    for values in (
        {"prompt_tokens": 100, "cached_tokens": 80},
        {"prompt_tokens": 100, "cached_tokens": 100, "prefill_tokens": 0},
        {"prompt_tokens": 100, "cached_tokens": 80, "prefill_tokens": 7},
    ):
        db.insert_request(conn, _record(values, 1500))
    conn.commit()
    assert stats.unattributed_window(conn, 1000, 2000)["prompt_tokens"] == 300
    assert (
        stats.unattributed_window(conn, 1000, 2000, prompt_excludes_cached=True)["prompt_tokens"]
        == 27
    )
    conn.close()


@pytest.mark.parametrize("busy_metric", [prom.REQUESTS_PROCESSING, prom.REQUESTS_DEFERRED])
def test_direct_requests_delay_baseline_and_unit_switch(tmp_path: Path, busy_metric: str) -> None:
    async def run() -> None:
        async with httpx.AsyncClient() as client:
            p = Poller(
                "http://unused",
                client,
                StatsWriter(str(tmp_path / "x.sqlite"), 10, 90),
                lambda: 0,
                5,
            )
            samples = {prom.PROMPT_TOTAL: 100.0, prom.PREDICTED_TOTAL: 100.0}
            p._on_metrics(samples, 1)
            assert p.baseline is not None
            p._on_metrics({**samples, busy_metric: 1.0}, 2, prompt_excludes_cached=True)
            assert p.baseline is None and p.upstream_busy
            p._on_metrics({**samples, busy_metric: 0.0}, 3, prompt_excludes_cached=True)
            assert p.baseline is not None and p.baseline.ts == 3
            assert p.baseline.prompt == p.baseline.predicted == 0
            p._set_online(False, None, 4)
            assert p.status()["upstream_requests"] == {"processing": None, "deferred": None}

    asyncio.run(run())


def test_nonfinite_metrics_are_ignored() -> None:
    assert prom.parse_prometheus("x +Inf\ny -Inf\nz NaN\na 2") == {"a": 2.0}


@pytest.mark.parametrize("kind", ["chat", "completions", "responses"])
def test_progress_and_keepalive_passthrough(dash: Dash, fake: FakeGufo, kind: str) -> None:
    raw = (FIXTURES / f"{kind}_stream_progress.sse").read_bytes()
    # Keepalive comments are protocol events, never generated tokens.
    raw = b": keepalive\n\n" + raw
    inspector = extract.StreamInspector(kind, False)
    progress_seen = False
    first_tokens = 0
    for event in SSEParser().feed(raw):
        if event.data is None:
            continue
        drop, first = inspector.on_data(event.data)
        assert not drop
        first_tokens += first
        if '"prompt_progress"' in event.data:
            progress_seen = True
            assert not first
            assert not inspector.first_token_seen
    assert progress_seen and first_tokens == 1

    def override(request: Any, body: bytes) -> Response:
        assert json.loads(body)["return_progress"] is True
        return Response(raw, media_type="text/event-stream")

    fake.override = override
    endpoint = {
        "chat": "/v1/chat/completions",
        "completions": "/v1/completions",
        "responses": "/v1/responses",
    }[kind]
    with dash.client() as client:
        response = client.post(
            endpoint,
            json={
                "stream": True,
                "return_progress": True,
                "stream_options": {"include_usage": True},
            },
        )
    assert response.content == raw
    (row,) = dash.wait_rows(1)
    assert row["completion_tokens"] == 16
    assert row["prefill_tokens"] is not None
    assert row["proxy_first_token_ms"] is not None
    assert row["ttft_source"] == ("gufo" if kind == "chat" else "proxy_stream")


def test_completions_stream_now_has_timings() -> None:
    row = fields("completions_stream_include_usage", "completions_sse")
    assert row["prefill_tokens"] == 1
    assert row["prefill_ms"] > 0 and row["decode_ms"] > 0
    assert row["draft_tokens"] > 0


def test_dashboard_assets_revalidate_after_upgrade(dash: Dash) -> None:
    with dash.client() as client:
        index = client.get("/")
        assert "/static/app.js?v=20261001-cache" in index.text
        for path in ("/static/app.js?v=20261001-cache", "/static/app.css"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-cache"
