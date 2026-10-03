"""WebSocket proxy behavior over real client, dashboard, and upstream sockets."""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from app.config import Settings
from app.websocket_proxy import WebSocketProxy

from .conftest import Dash, ServerThread, make_dash


@pytest.fixture
def upstream() -> Iterator[tuple[ServerThread, dict[str, Any]]]:
    state: dict[str, Any] = {"handler": None, "paths": []}

    async def endpoint(ws: WebSocket) -> None:
        state["paths"].append((ws.scope["path"], ws.scope["query_string"]))
        handler = state["handler"]
        await handler(ws)

    server = ServerThread(Starlette(routes=[WebSocketRoute("/{path:path}", endpoint)])).start()
    yield server, state
    server.stop()


@pytest.fixture
def ws_dash(tmp_path: Path, upstream: tuple[ServerThread, dict[str, Any]]) -> Iterator[Dash]:
    dash = make_dash(tmp_path, upstream[0].url)
    yield dash
    dash.server.stop()


def ws_url(dash: Dash, path: str) -> str:
    return dash.url.replace("http://", "ws://", 1) + path


def websocket_scope() -> dict[str, Any]:
    return {
        "type": "websocket",
        "path": "/ws/test",
        "raw_path": b"/ws/test",
        "query_string": b"",
        "headers": [],
        "subprotocols": [],
    }


def test_https_upstream_uses_wss_and_preserves_encoded_path() -> None:
    proxy = WebSocketProxy("https://gufo.example:8080/prefix", 1)
    assert (
        proxy._url(
            {
                "path": "/ws/a b",
                "raw_path": b"/ws/a%20b",
                "query_string": b"voice=foo&format=pcm",
            }
        )
        == "wss://gufo.example:8080/prefix/ws/a%20b?voice=foo&format=pcm"
    )


@pytest.mark.parametrize(
    "base_url", ["ws://gufo:8080", "wss://gufo:8080", "ftp://gufo", "http:///"]
)
def test_settings_reject_non_http_upstream(base_url: str) -> None:
    with pytest.raises(ValueError, match="GUFO_BASE_URL must be an http:// or https:// URL"):
        Settings(gufo_base_url=base_url)


def test_text_query_auth_headers_and_subprotocol(
    ws_dash: Dash, upstream: tuple[ServerThread, dict[str, Any]], caplog: Any
) -> None:
    seen: dict[str, Any] = {}
    token = "Bearer ws-private-canary"

    async def handler(ws: WebSocket) -> None:
        seen["auth"] = ws.headers.get("authorization")
        seen["custom"] = ws.headers.get("x-gufo-client")
        seen["host"] = ws.headers.get("host")
        seen["subprotocols"] = ws.scope["subprotocols"]
        await ws.accept(subprotocol="pcm")
        seen["text"] = await ws.receive_text()
        await ws.send_text("world")

    upstream[1]["handler"] = handler
    with (
        caplog.at_level(logging.DEBUG),
        connect(
            ws_url(ws_dash, "/ws/test?voice=foo&format=pcm"),
            additional_headers={"Authorization": token, "X-Gufo-Client": "voice"},
            subprotocols=["pcm", "text"],
            open_timeout=3,
            close_timeout=2,
        ) as client,
    ):
        assert client.subprotocol == "pcm"
        client.send("hello")
        assert client.recv(timeout=3) == "world"
    assert upstream[1]["paths"] == [("/ws/test", b"voice=foo&format=pcm")]
    assert seen["auth"] == token
    assert seen["custom"] == "voice"
    assert seen["host"] == upstream[0].url.removeprefix("http://")
    assert seen["subprotocols"] == ["pcm", "text"]
    assert seen["text"] == "hello"
    assert token not in caplog.text
    assert ws_dash.rows() == []


