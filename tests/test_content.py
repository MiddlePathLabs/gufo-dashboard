"""Content capture boundaries, forwarding, retention, and deletion."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from starlette.responses import Response

from app.content import ContentCapture, TextBuffer
from app.db import StatsWriter, init_db, now_ms

from .conftest import FakeGufo, ServerThread, make_dash


def capture(kind: str, request: dict[str, Any], limit: int = 65536) -> ContentCapture:
    return ContentCapture(kind, json.dumps(request).encode(), True, limit)


def test_allowlist_and_latest_user() -> None:
    c = capture(
        "chat",
        {
            "messages": [
                {"role": "system", "content": "SYSTEM-SECRET"},
                {"role": "user", "content": "OLD-QUESTION"},
                {"role": "tool", "content": "TOOL-SECRET"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "latest question"},
                        {"type": "image_url", "image_url": {"url": "IMAGE-SECRET"}},
                        {"type": "input_file", "file_data": "FILE-SECRET"},
                        {"type": "tool_result", "content": "TOOL-RESULT-SECRET"},
                    ],
                },
            ]
        },
    )
    c.body(
        {
            "choices": [
                {
                    "message": {
                        "content": "answer",
                        "reasoning_content": "REASONING-SECRET",
                        "tool_calls": [{"arguments": "ARGS-SECRET"}],
                    }
                }
            ]
        }
    )
    result = c.record(complete=True)
    assert result["question"] == "latest question"
    assert result["answer"] == "answer"
    assert "SECRET" not in json.dumps(result)
    assert result["status"] == "complete"


@pytest.mark.parametrize(
    ("kind", "payload", "body"),
    [
        ("completions", {"prompt": "question"}, {"choices": [{"text": "answer"}]}),
        ("native", {"prompt": "question"}, {"content": "answer"}),
        (
            "messages",
            {"messages": [{"role": "user", "content": "question"}]},
            {
                "content": [
                    {"type": "text", "text": "answer"},
                    {"type": "thinking", "thinking": "SECRET"},
                ]
            },
        ),
        (
            "responses",
            {"input": [{"role": "user", "content": [{"type": "input_text", "text": "question"}]}]},
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "answer"}],
                    },
                    {"type": "reasoning", "summary": [{"text": "SECRET"}]},
                ],
            },
        ),
    ],
)
def test_endpoint_text_shapes(kind: str, payload: dict[str, Any], body: dict[str, Any]) -> None:
    c = capture(kind, payload)
    c.body(body)
    result = c.record(complete=True)
    assert (result["question"], result["answer"], result["status"]) == (
        "question",
        "answer",
        "complete",
    )


def test_streams_unicode_limit_and_partial() -> None:
    c = capture("chat", {"messages": [{"role": "user", "content": "hello"}]}, 7)
    c.event(
        json.dumps(
            {
                "choices": [
                    {"index": 0, "delta": {"content": "🙂🙂", "reasoning_content": "SECRET"}},
                    {"index": 1, "delta": {"content": "OTHER"}},
                ]
            }
        )
    )
    result = c.record(complete=False)
    assert result["answer"] == "🙂"
    assert result["answer_truncated"] and result["status"] == "partial"
    c.event("[DONE]")
    assert c.record(complete=True)["status"] == "complete"
    b = TextBuffer(0)
    b.append("hello")
    assert b.text() is None and b.truncated


def test_responses_terminal_does_not_duplicate_and_messages_exclude_tools() -> None:
    c = capture("responses", {"input": "question"})
    c.event('{"type":"response.output_text.delta","delta":"answer"}')
    c.event('{"type":"response.reasoning_text.delta","delta":"SECRET"}')
    c.event(
        '{"type":"response.completed","response":{"status":"completed","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"answer"}]}]}}'
    )
    assert c.record(complete=True)["answer"] == "answer"
    c = capture("messages", {"messages": [{"role": "user", "content": "question"}]})
    c.event('{"type":"content_block_start","content_block":{"type":"text","text":"a"}}')
    c.event('{"type":"content_block_delta","delta":{"type":"text_delta","text":"b"}}')
    c.event(
        '{"type":"content_block_delta","delta":{"type":"input_json_delta","partial_json":"SECRET"}}'
    )
    c.event('{"type":"message_stop"}')
    assert c.record(complete=True)["answer"] == "ab"


@pytest.mark.parametrize("stream", [False, True])
def test_opt_in_proxy_privacy_and_deletion(
    tmp_path: Path,
    fake: FakeGufo,
    fake_server: ServerThread,
    caplog: pytest.LogCaptureFixture,
    stream: bool,
) -> None:
    caplog.set_level(logging.DEBUG)
    output = {
        "choices": [
            {
                "index": 0,
                "message": {"content": "AUDIT-ANSWER", "reasoning_content": "REASONING-SECRET"},
            }
        ]
    }
    raw = json.dumps(output).encode()
    if stream:
        raw = b'data: {"choices":[{"index":0,"delta":{"content":"AUDIT-ANSWER","reasoning_content":"REASONING-SECRET"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

    def override(request: Any, body: bytes) -> Response:
        return Response(raw, media_type="text/event-stream" if stream else "application/json")

    fake.override = override
    d = make_dash(tmp_path, fake_server.url, capture_content=True)
    try:
        with d.client() as client:
            response = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer AUTH-SECRET"},
                json={
                    "model": "test",
                    "stream": stream,
                    "stream_options": {"include_usage": True},
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "AUDIT-QUESTION"},
                                {"type": "image_url", "image_url": {"url": "IMAGE-SECRET"}},
                                {"type": "file", "data": "FILE-SECRET"},
                            ],
                        }
                    ],
                },
            )
            assert response.content == raw
            ident = d.wait_rows(1)[0]["id"]
            content = client.get(f"/api/requests/{ident}/content")
            assert content.headers["cache-control"] == "no-store"
            assert content.json()["question"] == "AUDIT-QUESTION"
            assert content.json()["answer"] == "AUDIT-ANSWER"
            assert content.json()["status"] == "complete"
            assert "AUDIT-" not in client.get("/api/activity").text
            assert len(client.get("/api/activity?filter=content").json()["items"]) == 1
            assert client.delete(f"/api/requests/{ident}/content").status_code == 200
            assert client.get(f"/api/requests/{ident}/content").json()["status"] == "deleted"
            assert client.get(f"/api/requests/{ident}").status_code == 200
            assert not client.get("/api/activity?filter=content").json()["items"]
            assert client.post("/api/content/clear", json={"confirm": "no"}).status_code == 400
            assert client.post("/api/content/clear", json={"confirm": "clear"}).status_code == 200
        blobs = b"".join(p.read_bytes() for p in tmp_path.glob("dash.sqlite*"))
        for value in [b"AUTH-SECRET", b"IMAGE-SECRET", b"FILE-SECRET", b"REASONING-SECRET"]:
            assert value not in blobs
        logs = "\n".join(r.getMessage() for r in caplog.records)
        assert "AUDIT-QUESTION" not in logs and "AUDIT-ANSWER" not in logs
    finally:
        d.server.stop()


def test_retention_migration_clear_and_queue_budget(tmp_path: Path) -> None:
    path = str(tmp_path / "test.sqlite")
    init_db(path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_version SET version=1")
    conn.execute("DROP TABLE request_content")
    conn.commit()
    init_db(path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 2

    async def run() -> None:
        w = StatsWriter(path, 10, 90, 7)
        w.start()
        c = capture("responses", {"input": "question"})
        c.body({"status": "completed", "output": []})
        rec = {"completed_at_ms": now_ms() - 8 * 86_400_000, "_content": c.record(complete=True)}
        w.submit(rec)
        await w.flush()
        await w.prune()
        assert conn.execute("SELECT status, question FROM request_content").fetchone() == (
            "expired",
            None,
        )
        assert conn.execute("SELECT count(*) FROM request_stats").fetchone()[0] == 1
        w.content_queue_bytes = 8 * 1024 * 1024
        w.submit({"completed_at_ms": now_ms(), "_content": c.record(complete=True)})
        await w.flush()
        assert conn.execute(
            "SELECT status, question FROM request_content ORDER BY request_id DESC"
        ).fetchone() == ("dropped", None)
        await w.clear()
        assert conn.execute("SELECT count(*) FROM request_content").fetchone()[0] == 0
        await w.stop(5)

    asyncio.run(run())
    conn.close()


def test_bulk_clear_suppresses_inflight_content(
    tmp_path: Path, fake: FakeGufo, fake_server: ServerThread
) -> None:
    import threading

    from starlette.responses import StreamingResponse

    gate = threading.Event()
    started = threading.Event()

    async def gen() -> Any:
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        started.set()
        while not gate.is_set():
            await asyncio.sleep(0.01)
        yield b"data: [DONE]\n\n"

    fake.override = lambda request, body: StreamingResponse(gen(), media_type="text/event-stream")
    d = make_dash(tmp_path, fake_server.url, capture_content=True)

    def send() -> None:
        with d.client() as client:
            client.post(
                "/v1/chat/completions",
                json={
                    "stream": True,
                    "messages": [{"role": "user", "content": "INFLIGHT-QUESTION"}],
                },
            )

    worker = threading.Thread(target=send)
    try:
        worker.start()
        assert started.wait(3)
        with d.client() as client:
            assert client.post("/api/content/clear", json={"confirm": "clear"}).status_code == 200
            gate.set()
            worker.join(3)
            ident = d.wait_rows(1)[0]["id"]
            data = client.get(f"/api/requests/{ident}/content").json()
            assert data["status"] == "deleted"
            assert data["question"] is None and data["answer"] is None
    finally:
        gate.set()
        worker.join(3)
        d.server.stop()


def test_disabled_and_attachment_only_capture() -> None:
    c = ContentCapture("chat", b'{"messages":[{"role":"user","content":"SECRET"}]}', False, 65536)
    c.body({"choices": [{"message": {"content": "SECRET"}}]})
    assert c.record(complete=True)["status"] == "disabled"
    assert c.record(complete=True)["question"] is None
    assert c.record(complete=True)["answer"] is None
    c = capture(
        "responses",
        {
            "input": [
                {"type": "input_text", "text": "question"},
                {"type": "input_file", "file_data": "SECRET"},
            ]
        },
    )
    assert c.question.text() == "question"
    c = capture(
        "chat",
        {
            "messages": [
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "SECRET"}}]}
            ]
        },
    )
    c.body({"choices": [{"message": {"tool_calls": [{"arguments": "SECRET"}]}}]})
    assert c.record(complete=True)["status"] == "unsupported"
