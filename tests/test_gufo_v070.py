"""Gufo 0.7.0 captures: cached-token counters, new series, prefix sharing."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.responses import Response

from app import db, extract, prom, stats
from app.content import text_parts
from app.db import StatsWriter
from app.poller import Poller, compute_unattributed
from app.sse import SSEParser

from .conftest import Dash, FakeGufo
from .test_poller import _record

FIXTURES = Path(__file__).parent / "fixtures" / "gufo" / "v0.7.0"

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


@pytest.mark.parametrize("skip", [None, 4])
def test_executed_prefill_reconciliation(tmp_path: Path, skip: int | None) -> None:
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
            assert result["counter_prompt_delta"] == 94
            assert result["counter_completion_delta"] == 144
            missing = fields("chat_vision_nonstream", "chat") if skip is not None else {}
            assert result["prompt_tokens"] == missing.get("prefill_tokens", 0)
            assert result["completion_tokens"] == missing.get("completion_tokens", 0)
            assert result["visible"] == (skip is not None)

    asyncio.run(run())


def test_cached_and_executed_prompt_counters_sum_to_full_prompt() -> None:
    # 0.7.0 splits prompt accounting: executed prefill (existing counter,
    # excluding cache hits) + a new prompt_tokens_cached_total counter.
    before, after = _metrics("metrics_before"), _metrics("metrics_after")
    executed = after[prom.PROMPT_TOTAL] - before[prom.PROMPT_TOTAL]
    cached = (
        after["llamacpp:prompt_tokens_cached_total"] - before["llamacpp:prompt_tokens_cached_total"]
    )
    full_prompt = sum(fields(name, kind)["prompt_tokens"] for name, kind in WINDOW_REQUESTS)
    assert executed == 94 and cached == 72
    assert executed + cached == full_prompt == 166


def test_new_llamacpp_series_are_recorded() -> None:
    samples = _metrics("metrics_after")
    for name in (
        "gufo_device_lost_total",
        "llamacpp:prompt_tokens_cached_total",
        "llamacpp:prompt_seconds_total",
        "llamacpp:tokens_predicted_seconds_total",
        "llamacpp:n_tokens_max",
        "llamacpp:spec_decode_num_drafts_total",
        "llamacpp:spec_decode_num_draft_tokens_total",
        "llamacpp:spec_decode_num_accepted_tokens_total",
    ):
        assert samples[name] >= 0, name


def test_models_advertise_input_modalities() -> None:
    data = json.loads((FIXTURES / "models.json").read_bytes())["data"]
    assert data[0]["architecture"]["input_modalities"] == ["text", "image"]


def test_prefix_sharing_and_draft_rounds_reach_extra_metrics() -> None:
    row = fields("chat_nonstream", "chat")
    extra = row["extra_metrics"]
    # In-flight prefix sharing (#382) and verification rounds (#403) arrive as
    # new usage.gufo keys and flow into extra_metrics without code changes.
    assert extra["shared_prefix_wait_ms"] == 0
    assert extra["draft_rounds"] == 3


def test_timings_draft_rounds_without_usage_gufo() -> None:
    # Completions carry verification rounds only in terminal timings.
    row = fields("completions_stream_include_usage", "completions_sse")
    assert row["prefill_tokens"] == 1
    assert row["prefill_ms"] > 0 and row["decode_ms"] > 0
    assert row["draft_tokens"] > 0
    assert row["extra_metrics"]["draft_rounds"] == 10


def test_messages_thinking_stays_out_of_stats_and_capture() -> None:
    # #380 returns thinking as its own content block; with max_tokens 16 the
    # answer is thinking-only. Usage extraction is unaffected, text capture
    # keeps ignoring the thinking block.
    body = json.loads((FIXTURES / "messages_nonstream.json").read_bytes())
    assert [block["type"] for block in body["content"]] == ["thinking"]
    assert text_parts(body["content"]) == []
    row = fields("messages_nonstream", "messages")
    assert row["cached_tokens"] == 12
    assert row["completion_tokens"] == 16
    assert row["extra_metrics"]["draft_rounds"] == 5


def test_error_codes_still_extract() -> None:
    # Streaming-generation failures were not reproducible in the capture
    # harness (over-context prompts are refused before the stream starts),
    # but error envelopes still expose stable codes.
    unknown = json.loads((FIXTURES / "error_unknown_model.json").read_bytes())
    assert extract.extract_error_code(unknown) == "model_not_found"
    refused = json.loads((FIXTURES / "messages_stream.json").read_bytes())
    assert extract.extract_error_code(refused) == "invalid_request"


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
