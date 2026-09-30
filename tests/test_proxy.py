"""End-to-end proxy behaviour against the fake Gufo (real sockets on both sides)."""

from __future__ import annotations

import gzip
import json
import time
from pathlib import Path

import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from .conftest import (
    MODEL,
    Dash,
    FakeGufo,
    ServerThread,
    fixture_bytes,
    fixture_headers,
    make_dash,
    split_sse_events,
)

CHAT = {"model": MODEL, "messages": [{"role": "user", "content": "Say hi"}], "max_tokens": 16}
IGNORED_RESPONSE_HEADERS = {"transfer-encoding", "connection"}


def _upstream_headers(name: str) -> list[tuple[str, str]]:
    return [(k.lower(), v) for k, v in fixture_headers(name)]


def _client_headers(r: httpx.Response) -> list[tuple[str, str]]:
    return [
        (k.lower(), v)
        for k, v in r.headers.multi_items()
        if k.lower() not in IGNORED_RESPONSE_HEADERS and k.lower() != "content-length"
    ]


def test_nonstream_chat_passthrough(dash: Dash, fake: FakeGufo) -> None:
    with dash.client() as c:
        r = c.post(
            "/v1/chat/completions",
            content=json.dumps(CHAT).encode(),
            headers={"Authorization": "Bearer sk-x", "Content-Type": "application/json"},
        )
    assert r.status_code == 200
    assert r.content == fixture_bytes("chat_nonstream")
    assert _client_headers(r) == _upstream_headers("chat_nonstream")
    assert r.headers["x-request-id"] == "fixture-request"
    assert r.headers["access-control-allow-origin"] == "*"
    assert r.headers["content-length"] == str(len(fixture_bytes("chat_nonstream")))
    up = fake.requests[-1]
    assert up.body == json.dumps(CHAT).encode()  # original bytes
    hdrs = {k.lower(): v for k, v in up.headers}
    assert hdrs["authorization"] == "Bearer sk-x"
    assert hdrs["accept-encoding"] == "identity"
    assert hdrs["content-type"] == "application/json"

    (row,) = dash.wait_rows(1)
    assert row["endpoint"] == "/v1/chat/completions"
    assert row["model"] == MODEL
    assert row["gufo_request_id"] == "fixture-request"
    assert row["http_status"] == 200
    assert row["is_streaming"] == 0
    assert row["prompt_tokens"] == 42
    assert row["completion_tokens"] == 16
    assert row["cache_hit"] == 1
    assert row["prompt_tokens_per_second"] is None  # full cache hit: no prefill rate
    assert abs(row["completion_tokens_per_second"] - 33.8086) < 1e-3
    assert row["ttft_source"] == "gufo"
    assert abs(row["ttft_ms"] - 8.618079) < 1e-9
    assert row["execution_plan"] == "serial-fallback"
    assert row["proxy_ttfb_ms"] is not None
    assert row["proxy_first_token_ms"] is None


def test_stream_with_usage_is_byte_identical(dash: Dash, fake: FakeGufo) -> None:
    body = {**CHAT, "stream": True, "stream_options": {"include_usage": True}}
    raw = json.dumps(body).encode()
    with dash.client() as c:
        r = c.post(
            "/v1/chat/completions", content=raw, headers={"Content-Type": "application/json"}
        )
    assert r.content == fixture_bytes("chat_stream_include_usage")
    assert fake.requests[-1].body == raw  # nothing injected
    (row,) = dash.wait_rows(1)
    assert row["is_streaming"] == 1
    assert row["ttft_source"] == "gufo"
    assert row["cache_hit"] == 1
    assert row["draft_tokens"] == 15 and row["draft_tokens_accepted"] == 12
    assert row["proxy_first_token_ms"] is not None


