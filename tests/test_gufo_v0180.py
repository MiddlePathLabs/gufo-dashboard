"""Gufo 0.18.0 captures: restored Messages full-prompt tokens, component cache."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from starlette.responses import Response

from app import db, extract, prom, stats
from app.db import StatsWriter
from app.poller import Poller, compute_unattributed
from app.sse import SSEParser

from .conftest import Dash, FakeGufo
from .test_poller import _record

FIXTURES = Path(__file__).parent / "fixtures" / "gufo" / "v0.18.0"

# The 10 usage-bearing requests captured between metrics_before and
# metrics_after, as the proxy would record them (the no-usage stream gets
# usage via injection; both Messages captures are full-prompt cache hits).
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
    ("messages_stream", "messages_sse"),
]


def fields(name: str, kind: str) -> dict[str, Any]:
    if kind.endswith("_sse"):
        inspector = extract.StreamInspector(kind[:-4], False)
        for event in SSEParser().feed((FIXTURES / f"{name}.sse").read_bytes()):
            if event.data is not None:
                inspector.on_data(event.data)
        return inspector.fields
    return extract.BODY_EXTRACTORS[kind](json.loads((FIXTURES / f"{name}.json").read_bytes()))


def _metrics(name: str) -> dict[str, float]:
    return prom.parse_prometheus((FIXTURES / f"{name}.txt").read_text())


def test_counters_reconcile_across_window(tmp_path: Path) -> None:
    path = str(tmp_path / "stats.sqlite")
    db.init_db(path)
    conn = db.connect(path)
    for i, (name, kind) in enumerate(WINDOW_REQUESTS):
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
            assert result["counter_completion_delta"] == 160
            assert result["prompt_tokens"] == 0 and result["completion_tokens"] == 0
            assert result["visible"] is False

    asyncio.run(run())


def test_cached_and_executed_prompt_counters_sum_to_full_prompt() -> None:
    before, after = _metrics("metrics_before"), _metrics("metrics_after")
    executed = after[prom.PROMPT_TOTAL] - before[prom.PROMPT_TOTAL]
    cached = (
        after["llamacpp:prompt_tokens_cached_total"] - before["llamacpp:prompt_tokens_cached_total"]
    )
    full_prompt = sum(fields(name, kind)["prompt_tokens"] for name, kind in WINDOW_REQUESTS)
    # 154 executed + 294 cached = 448 full prompt; the Messages rows only
    # reach the full count because the recorder restores input_tokens +
    # cache_read_input_tokens (Gufo 0.12+ reports the uncached share only).
    assert executed == 154 and cached == 294
    assert executed + cached == full_prompt == 448


def test_messages_full_prompt_hits_are_restored() -> None:
    for name in ("messages_nonstream", "messages_stream"):
        row = fields(name, "messages" if name.endswith("nonstream") else "messages_sse")
        assert row["prompt_tokens"] == 42 and row["cached_tokens"] == 42
        assert row["completion_tokens"] == 16
        assert row["finish_reason"] == "max_tokens"


def test_messages_raw_captures_report_uncached_input_only() -> None:
    # Pin the upstream #530 semantics these captures rely on.
    body = json.loads((FIXTURES / "messages_nonstream.json").read_bytes())
    usage = body["usage"]
    assert usage["input_tokens"] == 0 and usage["cache_read_input_tokens"] == 42


def test_spec_lookup_series_present_but_not_consumed() -> None:
    samples = _metrics("metrics_after")
    assert "gufo_spec_lookup_tokens_total" in samples
    assert "gufo_spec_lookup_accepted_tokens_total" in samples
    assert not hasattr(prom, "SPEC_LOOKUP")


def test_messages_tools_captures_record_usage() -> None:
    nonstream = fields("messages_tools_nonstream", "messages")
    assert (nonstream["prompt_tokens"], nonstream["cached_tokens"]) == (299, 0)
    assert nonstream["finish_reason"] == "tool_use"
    stream = fields("messages_tools_stream", "messages_sse")
    assert (stream["prompt_tokens"], stream["cached_tokens"]) == (299, 299)  # full-prompt hit


def test_streamed_messages_through_proxy(dash: Dash, fake: FakeGufo) -> None:
    raw = (FIXTURES / "messages_stream.sse").read_bytes()

    def override(request: Any, body: bytes) -> Response:
        return Response(raw, media_type="text/event-stream")

    fake.override = override
    with dash.client() as client:
        response = client.post("/v1/messages", json={"model": "fixture-model-a", "stream": True})
    assert response.content == raw
    (row,) = dash.wait_rows(1)
    assert row["is_streaming"] == 1 and row["http_status"] == 200
    assert (row["prompt_tokens"], row["cached_tokens"], row["completion_tokens"]) == (42, 42, 16)
    assert row["finish_reason"] == "max_tokens"
    assert row["proxy_first_token_ms"] is not None  # thinking-only stream
    assert row["ttft_source"] == "proxy_stream"
