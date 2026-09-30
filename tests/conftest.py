"""Test harness: a fake Gufo built from tests/fixtures/gufo and real uvicorn servers."""

from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from app.config import Settings
from app.main import create_app

FIXTURES = Path(__file__).parent / "fixtures" / "gufo"
DROP_META_HEADERS = {"content-length", "transfer-encoding", "connection"}


def fixture_bytes(name: str) -> bytes:
    for ext in (".json", ".sse", ".txt"):
        p = FIXTURES / f"{name}{ext}"
        if p.exists():
            return p.read_bytes()
    raise FileNotFoundError(name)


def fixture_json(name: str) -> Any:
    return json.loads(fixture_bytes(name))


def fixture_meta(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.meta.json").read_text())


def fixture_headers(name: str) -> list[tuple[str, str]]:
    return [
        (k, v)
        for k, v in fixture_meta(name)["headers"].items()
        if k.lower() not in DROP_META_HEADERS
    ]


def split_sse_events(body: bytes) -> list[bytes]:
    """Split a fixture SSE body into raw events (each with its blank line)."""
    parts = body.split(b"\n\n")
    events = [p + b"\n\n" for p in parts[:-1]]
    if parts[-1]:
        events.append(parts[-1])
    return events


def strip_usage_event(body: bytes) -> bytes:
    """What Gufo sends without include_usage: the same stream minus the usage event."""
    out = []
    for ev in split_sse_events(body):
        if b'"choices":[],"usage"' in ev:
            continue
        out.append(ev)
    return b"".join(out)


# --------------------------------------------------------------------------- #
# Fake Gufo
# --------------------------------------------------------------------------- #


@dataclass
class Received:
    method: str
    path: str
    query: str
    headers: list[tuple[str, str]]
    body: bytes


@dataclass
class FakeGufo:
    requests: list[Received] = field(default_factory=list)
    # override(request, body) -> Response | None ; None = default behaviour
    override: Callable[[Request, bytes], Any] | None = None
    chunk_size: int | None = None  # split SSE bodies into chunks of this size
    stream_closed_early: threading.Event = field(default_factory=threading.Event)
    stream_finished: threading.Event = field(default_factory=threading.Event)
    model: str = "fixture-model-a"
    metrics_text: bytes = field(default_factory=lambda: fixture_bytes("metrics_before"))
    ready: bool = True

    def sse(self, body: bytes, name: str) -> Response:
        size = self.chunk_size

        async def gen() -> Any:
            if size:
                for i in range(0, len(body), size):
                    yield body[i : i + size]
                    await asyncio.sleep(0)
            else:
                for ev in split_sse_events(body):
                    yield ev
                    await asyncio.sleep(0)

        return StreamingResponse(gen(), status_code=200, headers=dict(fixture_headers(name)))

    def fixed(self, name: str, status: int | None = None) -> Response:
        meta = fixture_meta(name)
        return Response(
            fixture_bytes(name),
            status_code=status or meta["status"],
            headers=dict(fixture_headers(name)),
        )

    async def handle(self, request: Request) -> Response:
        body = await request.body()
        self.requests.append(
            Received(
                request.method,
                request.url.path,
                request.url.query,
                [(k.decode(), v.decode()) for k, v in request.headers.raw],
                body,
            )
        )
        if self.override is not None:
            res = self.override(request, body)
            if asyncio.iscoroutine(res):
                res = await res
            if res is not None:
                return res  # type: ignore[no-any-return]
        path, method = request.url.path, request.method
        if method == "OPTIONS":
            return Response(status_code=204, headers=dict(fixture_headers("health")))
        if path == "/health":
            return self.fixed("health")
        if path == "/ready":
            if not self.ready:
                return Response(b'{"status":"loading"}', status_code=503)
            return self.fixed("ready")
        if path == "/v1/models":
            return self.fixed("models")
        if path == "/metrics":
            return Response(self.metrics_text, headers=dict(fixture_headers("metrics_before")))
        if path == "/slots":
            return Response(
                b'[{"id":0,"prompt":""}]',
                headers={"Content-Type": "application/json", "X-Request-ID": "r99"},
            )
        if method != "POST":
            return Response(b'{"error":"not found"}', status_code=404)
        try:
            req = json.loads(body)
        except ValueError:
            return self.fixed("error_bad_json")
        if not isinstance(req, dict):
            return self.fixed("error_bad_json")
        if req.get("model") == "nope":
            return self.fixed("error_unknown_model")
        stream = req.get("stream") is True
        so = req.get("stream_options")
        usage = isinstance(so, dict) and so.get("include_usage") is True
        if path == "/v1/chat/completions":
            if stream:
                if usage:
                    return self.sse(
                        fixture_bytes("chat_stream_include_usage"), "chat_stream_include_usage"
                    )
                return self.sse(fixture_bytes("chat_stream_no_usage"), "chat_stream_no_usage")
            if "image_url" in body.decode(errors="replace"):
                return self.fixed("chat_vision_nonstream")
            return self.fixed("chat_nonstream")
        if path == "/v1/completions":
            if stream:
                raw = fixture_bytes("completions_stream_include_usage")
                return self.sse(
                    raw if usage else strip_usage_event(raw), "completions_stream_include_usage"
                )
            return self.fixed("completions_nonstream")
        if path == "/v1/responses":
            if stream:
                return self.sse(fixture_bytes("responses_stream"), "responses_stream")
            return self.fixed("responses_nonstream")
        if path == "/v1/messages":
            return self.fixed("messages_stream" if stream else "messages_nonstream")
        if path == "/completion":
            return self.fixed(
                "native_completion_stream" if stream else "native_completion_nonstream"
            )
        return Response(b'{"error":"not found"}', status_code=404)

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route(
                    "/{path:path}",
                    self.handle,
                    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
                )
            ]
        )