def test_usage_injection_and_event_removal(dash: Dash, fake: FakeGufo) -> None:
    body = {**CHAT, "stream": True}
    with dash.client() as c:
        r = c.post("/v1/chat/completions", json=body)
    sent = json.loads(fake.requests[-1].body)
    assert sent["stream_options"] == {"include_usage": True}
    assert {k: v for k, v in sent.items() if k != "stream_options"} == body
    upstream = fixture_bytes("chat_stream_include_usage")
    usage_events = [e for e in split_sse_events(upstream) if b'"choices":[]' in e]
    assert len(usage_events) == 1
    assert r.content == upstream.replace(usage_events[0], b"")
    assert b'"choices":[]' not in r.content
    assert r.content.endswith(b"data: [DONE]\n\n")
    assert "content-length" not in r.headers
    (row,) = dash.wait_rows(1)
    assert row["prompt_tokens"] == 42
    assert row["queue_ms"] is not None and row["ttft_source"] == "gufo"
    assert row["execution_plan"] == "serial-fallback"


def test_injection_keeps_other_stream_options(dash: Dash, fake: FakeGufo) -> None:
    body = {**CHAT, "stream": True, "stream_options": {"include_usage": False, "x": 1}}
    with dash.client() as c:
        r = c.post("/v1/chat/completions", json=body)
    sent = json.loads(fake.requests[-1].body)
    assert sent["stream_options"] == {"include_usage": True, "x": 1}
    assert b'"choices":[]' not in r.content


def test_malformed_stream_options_forwarded_unchanged(dash: Dash, fake: FakeGufo) -> None:
    for so in ("yes", [1], 3, None):
        raw = json.dumps({**CHAT, "stream": True, "stream_options": so}).encode()
        with dash.client() as c:
            r = c.post("/v1/chat/completions", content=raw)
        assert fake.requests[-1].body == raw
        assert r.content == fixture_bytes("chat_stream_no_usage")
    rows = dash.wait_rows(4)
    assert all(r["http_status"] == 200 and r["prompt_tokens"] is None for r in rows)
    # timings-only finish chunk still gives decode stats; TTFT from the proxy
    assert all(r["completion_tokens"] == 16 and r["ttft_source"] == "proxy_stream" for r in rows)