def test_binary_message_boundaries(
    ws_dash: Dash, upstream: tuple[ServerThread, dict[str, Any]]
) -> None:
    sent = [bytes(range(256)), b"\x00\xff\x80\x00", b""]
    received: list[bytes] = []

    async def handler(ws: WebSocket) -> None:
        await ws.accept()
        for _ in sent:
            received.append(await ws.receive_bytes())
        for payload in reversed(sent):
            await ws.send_bytes(payload)

    upstream[1]["handler"] = handler
    with connect(ws_url(ws_dash, "/v1/audio/speech/stream"), open_timeout=3) as client:
        for payload in sent:
            client.send(payload)
        assert [client.recv(timeout=3) for _ in sent] == list(reversed(sent))
    assert received == sent


def test_client_disconnect_closes_upstream(
    ws_dash: Dash, upstream: tuple[ServerThread, dict[str, Any]]
) -> None:
    closed = threading.Event()
    seen: dict[str, Any] = {}

    async def handler(ws: WebSocket) -> None:
        await ws.accept()
        try:
            await ws.receive_text()
        except WebSocketDisconnect as exc:
            seen["code"] = exc.code
            closed.set()

    upstream[1]["handler"] = handler
    with connect(ws_url(ws_dash, "/ws/test"), open_timeout=3) as client:
        client.close(code=4001, reason="finished")
    assert closed.wait(3)
    assert seen["code"] == 4001


def test_upstream_disconnect_closes_client(
    ws_dash: Dash, upstream: tuple[ServerThread, dict[str, Any]]
) -> None:
    async def handler(ws: WebSocket) -> None:
        await ws.accept()
        await ws.close(code=4002, reason="upstream finished")

    upstream[1]["handler"] = handler
    with connect(ws_url(ws_dash, "/ws/test"), open_timeout=3) as client:
        with pytest.raises(ConnectionClosed) as closed:
            client.recv(timeout=3)
        assert closed.value.rcvd is not None
        assert closed.value.rcvd.code == 4002
        assert closed.value.rcvd.reason == "upstream finished"


def test_upstream_unavailable(tmp_path: Path) -> None:
    dash = make_dash(tmp_path, "http://127.0.0.1:9", upstream_connect_timeout=1)
    try:
        with connect(ws_url(dash, "/ws/test"), open_timeout=3) as client:
            with pytest.raises(ConnectionClosed) as closed:
                client.recv(timeout=3)
            assert closed.value.rcvd is not None
            assert closed.value.rcvd.code == 1011
    finally:
        dash.server.stop()


@pytest.mark.parametrize("fail_on", ["accept", "message"])
def test_client_send_failure_closes_upstream_without_error(
    upstream: tuple[ServerThread, dict[str, Any]], fail_on: str
) -> None:
    closed = threading.Event()

    async def handler(ws: WebSocket) -> None:
        await ws.accept()
        if fail_on == "message":
            await ws.send_text("trigger client send")
        try:
            await ws.receive_text()
        except WebSocketDisconnect:
            closed.set()

    upstream[1]["handler"] = handler

    async def scenario() -> None:
        first = True
        waiting = asyncio.Event()

        async def receive() -> dict[str, Any]:
            nonlocal first
            if first:
                first = False
                return {"type": "websocket.connect"}
            await waiting.wait()
            raise AssertionError("receive should have been cancelled")

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "websocket.accept" and fail_on == "accept":
                raise OSError("client disconnected")
            if message["type"] == "websocket.send" and fail_on == "message":
                raise OSError("client disconnected")

        await asyncio.wait_for(
            WebSocketProxy(upstream[0].url, 2)(websocket_scope(), receive, send), 5
        )

    asyncio.run(scenario())
    assert closed.wait(3)


def test_statusless_client_close_is_normal_upstream(
    upstream: tuple[ServerThread, dict[str, Any]],
) -> None:
    seen: dict[str, int] = {}
    closed = threading.Event()

    async def handler(ws: WebSocket) -> None:
        await ws.accept()
        try:
            await ws.receive_text()
        except WebSocketDisconnect as exc:
            seen["code"] = exc.code
            closed.set()

    upstream[1]["handler"] = handler

    async def scenario() -> None:
        events = iter(
            [{"type": "websocket.connect"}, {"type": "websocket.disconnect", "code": 1005}]
        )

        async def receive() -> dict[str, Any]:
            return next(events)

        async def send(message: dict[str, Any]) -> None:
            assert message["type"] in ("websocket.accept", "websocket.close")

        await asyncio.wait_for(
            WebSocketProxy(upstream[0].url, 2)(websocket_scope(), receive, send), 5
        )

    asyncio.run(scenario())
    assert closed.wait(3)
    assert seen["code"] == 1000


