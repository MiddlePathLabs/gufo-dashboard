"""SSE parser, stream inspector and extractor unit tests (fixtures + synthetic variants)."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from app import extract
from app.sse import SSEParser

from .conftest import (
    MODEL,
    Dash,
    FakeGufo,
    fixture_bytes,
    fixture_json,
    make_dash,
    split_sse_events,
)

# --------------------------------------------------------------------------- #
# SSE parser
# --------------------------------------------------------------------------- #


def _feed_all(body: bytes, sizes: list[int]) -> tuple[list[Any], bytes]:
    p = SSEParser()
    events = []
    i = 0
    for n in sizes:
        events += p.feed(body[i : i + n])
        i += n
    events += p.feed(body[i:])
    return events, p.take_remainder()


@pytest.mark.parametrize("seed", range(25))
def test_sse_arbitrary_chunk_boundaries(seed: int) -> None:
    body = fixture_bytes("responses_stream")
    rnd = random.Random(seed)
    sizes = [rnd.randint(1, 40) for _ in range(400)]
    events, rest = _feed_all(body, sizes)
    assert rest == b""
    assert b"".join(e.raw for e in events) == body
    assert len(events) == 24
    assert events[0].event == "response.created"
    assert json.loads(events[-1].data)["type"] == "response.incomplete"


def test_sse_crlf_multiline_and_comments() -> None:
    body = (
        b": keepalive\r\n\r\n"
        b'event: a\r\ndata: {"x":\r\ndata: 1}\r\n\r\n'
        b"data:no-space\n\n"
        b"data: cr-only\r\r"
        b"data: [DONE]\r\n\r\n"
    )
    for size in (1, 2, 3, 7, len(body)):
        events, rest = _feed_all(body, [size] * (len(body) // size + 1))
        assert rest == b""
        assert b"".join(e.raw for e in events) == body
        assert [e.data for e in events] == [None, '{"x":\n1}', "no-space", "cr-only", "[DONE]"]
        assert events[1].event == "a"


def test_sse_incomplete_tail_is_returned() -> None:
    p = SSEParser()
    assert p.feed(b"data: 1\n\ndata: 2\n") != []
    assert p.take_remainder() == b"data: 2\n"


# --------------------------------------------------------------------------- #
# Numeric / string validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        -1,
        -0.5,
        float("nan"),
        float("inf"),
        float("-inf"),
        2**63,
        2**70,
        "42",
        "1e3",
        None,
        [1],
        {"a": 1},
    ],
)
def test_bad_numbers_become_null(value: Any) -> None:
    assert extract.as_int(value) is None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        assert extract.as_float(value) is None


def test_good_numbers() -> None:
    assert extract.as_int(0) == 0
    assert extract.as_int(2**63 - 1) == 2**63 - 1
    assert extract.as_int(16.0) == 16
    assert extract.as_int(16.5) is None
    assert extract.as_float(3) == 3.0


def test_loads_strict_nonfinite_constants_are_rejected_per_field() -> None:
    obj = extract.loads_strict(b'{"a": NaN, "b": Infinity, "c": -Infinity, "d": 4}')
    assert extract.as_float(obj["a"]) is None
    assert extract.as_float(obj["b"]) is None
    assert extract.as_float(obj["c"]) is None
    assert extract.as_float(obj["d"]) == 4.0


def test_string_fields_validated() -> None:
    assert extract.as_str("serial-c1") == "serial-c1"
    assert extract.as_str("x" * 65) is None
    assert extract.as_str("has space") is None
    assert extract.as_str("inj'ect") is None
    assert extract.as_str("m" * 128, 128) is not None


# --------------------------------------------------------------------------- #
# Extractors on real fixtures
# --------------------------------------------------------------------------- #


def _finalize(
    fields: dict[str, Any], streaming: bool = False, first: float | None = None
) -> dict[str, Any]:
    return extract.finalize_record(
        fields,
        endpoint="/x",
        request_model=None,
        is_streaming=streaming,
        image_count=0,
        http_status=200,
        gufo_request_id="r1",
        started_at_ms=1,
        completed_at_ms=2,
        proxy_ttfb_ms=5.0,
        proxy_first_token_ms=first,
        total_request_ms=10.0,
        context_lengths={MODEL: 65536},
        cancelled=False,
    )


def test_chat_nonstream_fixture() -> None:
    rec = _finalize(extract.extract_chat(fixture_json("chat_nonstream")))
    assert rec["model"] == MODEL
    assert rec["finish_reason"] == "length"
    assert (rec["prompt_tokens"], rec["cached_tokens"], rec["prefill_tokens"]) == (42, 42, 0)
    assert rec["decode_ms"] == pytest.approx(473.252382)
    assert rec["prompt_tokens_per_second"] is None  # zero-work rule
    assert rec["cache_hit"] is True and rec["cache_miss_reason"] is None
    assert rec["queue_depth_at_submit"] == 1 and rec["physical_execution_width"] == 1
    assert rec["requested_logical_concurrency"] == 2
    assert rec["context_length"] == 65536
    assert rec["context_used_pct"] == pytest.approx(100 * 58 / 65536)
    extra = rec["extra_metrics"]
    assert extra["cache_snapshot_bytes"] == 0 and extra["cache_disk_hit"] is False
    assert "execution_plan" not in extra and "ttft_ms" not in extra


def test_chat_cachehit_fixture_prefill_rate_null() -> None:
    rec = _finalize(extract.extract_chat(fixture_json("chat_nonstream_cachehit")))
    assert rec["prefill_tokens"] == 0
    assert rec["prefill_ms"] == 0
    assert rec["prompt_tokens_per_second"] is None
    assert rec["cache_hit"] is True


def test_vision_fixture_miss() -> None:
    rec = _finalize(extract.extract_chat(fixture_json("chat_vision_nonstream")))
    assert rec["cache_hit"] is False
    assert rec["cache_miss_reason"] == "input_changed"
    assert rec["cache_common_prefix_tokens"] == 33
    assert rec["extra_metrics"]["cache_checkpoint_tokens"] == 58
    assert rec["prompt_tokens_per_second"] == pytest.approx(283.168084942507)


def test_responses_and_messages_have_no_gufo() -> None:
    r = _finalize(extract.extract_responses(fixture_json("responses_nonstream")))
    assert r["reasoning_tokens"] == 16 and r["cached_tokens"] == 42
    assert r["ttft_ms"] is None and r["cache_hit"] is None and r["extra_metrics"] is None
    m = _finalize(extract.extract_messages(fixture_json("messages_nonstream")))
    assert m["cached_tokens"] == 42 and m["finish_reason"] == "max_tokens"
    assert m["reasoning_tokens"] is None


def test_chat_reasoning_tokens_null() -> None:
    assert (
        _finalize(extract.extract_chat(fixture_json("chat_nonstream")))["reasoning_tokens"] is None
    )


# --------------------------------------------------------------------------- #
# Synthetic variants
# --------------------------------------------------------------------------- #


def _chat_with(**gufo_overrides: Any) -> dict[str, Any]:
    obj = fixture_json("chat_vision_nonstream")
    obj["usage"]["gufo"].update(gufo_overrides)
    return obj


def test_missing_everything() -> None:
    rec = _finalize(extract.extract_chat({}))
    assert rec["prompt_tokens"] is None and rec["model"] is None and rec["extra_metrics"] is None
    rec = _finalize(extract.extract_chat({"usage": None, "timings": "x", "choices": "y"}))
    assert rec["completion_tokens"] is None


def test_pathological_numbers_null_field_only() -> None:
    obj = fixture_json("chat_vision_nonstream")
    obj["usage"]["prompt_tokens"] = True
    obj["usage"]["completion_tokens"] = -3
    obj["usage"]["gufo"]["queue_ms"] = float("nan")
    obj["usage"]["gufo"]["decode_ms"] = float("inf")
    obj["usage"]["gufo"]["prefill_chunks"] = 2**64
    obj["usage"]["gufo"]["ttft_ms"] = "421"
    raw = json.dumps(obj).replace("NaN", "NaN")  # json.dumps emits NaN / Infinity
    rec = _finalize(extract.extract_chat(extract.loads_strict(raw)))
    assert rec["prompt_tokens"] is None
    assert rec["completion_tokens"] == 16  # falls back to timings.predicted_n
    assert rec["queue_ms"] is None
    assert rec["decode_ms"] == pytest.approx(417.854128)  # falls back to timings.predicted_ms
    assert rec["prefill_chunks"] is None
    assert rec["ttft_ms"] is None and rec["ttft_source"] is None
    assert rec["cache_miss_reason"] == "input_changed"  # rest of the record kept


def test_extra_metrics_numbers_and_bools_only() -> None:
    obj = _chat_with(
        new_counter=7,
        new_flag=True,
        new_ratio=0.5,
        new_string="CANARY-string",
        new_list=[1, 2],
        new_obj={"a": 1},
        BadKey=1,
        **{"bad-key": 2, "x" * 70: 3},
        new_nan=float("nan"),
    )
    rec = _finalize(extract.extract_chat(extract.loads_strict(json.dumps(obj))))
    extra = rec["extra_metrics"]
    assert extra["new_counter"] == 7 and extra["new_flag"] is True and extra["new_ratio"] == 0.5
    for k in ("new_string", "new_list", "new_obj", "BadKey", "bad-key", "x" * 70, "new_nan"):
        assert k not in extra


def test_extra_metrics_capped() -> None:
    obj = _chat_with(**{f"k{i}": i for i in range(200)})
    extra = _finalize(extract.extract_chat(obj))["extra_metrics"]
    assert len(extra) <= extract.MAX_EXTRA_KEYS


def test_unknown_execution_plan_string_validated() -> None:
    assert (
        _finalize(extract.extract_chat(_chat_with(execution_plan="parallel-c2")))["execution_plan"]
        == "parallel-c2"
    )
    assert (
        _finalize(extract.extract_chat(_chat_with(execution_plan="a b")))["execution_plan"] is None
    )
    assert _finalize(extract.extract_chat(_chat_with(cache_hit="yes")))["cache_hit"] is None


# --------------------------------------------------------------------------- #
# Request analysis
# --------------------------------------------------------------------------- #


def test_analyze_request_rules() -> None:
    base = {"model": MODEL, "messages": [], "stream": True}
    i = extract.analyze_request("chat", json.dumps(base).encode())
    assert i.injected and json.loads(i.body)["stream_options"] == {"include_usage": True}
    i = extract.analyze_request(
        "chat", json.dumps({**base, "stream_options": {"include_usage": True}}).encode()
    )
    assert i.usage_requested and not i.injected
    for bad in ("x", 1, [], None):
        raw = json.dumps({**base, "stream_options": bad}).encode()
        i = extract.analyze_request("chat", raw)
        assert not i.injected and i.body == raw
    raw = b'{"model":"m","stream":true,"temperature":NaN}'
    i = extract.analyze_request("chat", raw)
    assert not i.injected and i.body == raw
    raw = json.dumps({**base}).encode()
    assert not extract.analyze_request("responses", raw).injected
    assert not extract.analyze_request("chat", b"[1,2]").injected
    raw = '{"model":"m","stream":true,"messages":[{"role":"user","content":"héllo ✓"}]}'.encode()
    i = extract.analyze_request("chat", raw)
    assert "héllo ✓" in i.body.decode()  # ensure_ascii=False


# --------------------------------------------------------------------------- #
# Stream inspector
# --------------------------------------------------------------------------- #


def _inspect(
    kind: str, body: bytes, strip: bool
) -> tuple[extract.StreamInspector, list[bool], list[bool]]:
    ins = extract.StreamInspector(kind, strip)
    drops, firsts = [], []
    for ev in SSEParser().feed(body):
        d, f = ins.on_data(ev.data) if ev.data is not None else (False, False)
        drops.append(d)
        firsts.append(f)
    return ins, drops, firsts


def test_role_only_first_chunk_is_not_first_token() -> None:
    _, _, firsts = _inspect("chat", fixture_bytes("chat_stream_include_usage"), False)
    assert firsts.index(True) == 1  # chunk 0 is role-only


def test_completions_first_token_uses_text() -> None:
    body = b'data: {"object":"text_completion","choices":[{"text":""}]}\n\n' + fixture_bytes(
        "completions_stream_include_usage"
    )
    _, _, firsts = _inspect("completions", body, False)
    assert firsts.index(True) == 1


def test_responses_first_token_reasoning_summary() -> None:
    ins, _, firsts = _inspect("responses", fixture_bytes("responses_stream"), False)
    events = SSEParser().feed(fixture_bytes("responses_stream"))
    assert events[firsts.index(True)].event == "response.reasoning_summary_text.delta"
    assert ins.fields["finish_reason"] == "incomplete:max_output_tokens"


def test_malformed_events_ignored() -> None:
    good = fixture_bytes("chat_stream_include_usage")
    body = b'data: {broken\n\nevent: weird\ndata: {"type":"unknown"}\n\ndata: 12\n\n' + good
    ins, drops, _ = _inspect("chat", body, True)
    assert sum(drops) == 1
    assert ins.fields["prompt_tokens"] == 42


def test_strip_only_one_event() -> None:
    good = fixture_bytes("chat_stream_include_usage")
    usage_ev = next(e for e in split_sse_events(good) if b'"choices":[]' in e)
    _, drops, _ = _inspect("chat", usage_ev + usage_ev, True)
    assert drops == [True, False]


# --------------------------------------------------------------------------- #
# Through the proxy: CRLF streams and tiny chunks
# --------------------------------------------------------------------------- #


def _crlf(body: bytes) -> bytes:
    return body.replace(b"\n", b"\r\n")


@pytest.mark.parametrize("crlf", [False, True])
@pytest.mark.parametrize("chunk", [None, 1, 5, 97])
def test_usage_event_removed_with_terminator(
    dash: Dash, fake: FakeGufo, crlf: bool, chunk: int | None
) -> None:
    from starlette.responses import StreamingResponse

    upstream = fixture_bytes("chat_stream_include_usage")
    if crlf:
        upstream = _crlf(upstream)

    def override(request: Any, body: bytes) -> Any:
        async def gen() -> Any:
            size = chunk or len(upstream)
            for i in range(0, len(upstream), size):
                yield upstream[i : i + size]

        return StreamingResponse(gen(), media_type="text/event-stream")

    fake.override = override
    with dash.client() as c:
        r = c.post("/v1/chat/completions", json={"model": MODEL, "messages": [], "stream": True})
    sep = b"\r\n\r\n" if crlf else b"\n\n"
    events = [e + sep for e in upstream.split(sep) if e]
    usage = [e for e in events if b'"choices":[]' in e]
    assert len(usage) == 1
    assert r.content == upstream.replace(usage[0], b"")
    assert r.content.endswith(b"data: [DONE]" + sep)
    (row,) = dash.wait_rows(1)
    assert row["prompt_tokens"] == 42 and row["ttft_source"] == "gufo"


def test_malformed_sse_through_proxy(dash: Dash, fake: FakeGufo) -> None:
    from starlette.responses import Response

    upstream = b"data: {oops\n\nevent: mystery\ndata: {}\n\n" + fixture_bytes(
        "chat_stream_include_usage"
    )
    fake.override = lambda req, body: Response(upstream, media_type="text/event-stream")
    with dash.client() as c:
        r = c.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": [],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )
    assert r.content == upstream
    (row,) = dash.wait_rows(1)
    assert row["completion_tokens"] == 16 and row["execution_plan"] == "serial-fallback"


def test_giant_event_does_not_break_stream(
    tmp_path: Path, fake: FakeGufo, fake_server: Any
) -> None:
    import app.sse as sse_mod

    old = sse_mod.DEFAULT_MAX_EVENT_BYTES
    sse_mod.SSEParser.__init__.__defaults__ = (1000,)  # type: ignore[attr-defined]
    try:
        from starlette.responses import StreamingResponse

        upstream = b"data: " + b"x" * 5000 + b"\n\n" + fixture_bytes("chat_stream_include_usage")

        def override(req: Any, body: bytes) -> Any:
            async def gen() -> Any:
                for i in range(0, len(upstream), 512):  # giant event spans chunks
                    yield upstream[i : i + 512]

            return StreamingResponse(gen(), media_type="text/event-stream")

        fake.override = override
        d = make_dash(tmp_path, fake_server.url)
        try:
            with d.client() as c:
                r = c.post(
                    "/v1/chat/completions", json={"model": MODEL, "messages": [], "stream": True}
                )
            assert r.content == upstream  # inspection abandoned; nothing dropped
            d.wait_rows(1)
        finally:
            d.server.stop()
    finally:
        sse_mod.SSEParser.__init__.__defaults__ = (old,)  # type: ignore[attr-defined]