def test_non_json_body_forwarded(dash: Dash, fake: FakeGufo) -> None:
    with dash.client() as c:
        r = c.post(
            "/v1/chat/completions",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
    assert r.status_code == 400
    assert r.content == fixture_bytes("error_bad_json")
    assert fake.requests[-1].body == b"{not json"
    (row,) = dash.wait_rows(1)
    assert row["http_status"] == 400
    assert row["error_code"] == "parse_error"


def test_completions_stream_injection(dash: Dash, fake: FakeGufo) -> None:
    with dash.client() as c:
        r = c.post(
            "/v1/completions",
            json={"model": MODEL, "prompt": "Hello", "max_tokens": 16, "stream": True},
        )
    upstream = fixture_bytes("completions_stream_include_usage")
    assert r.content == upstream.replace(
        next(e for e in split_sse_events(upstream) if b'"choices":[]' in e), b""
    )
    (row,) = dash.wait_rows(1)
    assert row["prompt_tokens"] == 1 and row["completion_tokens"] == 16
    assert row["draft_tokens"] == 12 and row["draft_tokens_accepted"] == 5
    assert row["ttft_source"] == "proxy_stream"  # no usage.gufo on /v1/completions
    assert row["cache_hit"] is None


def test_responses_stream_and_nonstream(dash: Dash) -> None:
    with dash.client() as c:
        r1 = c.post(
            "/v1/responses",
            json={"model": MODEL, "input": "Say hi", "max_output_tokens": 16, "stream": True},
        )
        r2 = c.post(
            "/v1/responses", json={"model": MODEL, "input": "Say hi", "max_output_tokens": 16}
        )
    assert r1.content == fixture_bytes("responses_stream")
    assert r2.content == fixture_bytes("responses_nonstream")
    s, n = dash.wait_rows(2)
    assert s["is_streaming"] == 1
    assert s["prompt_tokens"] == 42 and s["cached_tokens"] == 42 and s["reasoning_tokens"] == 16
    assert s["finish_reason"] == "incomplete:max_output_tokens"
    assert s["draft_tokens"] == 14 and s["draft_tokens_accepted"] == 12
    assert s["ttft_source"] == "proxy_stream"  # from reasoning_summary_text.delta
    assert n["ttft_ms"] is None and n["ttft_source"] is None
    assert n["proxy_ttfb_ms"] is not None
    assert n["reasoning_tokens"] == 16


def test_messages_and_native_completion(dash: Dash) -> None:
    with dash.client() as c:
        m = c.post("/v1/messages", json=CHAT)
        ms = c.post("/v1/messages", json={**CHAT, "stream": True})
        n = c.post("/completion", json={"prompt": "Hello", "n_predict": 16})
        ns = c.post("/completion", json={"prompt": "Hello", "n_predict": 16, "stream": True})
    assert (m.status_code, ms.status_code, n.status_code, ns.status_code) == (200, 400, 200, 400)
    rows = dash.wait_rows(4)
    msg, msg_s, nat, nat_s = rows
    assert msg["prompt_tokens"] == 42 and msg["cached_tokens"] == 42
    assert msg["finish_reason"] == "max_tokens"
    assert msg["ttft_ms"] is None and msg["draft_tokens"] == 10
    assert msg_s["http_status"] == 400 and msg_s["error_code"] == "invalid_request"
    assert nat["prompt_tokens"] == 1 and nat["completion_tokens"] == 16
    assert nat["finish_reason"] == "length" and nat["ttft_ms"] is None
    assert nat_s["error_code"] == "invalid_request"


def test_error_passthrough_code_only(dash: Dash) -> None:
    with dash.client() as c:
        r = c.post("/v1/chat/completions", json={**CHAT, "model": "nope"})
    assert r.status_code == 404
    assert r.content == fixture_bytes("error_unknown_model")
    (row,) = dash.wait_rows(1)
    assert row["error_code"] == "model_not_found"
    assert row["model"] == "nope"
    assert "not served" not in json.dumps(row)


def test_unreachable_upstream(tmp_path: Path) -> None:
    d = make_dash(tmp_path, "http://127.0.0.1:9")  # nothing listens on :9
    try:
        with d.client() as c:
            r = c.post("/v1/chat/completions", json=CHAT)
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "upstream_unreachable"
        (row,) = d.wait_rows(1)
        assert row["http_status"] == 502 and row["error_code"] == "upstream_unreachable"
    finally:
        d.server.stop()


def test_vision_count_only(dash: Dash) -> None:
    img = "data:image/png;base64,QUJDREVGRw=="
    body = {
        "model": MODEL,
        "max_tokens": 16,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What colour is this?"},
                    {"type": "image_url", "image_url": {"url": img}},
                    {"type": "image_url", "image_url": {"url": img}},
                ],
            }
        ],
    }
    with dash.client() as c:
        c.post("/v1/chat/completions", json=body)
        c.post(
            "/v1/responses",
            json={
                "model": MODEL,
                "input": [{"role": "user", "content": [{"type": "input_image", "image_url": img}]}],
            },
        )
        c.post(
            "/v1/messages",
            json={
                "model": MODEL,
                "max_tokens": 4,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "data": "QUJD"}}
                        ],
                    }
                ],
            },
        )
    v, rsp, msg = dash.wait_rows(3)
    assert (v["is_vision"], v["image_count"]) == (1, 2)
    assert (rsp["is_vision"], rsp["image_count"]) == (1, 1)
    assert (msg["is_vision"], msg["image_count"]) == (1, 1)
    assert v["cache_miss_reason"] == "input_changed"
    assert v["prefill_tokens"] == 111
    assert abs(v["prompt_tokens_per_second"] - 283.168084942507) < 1e-9


def test_non_recorded_routes_untouched(dash: Dash, fake: FakeGufo) -> None:
    with dash.client() as c:
        h = c.get("/health")
        m = c.get("/metrics")
        s = c.get("/slots")
        o = c.options(
            "/v1/chat/completions",
            headers={"Origin": "http://x", "Access-Control-Request-Method": "POST"},
        )
        mo = c.get("/v1/models?x=1")
        api = c.get("/api/health")
        api404 = c.get("/api/nope")
    assert h.content == fixture_bytes("health")
    assert _client_headers(h) == _upstream_headers("health")
    assert m.content == fixture_bytes("metrics_before")
    assert s.json() == [{"id": 0, "prompt": ""}]
    assert o.status_code == 204 and o.headers["access-control-allow-origin"] == "*"
    assert mo.content == fixture_bytes("models")
    assert fake.requests[-1].query == "x=1"
    assert api.json() == {"status": "ok"}
    assert api404.status_code == 404
    assert [r.path for r in fake.requests] == [
        "/health",
        "/metrics",
        "/slots",
        "/v1/chat/completions",
        "/v1/models",
    ]
    time.sleep(0.2)
    assert dash.rows() == []