# --------------------------------------------------------------------------- #
# Real servers in threads
# --------------------------------------------------------------------------- #


class ServerThread:
    def __init__(self, app: Any) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.app = app
        config = uvicorn.Config(
            app,
            log_config=None,
            access_log=False,
            server_header=False,
            date_header=False,
            lifespan="on",
            timeout_graceful_shutdown=5,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]})

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> ServerThread:
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.01)
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(15)


@pytest.fixture
def fake() -> Iterator[FakeGufo]:
    yield FakeGufo()


@pytest.fixture
def fake_server(fake: FakeGufo) -> Iterator[ServerThread]:
    srv = ServerThread(fake.app()).start()
    yield srv
    srv.stop()


@dataclass
class Dash:
    server: ServerThread
    db_path: str
    settings: Settings

    @property
    def url(self) -> str:
        return self.server.url

    @property
    def ctx(self) -> Any:
        return self.server.app.dashboard.state.ctx

    def client(self, **kw: Any) -> httpx.Client:
        return httpx.Client(base_url=self.url, timeout=30, **kw)

    def rows(self, where: str = "", params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [
                dict(r)
                for r in conn.execute(f"SELECT * FROM request_stats {where} ORDER BY id", params)
            ]
        finally:
            conn.close()

    def wait_rows(self, n: int, timeout: float = 10.0) -> list[dict[str, Any]]:
        deadline = time.time() + timeout
        while True:
            rows = self.rows()
            if len(rows) >= n:
                return rows
            if time.time() > deadline:
                raise AssertionError(f"expected {n} rows, got {len(rows)}")
            time.sleep(0.02)


def make_dash(tmp_path: Path, upstream: str, **overrides: Any) -> Dash:
    db_path = str(tmp_path / "dash.sqlite")
    settings = Settings(
        gufo_base_url=upstream,
        database_path=db_path,
        enable_poller=overrides.pop("enable_poller", False),
        poll_interval_seconds=overrides.pop("poll_interval_seconds", 0.2),
        **overrides,
    )
    srv = ServerThread(create_app(settings)).start()
    return Dash(srv, db_path, settings)


@pytest.fixture
def dash(tmp_path: Path, fake_server: ServerThread) -> Iterator[Dash]:
    d = make_dash(tmp_path, fake_server.url)
    yield d
    d.server.stop()


MODEL = "fixture-model-a"
