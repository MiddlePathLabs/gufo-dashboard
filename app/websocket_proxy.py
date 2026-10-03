"""ASGI WebSocket passthrough to Gufo; no frame content is inspected or stored."""

from __future__ import annotations

from urllib.parse import quote, urlsplit, urlunsplit

import anyio
from starlette.types import Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from .proxy import filter_headers


class WebSocketProxy:
    def __init__(self, base_url: str, connect_timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.connect_timeout = connect_timeout

    def _url(self, scope: Scope) -> str:
        base = urlsplit(self.base_url)
        scheme = {"http": "ws", "https": "wss"}[base.scheme]
        path = scope.get("raw_path")
        # ASGI raw_path preserves percent-encoding; path is the fallback if unavailable.
        suffix = path.decode("ascii") if path is not None else quote(scope["path"], safe="/%")
        query = scope.get("query_string", b"").decode("ascii")
        return urlunsplit((scheme, base.netloc, base.path + suffix, query, ""))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        client = WebSocket(scope, receive, send)
        headers = filter_headers(
            [
                (key.decode("latin-1"), value.decode("latin-1"))
                for key, value in scope["headers"]
                if not key.lower().startswith(b"sec-websocket-")
            ],
            extra_drop=("host", "content-length", "accept-encoding", "proxy-connection"),
        )
        try:
            upstream_connection = connect(
                self._url(scope),
                additional_headers=headers,
                subprotocols=scope.get("subprotocols") or None,
                open_timeout=self.connect_timeout,
                close_timeout=2,
                max_size=None,
                compression=None,
                proxy=None,
                user_agent_header=None,
            )
            async with upstream_connection as upstream:
                await client.accept(subprotocol=upstream.subprotocol)
                try:
                    async with anyio.create_task_group() as group:

                        async def client_to_upstream() -> None:
                            try:
                                await self._client_to_upstream(client, upstream)
                            finally:
                                group.cancel_scope.cancel()

                        async def upstream_to_client() -> None:
                            try:
                                await self._upstream_to_client(upstream, client)
                            finally:
                                group.cancel_scope.cancel()

                        group.start_soon(client_to_upstream)
                        group.start_soon(upstream_to_client)
                finally:
                    # Keep the upstream close handshake alive on ASGI cancellation/shutdown.
                    with anyio.CancelScope(shield=True):
                        await upstream.close()
        except WebSocketDisconnect:
            # The client disappeared while the upstream handshake was in flight.
            return
        except (OSError, TimeoutError, WebSocketException):
            if client.application_state is WebSocketState.CONNECTING:
                try:
                    await client.accept()
                except (OSError, WebSocketDisconnect):
                    return
            if client.application_state is WebSocketState.CONNECTED:
                await self._close_client(client, 1011, "Gufo WebSocket unavailable")

    @staticmethod
    def _close_code(code: int) -> int:
        if code == 1005:  # No status on the wire is a normal, not an internal, close.
            return 1000
        return code if 1000 <= code <= 4999 and code not in (1004, 1006, 1015) else 1011

    @staticmethod
    async def _close_client(client: WebSocket, code: int, reason: str = "") -> None:
        try:
            with anyio.CancelScope(shield=True):
                await client.close(code=code, reason=reason)
        except (OSError, RuntimeError, WebSocketDisconnect):
            # The client may have already gone away while the other side closed.
            pass

    async def _client_to_upstream(self, client: WebSocket, upstream: ClientConnection) -> None:
        try:
            while True:
                message = await client.receive()
                if message["type"] == "websocket.disconnect":
                    with anyio.CancelScope(shield=True):
                        await upstream.close(
                            code=self._close_code(message["code"]),
                            reason=message.get("reason", ""),
                        )
                    return
                if message["type"] == "websocket.receive":
                    if message.get("text") is not None:
                        await upstream.send(message["text"])
                    else:
                        await upstream.send(message["bytes"])
        except WebSocketDisconnect:
            return
        except ConnectionClosed as exc:
            code = self._close_code(exc.rcvd.code) if exc.rcvd else 1011
            await self._close_client(client, code, exc.rcvd.reason if exc.rcvd else "")
        except OSError:
            await self._close_client(client, 1011)

    async def _upstream_to_client(self, upstream: ClientConnection, client: WebSocket) -> None:
        try:
            async for message in upstream:
                if isinstance(message, str):
                    await client.send_text(message)
                else:
                    await client.send_bytes(message)
        except ConnectionClosed as exc:
            code = self._close_code(exc.rcvd.code) if exc.rcvd else 1011
            await self._close_client(client, code, exc.rcvd.reason if exc.rcvd else "")
            return
        except (OSError, RuntimeError, WebSocketDisconnect):
            return
        # Normal iteration ends after a close handshake; retain its code and reason.
        await self._close_client(
            client,
            self._close_code(upstream.close_code) if upstream.close_code is not None else 1011,
            upstream.close_reason or "",
        )
