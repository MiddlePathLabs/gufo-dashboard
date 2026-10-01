"""Dashboard JSON API (`/api/*`). Same-origin only; no CORS."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import stats
from .db import connect, now_ms
from .poller import compute_unattributed
from .version import dashboard_version

if TYPE_CHECKING:
    from .main import AppContext

router = APIRouter(prefix="/api")

ClearBody = Annotated[Any, Body()]
RangeQ = Query("24h", pattern="^(1h|24h|7d|30d|all)$")
ModelQ = Query(None, max_length=128)


def _ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


@contextmanager
def _read(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(_ctx(request).settings.database_path, readonly=True)
    try:
        yield conn
    finally:
        conn.close()


def _model(model: str | None) -> str | None:
    return model or None


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/status")
def status(request: Request) -> dict[str, Any]:
    ctx = _ctx(request)
    poller = ctx.poller
    inflight = ctx.inflight.snapshot()
    now = now_ms()
    per_model: dict[str, int] = {}
    for f in inflight:
        per_model[f.model or ""] = per_model.get(f.model or "", 0) + 1
    recorded = None
    if poller.baseline is not None:
        with _read(request) as conn:
            recorded = stats.unattributed_window(
                conn, poller.baseline.ts, poller.baseline.window_end
            )
    writer = ctx.writer
    return {
        "version": dashboard_version(),
        **poller.status(),
        "in_flight": {
            "count": len(inflight),
            "per_model": per_model,
            "items": [
                {
                    "endpoint": f.endpoint,
                    "model": f.model,
                    "is_streaming": f.is_streaming,
                    "started_at_ms": f.started_at_ms,
                    "elapsed_ms": now - f.started_at_ms,
                }
                for f in sorted(inflight, key=lambda f: f.started_at_ms)
            ],
        },
        "unattributed": compute_unattributed(
            poller.baseline,
            recorded,
            len(inflight) + writer.queue.qsize(),
            ctx.settings.unattributed_threshold,
        ),
        "pipeline": {
            "stats_written": writer.counters.stats_written,
            "stats_dropped_queue_full": writer.counters.stats_dropped_queue_full,
            "stats_extraction_errors": writer.counters.stats_extraction_errors,
            "stats_write_errors": writer.counters.stats_write_errors,
            "queue_size": writer.queue.qsize(),
        },
        "content_capture": {
            "enabled": ctx.settings.capture_content,
            "retention_days": ctx.settings.content_retention_days,
            "max_bytes": ctx.settings.content_max_bytes,
        },
        "retention_days": ctx.settings.retention_days,
        "poll_interval_seconds": ctx.settings.poll_interval_seconds,
        "now_ms": now,
    }


@router.get("/models")
def models(request: Request) -> dict[str, Any]:
    with _read(request) as conn:
        seen = stats.models(conn)
    current = _ctx(request).poller.model
    if current and current not in seen:
        seen.append(current)
    return {"models": sorted(seen), "current": current}


@router.get("/summary")
def summary(request: Request, range: str = RangeQ, model: str | None = ModelQ) -> dict[str, Any]:
    with _read(request) as conn:
        return stats.summary(conn, range, _model(model), now_ms())


@router.get("/timeseries")
def timeseries(
    request: Request,
    range: str = RangeQ,
    bucket: str = Query("auto", pattern=r"^(auto|[0-9]{4,12})$"),
    model: str | None = ModelQ,
) -> dict[str, Any]:
    if bucket != "auto" and int(bucket) < 1000:
        raise HTTPException(422, "bucket must be at least 1000 milliseconds")
    with _read(request) as conn:
        return stats.timeseries(conn, range, _model(model), now_ms(), bucket)


@router.get("/activity")
def activity(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    before: str | None = Query(None, pattern=r"^\d+:(request|event):\d+$"),
    after: str | None = Query(None, pattern=r"^\d+:(request|event):\d+$"),
    model: str | None = ModelQ,
    filter: str = Query("all", pattern="^(all|errors|vision|streaming|cancelled|content|partial)$"),
) -> dict[str, Any]:
    if before and after:
        raise HTTPException(400, "use either before or after")
    with _read(request) as conn:
        return stats.activity(
            conn,
            limit=limit,
            before=before,
            after=after,
            model=_model(model),
            filter_=filter,
            content_cutoff_ms=now_ms() - _ctx(request).settings.content_retention_days * 86_400_000,
        )


@router.get("/requests/{ident}")
def request_detail(request: Request, ident: int) -> dict[str, Any]:
    with _read(request) as conn:
        row = stats.request_detail(conn, ident)
    if row is None:
        raise HTTPException(404, "not found")
    return row


@router.get("/requests/{ident}/content")
def request_content(request: Request, ident: int) -> JSONResponse:
    cutoff = now_ms() - _ctx(request).settings.content_retention_days * 86_400_000
    with _read(request) as conn:
        if conn.execute("SELECT 1 FROM request_stats WHERE id=?", (ident,)).fetchone() is None:
            return JSONResponse(
                {"detail": "not found"}, status_code=404, headers={"Cache-Control": "no-store"}
            )
        row = conn.execute("SELECT * FROM request_content WHERE request_id=?", (ident,)).fetchone()
    data = dict(row) if row else {"status": "unavailable", "question": None, "answer": None}
    if (
        row
        and row["captured_at_ms"] < cutoff
        and row["status"] not in ("disabled", "deleted", "expired")
    ):
        data.update(status="expired", question=None, answer=None)
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@router.delete("/requests/{ident}/content")
async def delete_request_content(request: Request, ident: int) -> JSONResponse:
    with _read(request) as conn:
        if conn.execute("SELECT 1 FROM request_stats WHERE id=?", (ident,)).fetchone() is None:
            return JSONResponse(
                {"detail": "not found"}, status_code=404, headers={"Cache-Control": "no-store"}
            )
    await _ctx(request).writer.delete_content(ident)
    return JSONResponse({"status": "deleted"}, headers={"Cache-Control": "no-store"})


@router.post("/content/clear")
async def clear_content(request: Request, payload: ClearBody) -> JSONResponse:
    if not isinstance(payload, dict) or payload.get("confirm") != "clear":
        raise HTTPException(400, 'body must be {"confirm": "clear"}')
    await _ctx(request).writer.delete_content()
    return JSONResponse({"status": "cleared"}, headers={"Cache-Control": "no-store"})


@router.get("/speculative")
def speculative(
    request: Request, range: str = RangeQ, model: str | None = ModelQ
) -> dict[str, Any]:
    with _read(request) as conn:
        return stats.speculative(conn, range, _model(model), now_ms())


@router.get("/cache")
def cache(request: Request, range: str = RangeQ, model: str | None = ModelQ) -> dict[str, Any]:
    with _read(request) as conn:
        return stats.cache(conn, range, _model(model), now_ms())


@router.get("/events")
def events(request: Request, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    with _read(request) as conn:
        return {"events": stats.events(conn, limit)}


@router.post("/stats/clear")
async def clear(request: Request, payload: ClearBody) -> dict[str, str]:
    if not isinstance(payload, dict) or payload.get("confirm") != "clear":
        raise HTTPException(400, 'body must be {"confirm": "clear"}')
    ctx = _ctx(request)
    await ctx.writer.clear()
    ctx.poller.reset_baseline()
    return {"status": "cleared"}
