"""Gufo 0.10.0 captures: /v1/messages gains streaming and tools."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from starlette.responses import Response

from app import db, extract, prom, stats
from app.content import text_parts
from app.db import StatsWriter
from app.poller import Poller, compute_unattributed
from app.sse import SSEParser

from .conftest import Dash, FakeGufo
from .test_poller import _record

FIXTURES = Path(__file__).parent / "fixtures" / "gufo" / "v0.10.0"

# The 10 usage-bearing requests captured between metrics_before and
# metrics_after, as the proxy would record them (the no-usage stream gets
# usage via injection; 0.10.0's messages_stream replaces the 0.7.0 400).
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
            assert result["counter_prompt_delta"] == 98
            assert result["counter_completion_delta"] == 25
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
    # 98 executed + 98 cached = 196 full prompt; message input_tokens count
    # the full prompt including cached tokens, like chat usage.
    assert executed == 98 and cached == 98
    assert executed + cached == full_prompt == 196


def test_messages_stream_usage_and_first_token() -> None:
    raw = (FIXTURES / "messages_stream.sse").read_bytes()
    events = SSEParser().feed(raw)
    inspector = extract.StreamInspector("messages", False)
    first_at = None
    for i, event in enumerate(events):
        if event.data is None:
            continue
        _, first = inspector.on_data(event.data)
        if first:
            first_at = i
    # The empty text_delta after content_block_start is not a token; "Hi" is.
    assert first_at is not None and '"text":"Hi"' in (events[first_at].data or "")
    assert inspector.fields["prompt_tokens"] == 14
    assert inspector.fields["cached_tokens"] == 14  # full-prompt hit: 14 of 14
    assert inspector.fields["completion_tokens"] == 1
    assert inspector.fields["finish_reason"] == "end_turn"


def test_messages_tools_nonstream_records_usage_and_tool_use() -> None:
    body = json.loads((FIXTURES / "messages_tools_nonstream.json").read_bytes())
    assert [block["type"] for block in body["content"]] == ["tool_use"]
    assert text_parts(body["content"]) == []  # tool arguments are never captured
    row = fields("messages_tools_nonstream", "messages")
    assert (row["prompt_tokens"], row["cached_tokens"], row["completion_tokens"]) == (292, 0, 26)
    assert row["finish_reason"] == "tool_use"
    assert row["extra_metrics"]["draft_rounds"] == 5


def test_messages_tools_stream_tool_json_is_not_a_token() -> None:
    # The tool_use block streams as input_json_delta; arguments are not tokens,
    # but usage and stop_reason still arrive on message_delta.
    inspector = extract.StreamInspector("messages", False)
    for event in SSEParser().feed((FIXTURES / "messages_tools_stream.sse").read_bytes()):
        if event.data is not None:
            inspector.on_data(event.data)
    assert inspector.first_token_seen is False
    assert inspector.fields["prompt_tokens"] == 292
    assert inspector.fields["cached_tokens"] == 292  # replay of the non-stream capture
    assert inspector.fields["completion_tokens"] == 26
    assert inspector.fields["finish_reason"] == "tool_use"


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
    assert (row["prompt_tokens"], row["cached_tokens"], row["completion_tokens"]) == (14, 14, 1)
    assert row["finish_reason"] == "end_turn"
    assert row["proxy_first_token_ms"] is not None
    assert row["ttft_source"] == "proxy_stream"  # no usage.gufo block on Messages