def test_api_key_not_injected(tmp_path: Path, fake: FakeGufo, fake_server: ServerThread) -> None:
    d = make_dash(tmp_path, fake_server.url, gufo_api_key="dash-secret")
    try:
        with d.client() as c:
            c.post("/v1/chat/completions", json=CHAT)
        hdrs = {k.lower() for k, _ in fake.requests[-1].headers}
        assert "authorization" not in hdrs
    finally:
        d.server.stop()


def test_connection_named_and_duplicate_headers(dash: Dash, fake: FakeGufo) -> None:
    def override(request: Request, body: bytes) -> Response:
        r = Response(b"{}", media_type="application/json")
        r.raw_headers += [
            (b"x-dup", b"a"),
            (b"x-dup", b"b"),
            (b"x-foo", b"secret-hop"),
            (b"keep-alive", b"timeout=5"),
            (b"connection", b"close, X-Foo"),
        ]
        return r

    fake.override = override
    with dash.client() as c:
        r = c.get(
            "/v1/models",
            headers={"Connection": "keep-alive, X-Bar", "X-Bar": "drop-me", "X-Keep": "1"},
        )
    assert r.headers.get_list("x-dup") == ["a", "b"]
    assert "x-foo" not in r.headers
    assert "keep-alive" not in r.headers
    assert "access-control-allow-origin" not in r.headers  # no CORS added by us
    up = {k.lower() for k, _ in fake.requests[-1].headers}
    assert "x-bar" not in up and "x-keep" in up


def test_gzip_passthrough(dash: Dash, fake: FakeGufo) -> None:
    payload = gzip.compress(fixture_bytes("chat_nonstream"))
    fake.override = lambda req, body: Response(
        payload, headers={"Content-Type": "application/json", "Content-Encoding": "gzip"}
    )
    with dash.client() as c, c.stream("POST", "/v1/chat/completions", json=CHAT) as r:
        raw = b"".join(r.iter_raw())
    assert raw == payload
    assert r.headers["content-encoding"] == "gzip"
    hdrs = {k.lower(): v for k, v in fake.requests[-1].headers}
    assert hdrs["accept-encoding"] == "identity"
    (row,) = dash.wait_rows(1)
    assert row["http_status"] == 200 and row["prompt_tokens"] is None  # stats skipped


def test_client_disconnect_cancels_upstream(dash: Dash, fake: FakeGufo) -> None:
    chunk = (
        b'data: {"object":"chat.completion.chunk","model":"'
        + MODEL.encode()
        + b'","choices":[{"index":0,"delta":{"content":"x"},"finish_reason":null}]}\n\n'
    )

    async def override(request: Request, body: bytes) -> Response:
        async def gen():  # type: ignore[no-untyped-def]
            import asyncio

            try:
                for _ in range(400):
                    yield chunk
                    await asyncio.sleep(0.025)
                fake.stream_finished.set()
            finally:
                if not fake.stream_finished.is_set():
                    fake.stream_closed_early.set()

        return StreamingResponse(gen(), media_type="text/event-stream")

    fake.override = override
    with (
        dash.client() as c,
        c.stream("POST", "/v1/chat/completions", json={**CHAT, "stream": True}) as r,
    ):
        it = r.iter_raw()
        next(it)
        next(it)
    assert fake.stream_closed_early.wait(5), "upstream stream was not closed"
    assert not fake.stream_finished.is_set()
    (row,) = dash.wait_rows(1)
    assert row["finish_reason"] == "client_cancelled"
    assert row["proxy_first_token_ms"] is not None
    assert row["total_request_ms"] < 5000
    assert len(dash.ctx.inflight) == 0
