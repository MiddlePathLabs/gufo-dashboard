"""Canary test: nothing sensitive may reach SQLite (main, -wal, -shm) or logs."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from starlette.responses import Response

from .conftest import MODEL, FakeGufo, ServerThread, fixture_json, make_dash

CANARIES = {
    "prompt": "CANARY-PROMPT-7f3a",
    "auth": "CANARY-AUTH-91bc",
    "image_url": "CANARY-IMGURL-5d2e",
    "tool_def": "CANARY-TOOLDEF-0a9f",
    "tool_args": "CANARY-TOOLARGS-66c1",
    "content": "CANARY-CONTENT-2b7d",
    "reasoning": "CANARY-REASONING-c4e8",
    "summary": "CANARY-SUMMARY-8e1f",
    "text": "CANARY-TEXT-3f60",
    "error_msg": "CANARY-ERRMSG-d7a2",
    "gufo_str": "CANARY-GUFOSTR-19b4",
    "query": "CANARY-QUERY-aa01",
}


def _sse(*objs: Any) -> bytes:
    return (
        b"".join(b"data: " + json.dumps(o).encode() + b"\n\n" for o in objs) + b"data: [DONE]\n\n"
    )


def _chat_body() -> bytes:
    obj = fixture_json("chat_vision_nonstream")
    obj["choices"][0]["message"] = {
        "role": "assistant",
        "content": CANARIES["content"],
        "reasoning_content": CANARIES["reasoning"],
        "tool_calls": [
            {"type": "function", "function": {"name": "f", "arguments": CANARIES["tool_args"]}}
        ],
    }
    obj["usage"]["gufo"]["new_string_field"] = CANARIES["gufo_str"]
    return json.dumps(obj).encode()


def _override(request: Any, body: bytes) -> Any:
    path = request.url.path
    try:
        req = json.loads(body) if body else {}
    except ValueError:
        return None
    if req.get("model") == "err":
        return Response(
            json.dumps(
                {"error": {"message": CANARIES["error_msg"], "type": "x", "code": "bad_thing"}}
            ).encode(),
            status_code=400,
            media_type="application/json",
        )
    if path == "/v1/chat/completions" and req.get("stream"):
        return Response(
            _sse(
                {
                    "object": "chat.completion.chunk",
                    "choices": [{"delta": {"reasoning_content": CANARIES["reasoning"]}}],
                },
                {
                    "object": "chat.completion.chunk",
                    "choices": [
                        {"delta": {"content": CANARIES["content"]}, "finish_reason": "stop"}
                    ],
                },
                {
                    "object": "chat.completion.chunk",
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 2,
                        "gufo": {"x_str": CANARIES["gufo_str"]},
                    },
                },
            ),
            media_type="text/event-stream",
        )
    if path == "/v1/chat/completions":
        return Response(_chat_body(), media_type="application/json")
    if path == "/v1/completions":
        return Response(
            _sse(
                {
                    "object": "text_completion",
                    "choices": [{"text": CANARIES["text"], "finish_reason": "length"}],
                },
                {
                    "object": "text_completion",
                    "choices": [],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            ),
            media_type="text/event-stream",
        )
    if path == "/v1/responses":
        return Response(
            b"event: response.reasoning_summary_text.delta\n"
            + b"data: "
            + json.dumps(
                {"type": "response.reasoning_summary_text.delta", "delta": CANARIES["summary"]}
            ).encode()
            + b"\n\nevent: response.completed\ndata: "
            + json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "output": [
                            {"type": "reasoning", "summary": [{"text": CANARIES["summary"]}]}
                        ],
                        "usage": {"input_tokens": 3, "output_tokens": 4},
                    },
                }
            ).encode()
            + b"\n\n",
            media_type="text/event-stream",
        )
    return None


def test_canaries_never_persisted_or_logged(
    tmp_path: Path, fake: FakeGufo, fake_server: ServerThread, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    fake.override = _override
    d = make_dash(tmp_path, fake_server.url)
    auth = {"Authorization": f"Bearer {CANARIES['auth']}"}
    tools = [
        {
            "type": "function",
            "function": {"name": "t", "description": CANARIES["tool_def"], "parameters": {}},
        }
    ]
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": CANARIES["prompt"]},
                {
                    "type": "image_url",
                    "image_url": {"url": f"http://img/{CANARIES['image_url']}.png"},
                },
            ],
        },
        {
            "role": "assistant",
            "tool_calls": [
                {"type": "function", "function": {"name": "t", "arguments": CANARIES["tool_args"]}}
            ],
        },
    ]
    try:
        with d.client(headers=auth) as c:
            q = f"?key={CANARIES['query']}"
            c.post(
                "/v1/chat/completions" + q,
                json={"model": MODEL, "messages": messages, "tools": tools},
            )
            c.post(
                "/v1/chat/completions",
                json={"model": MODEL, "messages": messages, "tools": tools, "stream": True},
            )
            c.post(
                "/v1/completions",
                json={"model": MODEL, "prompt": CANARIES["prompt"], "stream": True},
            )
            c.post(
                "/v1/responses", json={"model": MODEL, "input": CANARIES["prompt"], "stream": True}
            )
            c.post("/v1/chat/completions", json={"model": "err", "messages": messages})
            c.post("/v1/chat/completions", content=b"{" + CANARIES["prompt"].encode())
            c.get("/slots" + q)
            rows = d.wait_rows(6)
            ids = [r["id"] for r in rows]
            for i in ids:
                c.get(f"/api/requests/{i}")
            c.get("/api/activity")
        assert rows[4]["error_code"] == "bad_thing"
        assert rows[0]["execution_plan"] == "serial-fallback" and rows[0]["image_count"] == 1
        assert (
            rows[0]["extra_metrics"] is not None
            and "new_string_field" not in rows[0]["extra_metrics"]
        )
        files = [Path(d.db_path + suffix) for suffix in ("", "-wal", "-shm")]
        blobs = {str(f): f.read_bytes() for f in files if f.exists()}
        assert str(files[1]) in blobs  # WAL present while running
    finally:
        d.server.stop()
    for f in (Path(d.db_path + s) for s in ("", "-wal", "-shm")):
        if f.exists():
            blobs[str(f) + " (after shutdown)"] = f.read_bytes()
    logs = "\n".join(
        f"{r.name} {r.levelname} {r.getMessage()} {r.exc_text or ''}" for r in caplog.records
    )
    assert logs  # we did capture something
    for name, canary in CANARIES.items():
        for fname, blob in blobs.items():
            assert canary.encode() not in blob, f"{name} leaked into {fname}"
        assert canary not in logs, f"{name} leaked into logs"