def test_lifecycle_logs_without_websocket_content(
    tmp_path: Path, upstream: tuple[ServerThread, dict[str, Any]], caplog: Any
) -> None:
    async def handler(ws: WebSocket) -> None:
        await ws.accept()
        assert await ws.receive_text() == "message-canary"
        await ws.send_text("reply-canary")

    upstream[1]["handler"] = handler
    with caplog.at_level(logging.DEBUG, logger="uvicorn.error"):
        dash = make_dash(tmp_path, upstream[0].url)
        try:
            with connect(
                ws_url(dash, "/ws/test?key=query-canary"),
                additional_headers={"Authorization": "Bearer header-canary"},
                open_timeout=3,
            ) as client:
                client.send("message-canary")
                assert client.recv(timeout=3) == "reply-canary"
        finally:
            dash.server.stop()
    assert "Application startup complete" in caplog.text
    assert "Finished server process" in caplog.text
    assert not any(
        value in caplog.text
        for value in ("query-canary", "header-canary", "message-canary", "reply-canary")
    )


def test_dashboard_shutdown_closes_upstream(
    tmp_path: Path, upstream: tuple[ServerThread, dict[str, Any]]
) -> None:
    closed = threading.Event()

    async def handler(ws: WebSocket) -> None:
        await ws.accept()
        try:
            await ws.receive_text()
        except WebSocketDisconnect:
            closed.set()

    upstream[1]["handler"] = handler
    dash = make_dash(tmp_path, upstream[0].url)
    try:
        with connect(ws_url(dash, "/ws/test"), open_timeout=3) as client:
            dash.server.stop()
            with pytest.raises(ConnectionClosed):
                client.recv(timeout=3)
        assert closed.wait(3)
    finally:
        if dash.server.thread.is_alive():
            dash.server.stop()


def test_both_directions_progress_independently(
    ws_dash: Dash, upstream: tuple[ServerThread, dict[str, Any]]
) -> None:
    async def handler(ws: WebSocket) -> None:
        await ws.accept()

        async def send() -> None:
            for i in range(20):
                await ws.send_bytes(bytes([i]))
                await asyncio.sleep(0)

        async def receive() -> None:
            for i in range(20):
                assert await ws.receive_text() == str(i)

        await asyncio.gather(send(), receive())

    upstream[1]["handler"] = handler
    with connect(ws_url(ws_dash, "/ws/test"), open_timeout=3) as client:
        sent = threading.Thread(target=lambda: [client.send(str(i)) for i in range(20)])
        sent.start()
        try:
            assert [client.recv(timeout=3) for _ in range(20)] == [bytes([i]) for i in range(20)]
        finally:
            sent.join(3)
        assert not sent.is_alive()


def test_local_api_and_invalid_host_never_reach_upstream(
    ws_dash: Dash, upstream: tuple[ServerThread, dict[str, Any]]
) -> None:
    async def handler(ws: WebSocket) -> None:
        await ws.accept()

    upstream[1]["handler"] = handler
    with (
        pytest.raises(InvalidStatus) as rejected,
        connect(ws_url(ws_dash, "/api/status"), open_timeout=3),
    ):
        pass
    assert rejected.value.response.status_code == 403
    with socket.create_connection(("127.0.0.1", ws_dash.server.port), timeout=3) as sock:
        sock.sendall(
            b"GET /ws/test HTTP/1.1\r\n"
            b"Host: attacker.example\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            b"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        assert b"403 Forbidden" in sock.recv(1024)
    assert upstream[1]["paths"] == []
    with ws_dash.client() as client:
        assert client.get("/api/status").status_code == 200
