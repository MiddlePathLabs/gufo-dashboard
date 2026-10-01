"""Transparent reverse proxy to Gufo with side-channel stats extraction."""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from typing import Any

import anyio
import httpx
from starlette.requests import Request
from starlette.types import Receive, Scope, Send

from . import extract
from .content import ContentCapture
from .db import StatsWriter, now_ms
from .sse import SSEParser, parse_remainder

log = logging.getLogger("gufo_dashboard.proxy")

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
    "proxy-authenticate",
    "proxy-authorization",
}
# Paths logged verbatim; everything else is logged as "(proxy)".
KNOWN_ROUTES = {
    *extract.RECORDED_ENDPOINTS,
    "/v1/models",
    "/health",
    "/ready",
    "/metrics",
    "/slots",
    "/props",
}


def _connection_tokens(values: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for v in values:
        out.update(t.strip().lower() for t in v.split(",") if t.strip())
    return out


def filter_headers(
    items: list[tuple[str, str]], extra_drop: Iterable[str] = ()
) -> list[tuple[str, str]]:
    """Drop hop-by-hop headers and every header named in `Connection`."""
    drop = HOP_BY_HOP | _connection_tokens(v for k, v in items if k.lower() == "connection")
    drop |= {h.lower() for h in extra_drop}
    return [(k, v) for k, v in items if k.lower() not in drop]


# --------------------------------------------------------------------------- #
# In-flight tracking
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class InFlight:
    endpoint: str
    model: str | None
    is_streaming: bool
    started_at_ms: int
    phase: str = "waiting"
    prompt_progress: dict[str, int] | None = None


class InFlightTracker:
    def __init__(self) -> None:
        self._items: dict[int, InFlight] = {}
        self._ids = itertools.count(1)

    def add(self, item: InFlight) -> int:
        key = next(self._ids)
        self._items[key] = item
        return key

    def update_progress(self, key: int, inspector: extract.StreamInspector) -> None:
        item = self._items.get(key)
        if item is not None:
            item.prompt_progress = inspector.prompt_progress
            if inspector.first_token_seen:
                item.phase = "generating"
            elif inspector.prompt_progress is not None:
                item.phase = "prefill"

    def remove(self, key: int) -> None:
        self._items.pop(key, None)

    def __len__(self) -> int:
        return len(self._items)

    def snapshot(self) -> list[InFlight]:
        return list(self._items.values())


# --------------------------------------------------------------------------- #
# Response streaming with disconnect detection
# --------------------------------------------------------------------------- #


class ProxiedResponse:
    """ASGI response that streams chunks and reports client disconnects.

    `on_close(disconnected)` always runs (shielded) after the body ends, the
    client goes away, or an error occurs.
    """

    def __init__(
        self,
        status: int,
        raw_headers: list[tuple[bytes, bytes]],
        chunks: AsyncIterator[bytes],
        on_close: Callable[[bool], Any],
    ) -> None:
        self.status = status
        self.raw_headers = raw_headers
        self.chunks = chunks
        self.on_close = on_close

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        disconnected = False
        try:
            async with anyio.create_task_group() as tg:

                async def watch() -> None:
                    nonlocal disconnected
                    while True:
                        msg = await receive()
                        if msg["type"] == "http.disconnect":
                            disconnected = True
                            tg.cancel_scope.cancel()
                            return

                tg.start_soon(watch)
                try:
                    await send(
                        {
                            "type": "http.response.start",
                            "status": self.status,
                            "headers": self.raw_headers,
                        }
                    )
                    if scope.get("method") != "HEAD":
                        async for chunk in self.chunks:
                            if chunk:
                                await send(
                                    {"type": "http.response.body", "body": chunk, "more_body": True}
                                )
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                except OSError:
                    disconnected = True
                tg.cancel_scope.cancel()
        finally:
            with anyio.CancelScope(shield=True):
                await self.on_close(disconnected)


def json_error(
    status: int, message: str, code: str
) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    body = json.dumps(
        {"error": {"message": message, "type": "upstream_error", "code": code}}
    ).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    return status, headers, body


# --------------------------------------------------------------------------- #
# The proxy
# --------------------------------------------------------------------------- #


class Proxy:
    def __init__(
        self,
        base_url: str,
        client: httpx.AsyncClient,
        writer: StatsWriter,
        inflight: InFlightTracker,
        context_lengths: Callable[[], dict[str, int]],
        max_inspect_body_bytes: int = 64 * 1024 * 1024,
        *,
        capture_content: bool = False,
        content_max_bytes: int = 64 * 1024,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client
        self.writer = writer
        self.inflight = inflight
        self.context_lengths = context_lengths
        self.max_inspect = max_inspect_body_bytes
        self.capture_content = capture_content
        self.content_max_bytes = content_max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        t0 = time.monotonic()
        started = now_ms()
        path = request.url.path
        kind = extract.RECORDED_ENDPOINTS.get(path) if request.method == "POST" else None
        body = await request.body()

        info: extract.RequestInfo | None = None
        if kind is not None:
            try:
                info = extract.analyze_request(kind, body)
            except Exception as exc:
                self._extraction_error(exc)
                info = extract.RequestInfo(kind=kind, body=body)
        capture = (
            ContentCapture(kind, body, self.capture_content, self.content_max_bytes)
            if kind
            else None
        )
        generation = self.writer.content_generation
        forward_body = info.body if info is not None else body

        req_headers = filter_headers(
            [(k.decode("latin-1"), v.decode("latin-1")) for k, v in request.headers.raw],
            extra_drop=("host", "content-length", "accept-encoding"),
        )
        req_headers.append(("accept-encoding", "identity"))
        url = httpx.URL(self.base_url + path, query=request.url.query.encode("latin-1"))
        upstream_req = self.client.build_request(
            request.method, url, headers=req_headers, content=forward_body
        )

        flight_key = None
        if info is not None:
            flight_key = self.inflight.add(InFlight(path, info.model, info.is_streaming, started))

        try:
            resp = await self.client.send(upstream_req, stream=True)
        except httpx.HTTPError as exc:
            code = (
                "upstream_unreachable"
                if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)
                else "upstream_error"
            )
            message = (
                "Gufo upstream unreachable"
                if code == "upstream_unreachable"
                else "Gufo upstream error"
            )
            status, headers, err_body = json_error(502, message, code)
            if flight_key is not None:
                self.inflight.remove(flight_key)
            if info is not None:
                self._record(
                    {"error_code": code},
                    path=path,
                    info=info,
                    status=status,
                    request_id=None,
                    started=started,
                    t0=t0,
                    ttfb=None,
                    first_token=None,
                    cancelled=False,
                    content=capture.record(complete=False, failed=True) if capture else None,
                    generation=generation,
                )
            scope.setdefault("state", {})["gufo_log"] = {"status": status, "route": path}
            await _send_simple(send, status, headers, err_body)
            return

        exchange = _Exchange(self, resp, path, info, started, t0, flight_key)
        exchange.capture = capture
        exchange.generation = generation
        scope.setdefault("state", {})["gufo_log"] = {
            "status": resp.status_code,
            "request_id": extract.as_str(resp.headers.get("x-request-id")),
        }
        await ProxiedResponse(
            resp.status_code, exchange.response_headers(), exchange.chunks, exchange.close
        )(scope, receive, send)

    def _extraction_error(self, exc: BaseException) -> None:
        self.writer.counters.stats_extraction_errors += 1
        log.warning("stats extraction failed: %s", type(exc).__name__)

    def _record(
        self,
        fields: dict[str, Any],
        *,
        path: str,
        info: extract.RequestInfo,
        status: int,
        request_id: str | None,
        started: int,
        t0: float,
        ttfb: float | None,
        first_token: float | None,
        cancelled: bool,
        content: dict[str, Any] | None = None,
        generation: int | None = None,
    ) -> None:
        try:
            rec = extract.finalize_record(
                fields,
                endpoint=path,
                request_model=info.model,
                is_streaming=info.is_streaming,
                image_count=info.image_count,
                http_status=status,
                gufo_request_id=request_id,
                started_at_ms=started,
                completed_at_ms=now_ms(),
                proxy_ttfb_ms=ttfb,
                proxy_first_token_ms=first_token,
                total_request_ms=(time.monotonic() - t0) * 1000.0,
                context_lengths=self.context_lengths(),
                cancelled=cancelled,
            )
        except Exception as exc:
            self._extraction_error(exc)
            return
        if content is not None:
            if generation != self.writer.content_generation:
                content = {**content, "question": None, "answer": None, "status": "deleted"}
            rec["_content"] = content
        self.writer.submit(rec)


async def _send_simple(
    send: Send, status: int, headers: list[tuple[bytes, bytes]], body: bytes
) -> None:
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


class _Exchange:
    """State for one proxied request/response pair."""

    def __init__(
        self,
        proxy: Proxy,
        resp: httpx.Response,
        path: str,
        info: extract.RequestInfo | None,
        started: int,
        t0: float,
        flight_key: int | None,
    ) -> None:
        self.proxy = proxy
        self.resp = resp
        self.path = path
        self.info = info
        self.started = started
        self.t0 = t0
        self.flight_key = flight_key
        self.ttfb = (time.monotonic() - t0) * 1000.0
        self.first_token: float | None = None
        self.completed = False
        self.upstream_failed = False
        self.fields: dict[str, Any] = {}
        self.capture: ContentCapture | None = None
        self.generation = proxy.writer.content_generation

        ctype = resp.headers.get("content-type", "").lower()
        encoding = resp.headers.get("content-encoding", "identity").lower().strip()
        self.is_sse = "text/event-stream" in ctype
        # Inspect only recorded endpoints with a plain (uncompressed) body.
        self.inspecting = info is not None and encoding in ("", "identity")
        self.strip = bool(self.inspecting and self.is_sse and info is not None and info.injected)
        self.parser = SSEParser() if self.inspecting and self.is_sse else None
        self.inspector = (
            extract.StreamInspector(info.kind, self.strip)
            if self.parser is not None and info is not None
            else None
        )
        self.body_buf: bytearray | None = (
            bytearray() if self.inspecting and not self.is_sse else None
        )
        self.chunks = self._iterate()

    def response_headers(self) -> list[tuple[bytes, bytes]]:
        extra = ["content-length"] if self.strip else []
        items = filter_headers(list(self.resp.headers.multi_items()), extra_drop=extra)
        return [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in items]

    def _fail_inspection(self, exc: BaseException) -> None:
        self.proxy._extraction_error(exc)
        self.inspecting = False
        self.inspector = None
        self.body_buf = None

    async def _iterate(self) -> AsyncGenerator[bytes]:
        try:
            async for chunk in self.resp.aiter_raw():
                if self.parser is not None:
                    out = self._on_sse_chunk(chunk)
                    if out:
                        yield out
                else:
                    if self.body_buf is not None:
                        if len(self.body_buf) + len(chunk) > self.proxy.max_inspect:
                            self.body_buf = None
                            if self.capture:
                                self.capture.failed = True
                        else:
                            self.body_buf += chunk
                    yield chunk
        except httpx.HTTPError:
            self.upstream_failed = True
            raise
        if self.parser is not None:
            rest = self.parser.take_remainder()
            if rest:
                if self.inspector is not None and not self.parser.overflowed:
                    ev = parse_remainder(rest)
                    if ev is not None and ev.data is not None:
                        self._inspect(ev.data)
                yield rest
        self.completed = True

    def _on_sse_chunk(self, chunk: bytes) -> bytes:
        parser = self.parser
        assert parser is not None
        if parser.overflowed:
            return chunk
        events = parser.feed(chunk)
        if parser.overflowed:
            # Giant event: give up on inspection, release held bytes.
            self.inspector = None
            if self.capture:
                self.capture.failed = True
            held = b"".join(e.raw for e in events) + parser.take_remainder()
            return held if self.strip else chunk
        out: list[bytes] = []
        for ev in events:
            drop = False
            if self.inspector is not None and ev.data is not None:
                drop = self._inspect(ev.data)
            if self.strip and not drop:
                out.append(ev.raw)
        return b"".join(out) if self.strip else chunk

    def _inspect(self, data: str) -> bool:
        if self.capture:
            self.capture.event(data)
        inspector = self.inspector
        if inspector is None:
            return False
        try:
            drop, first = inspector.on_data(data)
        except Exception as exc:
            self.proxy._extraction_error(exc)
            self.inspector = None
            return False
        if self.flight_key is not None:
            self.proxy.inflight.update_progress(self.flight_key, inspector)
        if first and self.first_token is None:
            self.first_token = (time.monotonic() - self.t0) * 1000.0
        return drop

    async def close(self, disconnected: bool) -> None:
        with contextlib.suppress(Exception):
            await self.chunks.aclose()
        with contextlib.suppress(Exception):
            await self.resp.aclose()  # stops Gufo generating on disconnect
        if self.flight_key is not None:
            self.proxy.inflight.remove(self.flight_key)
        if self.info is None:
            return
        cancelled = disconnected and not self.completed
        fields: dict[str, Any] = {}
        try:
            if self.inspector is not None:
                fields = self.inspector.fields
            elif self.body_buf is not None and self.completed and self.body_buf:
                fields = self._extract_body(bytes(self.body_buf))
        except Exception as exc:
            self._fail_inspection(exc)
            fields = {}
        finally:
            self.body_buf = None
        if self.upstream_failed and "error_code" not in fields:
            fields["error_code"] = "upstream_read_error"
        self.proxy._record(
            fields,
            path=self.path,
            info=self.info,
            status=self.resp.status_code,
            request_id=self.resp.headers.get("x-request-id"),
            started=self.started,
            t0=self.t0,
            ttfb=self.ttfb,
            first_token=self.first_token,
            cancelled=cancelled,
            content=self.capture.record(
                complete=self.completed and (self.inspecting or not self.capture.enabled),
                failed=self.upstream_failed or self.resp.status_code >= 400,
            )
            if self.capture
            else None,
            generation=self.generation,
        )

    def _extract_body(self, raw: bytes) -> dict[str, Any]:
        assert self.info is not None
        try:
            obj = extract.loads_strict(raw)
        except (ValueError, RecursionError):
            return {}
        if self.resp.status_code >= 400:
            code = extract.extract_error_code(obj)
            return {"error_code": code} if code else {}
        if self.capture:
            self.capture.body(obj)
        return extract.BODY_EXTRACTORS[self.info.kind](obj)
