"""Application wiring: dashboard (/, /static, /api) + catch-all Gufo proxy."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .api import router
from .config import Settings
from .db import StatsWriter, connect, init_db
from .poller import Poller
from .proxy import KNOWN_ROUTES, InFlightTracker, Proxy

log = logging.getLogger("gufo_dashboard")
STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class AppContext:
    settings: Settings
    client: httpx.AsyncClient
    writer: StatsWriter
    inflight: InFlightTracker
    poller: Poller
    proxy: Proxy


def _is_dashboard_path(path: str) -> bool:
    return (
        path == "/"
        or path in ("/api", "/static")
        or path.startswith("/api/")
        or path.startswith("/static/")
    )


class Dispatcher:
    """Routes dashboard paths to FastAPI and everything else to the proxy.

    Also does the only request logging: method, route label, status,
    duration and Gufo request ID. Never bodies, headers, query strings or IPs.
    """

    def __init__(self, dashboard: FastAPI, allowed_hosts: tuple[str, ...] = ("localhost",)) -> None:
        self.dashboard = dashboard
        self.allowed_hosts = {host.lower().rstrip(".") for host in allowed_hosts}

    def valid_host(self, scope: Scope) -> bool:
        hosts = [value.decode("latin-1") for key, value in scope["headers"] if key == b"host"]
        if len(hosts) != 1 or any(c in hosts[0] for c in "/?#@ \t\r\n"):
            return False
        try:
            url = urlsplit("//" + hosts[0])
            host = (url.hostname or "").lower().rstrip(".")
            if url.port is not None and not 1 <= url.port <= 65535:
                return False
            if host in self.allowed_hosts:
                return True
            ip_address(host)
            return True
        except ValueError:
            return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.dashboard(scope, receive, send)
            return
        if not self.valid_host(scope):
            await JSONResponse({"detail": "invalid Host header"}, status_code=400)(
                scope, receive, send
            )
            return
        path: str = scope["path"]
        t0 = time.monotonic()
        status = {"code": 0}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        target: ASGIApp
        if _is_dashboard_path(path):
            target = self.dashboard
            route = (
                path
                if path in ("/", "/api/health")
                else ("/api/*" if path.startswith("/api") else "/static/*")
            )
        else:
            ctx: AppContext = self.dashboard.state.ctx
            target = ctx.proxy
            route = path if path in KNOWN_ROUTES else "(proxy)"
        try:
            await target(scope, receive, send_wrapper)
        except Exception as exc:
            log.error("%s %s failed: %s", scope.get("method"), route, type(exc).__name__)
            if status["code"] == 0:
                body = b'{"error":{"message":"internal error","type":"server_error","code":"internal_error"}}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
            raise
        finally:
            info = scope.get("state", {}).get("gufo_log", {})
            log.info(
                "%s %s %s %.1fms%s",
                scope.get("method"),
                route,
                status["code"],
                (time.monotonic() - t0) * 1000,
                f" rid={info['request_id']}" if info.get("request_id") else "",
            )


def create_app(settings: Settings | None = None) -> ASGIApp:
    settings = settings or Settings.from_env()
    # httpx logs full URLs (query strings may carry keys); httpcore logs headers.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_db(settings.database_path)
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.upstream_connect_timeout,
                read=settings.upstream_read_timeout,
                write=None,
                pool=None,
            ),
            limits=httpx.Limits(max_connections=None, max_keepalive_connections=20),
            follow_redirects=False,
            trust_env=False,
        )
        writer = StatsWriter(
            settings.database_path,
            settings.stats_queue_size,
            settings.retention_days,
            settings.content_retention_days,
        )
        inflight = InFlightTracker()
        poller = Poller(
            settings.gufo_base_url,
            client,
            writer,
            lambda: len(inflight),
            settings.poll_interval_seconds,
            settings.gufo_api_key,
        )
        conn = connect(settings.database_path)
        try:
            poller.restore_model(conn)
        finally:
            conn.close()
        writer.on_cleared.append(poller.reset_baseline)
        proxy = Proxy(
            settings.gufo_base_url,
            client,
            writer,
            inflight,
            lambda: poller.context_lengths,
            settings.max_inspect_body_bytes,
            capture_content=settings.capture_content,
            content_max_bytes=settings.content_max_bytes,
        )
        app.state.ctx = AppContext(settings, client, writer, inflight, poller, proxy)
        writer.start()
        if settings.enable_poller:
            poller.start()

        async def prune_loop() -> None:
            while True:
                with contextlib.suppress(Exception):
                    n = await writer.prune()
                    if n:
                        log.info("retention: pruned %d request rows", n)
                await asyncio.sleep(3600)

        pruner = asyncio.create_task(prune_loop(), name="retention")
        try:
            yield
        finally:
            pruner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pruner
            await poller.stop()
            await writer.stop(settings.shutdown_drain_timeout)
            await client.aclose()

    app = FastAPI(
        title="Gufo Dashboard",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def any_error(_: Request, exc: Exception) -> JSONResponse:
        log.error("dashboard error: %s", type(exc).__name__)
        return JSONResponse({"detail": "internal error"}, status_code=500)

    app.include_router(router)

    @app.get("/api/{rest:path}", include_in_schema=False)
    async def api_not_found(rest: str) -> Any:
        return JSONResponse({"detail": "not found"}, status_code=404)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return Dispatcher(app, settings.dashboard_allowed_hosts)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = Settings.from_env()
    uvicorn.run(
        create_app(settings),
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        access_log=False,
        server_header=False,
        date_header=False,
        proxy_headers=False,
        log_config=None,
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
