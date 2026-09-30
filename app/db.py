"""SQLite schema, the single writer task, and the daily rollup."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .extract import RECORD_COLUMNS

log = logging.getLogger("gufo_dashboard.db")

SCHEMA_VERSION = 2

_INT_COLS = {
    "started_at_ms",
    "completed_at_ms",
    "is_streaming",
    "is_vision",
    "image_count",
    "http_status",
    "prompt_tokens",
    "cached_tokens",
    "prefill_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "prefill_chunks",
    "cache_hit",
    "cache_common_prefix_tokens",
    "cache_restore_bytes",
    "draft_tokens",
    "draft_tokens_accepted",
    "queue_depth_at_submit",
    "client_queue_depth_at_submit",
    "resident_requests_at_admission",
    "requested_logical_concurrency",
    "physical_execution_width",
    "context_length",
}
_TEXT_COLS = {
    "endpoint",
    "model",
    "gufo_request_id",
    "error_code",
    "finish_reason",
    "ttft_source",
    "cache_miss_reason",
    "execution_plan",
    "extra_metrics",
}


def _col_type(name: str) -> str:
    if name in _INT_COLS:
        return "INTEGER"
    if name in _TEXT_COLS:
        return "TEXT"
    return "REAL"


# Sums kept per (day, model) so all-time figures survive retention pruning.
# The same names are produced by stats.AGG_SQL over raw rows.
ROLLUP_FIELDS = (
    "requests",
    "errors",
    "cancelled",
    "streaming",
    "vision",
    "prompt_tokens",
    "cached_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "share_cached",
    "share_prompt",
    "prefill_tokens_w",
    "prefill_ms_w",
    "decode_tokens_w",
    "decode_ms_w",
    "draft_tokens",
    "draft_accepted",
    "draft_requests",
    "cache_hits",
    "cache_known",
    "restore_ms_sum",
    "restore_n",
    "ttft_sum",
    "ttft_n",
    "ttft_proxy_n",
    "latency_sum",
    "latency_n",
)


def _schema() -> str:
    cols = ",\n  ".join(f"{c} {_col_type(c)}" for c in RECORD_COLUMNS)
    rollup_cols = ",\n  ".join(f"{c} REAL NOT NULL DEFAULT 0" for c in ROLLUP_FIELDS)
    return f"""
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS request_stats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  {cols}
);
CREATE TABLE IF NOT EXISTS request_content (
  request_id INTEGER PRIMARY KEY,
  captured_at_ms INTEGER NOT NULL,
  status TEXT NOT NULL,
  question TEXT,
  answer TEXT,
  question_truncated INTEGER NOT NULL DEFAULT 0,
  answer_truncated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_content_captured ON request_content (captured_at_ms);
CREATE INDEX IF NOT EXISTS ix_rs_completed ON request_stats (completed_at_ms);
CREATE INDEX IF NOT EXISTS ix_rs_model_completed ON request_stats (model, completed_at_ms);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  kind TEXT NOT NULL,
  model TEXT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS metrics_snapshots (
  ts INTEGER NOT NULL,
  model TEXT,
  prompt_tokens_total REAL,
  tokens_predicted_total REAL,
  prompt_tokens_seconds REAL,
  predicted_tokens_seconds REAL
);
CREATE INDEX IF NOT EXISTS ix_ms_ts ON metrics_snapshots (ts);
CREATE TABLE IF NOT EXISTS daily_rollup (
  day TEXT NOT NULL,
  model TEXT NOT NULL,
  {rollup_cols},
  latency_max REAL,
  first_ms INTEGER,
  last_ms INTEGER,
  PRIMARY KEY (day, model)
);
"""


def connect(path: str, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(_schema())
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.execute(
            "UPDATE schema_version SET version = ? WHERE version < ?",
            (SCHEMA_VERSION, SCHEMA_VERSION),
        )
        conn.commit()
    finally:
        conn.close()


def utc_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def _pos(v: Any) -> bool:
    return v is not None and v > 0


def rollup_contribution(rec: dict[str, Any]) -> dict[str, Any]:
    """One request row's contribution to daily_rollup (mirrors stats.AGG_SQL)."""
    g = rec.get
    status = g("http_status") or 0
    c: dict[str, Any] = dict.fromkeys(ROLLUP_FIELDS, 0.0)
    c["requests"] = 1
    c["errors"] = 1 if status >= 400 or g("error_code") == "upstream_unreachable" else 0
    c["cancelled"] = 1 if g("finish_reason") == "client_cancelled" else 0
    c["streaming"] = 1 if g("is_streaming") else 0
    c["vision"] = 1 if g("is_vision") else 0
    for k in ("prompt_tokens", "cached_tokens", "completion_tokens", "reasoning_tokens"):
        c[k] = g(k) or 0
    if g("cached_tokens") is not None and g("prompt_tokens") is not None:
        c["share_cached"] = g("cached_tokens")
        c["share_prompt"] = g("prompt_tokens")
    if _pos(g("prefill_tokens")) and _pos(g("prefill_ms")):
        c["prefill_tokens_w"] = g("prefill_tokens")
        c["prefill_ms_w"] = g("prefill_ms")
    if _pos(g("completion_tokens")) and _pos(g("decode_ms")):
        c["decode_tokens_w"] = g("completion_tokens")
        c["decode_ms_w"] = g("decode_ms")
    if g("draft_tokens") is not None and g("draft_tokens_accepted") is not None:
        c["draft_tokens"] = g("draft_tokens")
        c["draft_accepted"] = g("draft_tokens_accepted")
        c["draft_requests"] = 1
    if g("cache_hit") is not None:
        c["cache_known"] = 1
        if g("cache_hit"):
            c["cache_hits"] = 1
            if g("cache_restore_ms") is not None:
                c["restore_ms_sum"] = g("cache_restore_ms")
                c["restore_n"] = 1
    if g("ttft_ms") is not None:
        c["ttft_sum"] = g("ttft_ms")
        c["ttft_n"] = 1
        c["ttft_proxy_n"] = 1 if g("ttft_source") == "proxy_stream" else 0
    if g("total_request_ms") is not None:
        c["latency_sum"] = g("total_request_ms")
        c["latency_n"] = 1
    return c


_ROLLUP_UPSERT = (
    "INSERT INTO daily_rollup (day, model, "
    + ", ".join(ROLLUP_FIELDS)
    + ", latency_max, first_ms, last_ms) VALUES ("
    + ", ".join("?" for _ in range(len(ROLLUP_FIELDS) + 5))
    + ") ON CONFLICT(day, model) DO UPDATE SET "
    + ", ".join(f"{f} = {f} + excluded.{f}" for f in ROLLUP_FIELDS)
    + ", latency_max = max(coalesce(latency_max, excluded.latency_max), coalesce(excluded.latency_max, latency_max))"
    + ", first_ms = min(first_ms, excluded.first_ms), last_ms = max(last_ms, excluded.last_ms)"
)

_INSERT_REQUEST = (
    "INSERT INTO request_stats ("
    + ", ".join(RECORD_COLUMNS)
    + ") VALUES ("
    + ", ".join("?" for _ in RECORD_COLUMNS)
    + ")"
)


def _db_value(col: str, v: Any) -> Any:
    if v is None:
        return None
    if col == "extra_metrics":
        return json.dumps(v, separators=(",", ":")) if v else None
    if isinstance(v, bool):
        return int(v)
    return v


def insert_request(conn: sqlite3.Connection, rec: dict[str, Any]) -> int:
    cur = conn.execute(_INSERT_REQUEST, [_db_value(c, rec.get(c)) for c in RECORD_COLUMNS])
    contrib = rollup_contribution(rec)
    ms = int(rec["completed_at_ms"])
    conn.execute(
        _ROLLUP_UPSERT,
        [
            utc_day(ms),
            rec.get("model") or "",
            *(contrib[f] for f in ROLLUP_FIELDS),
            rec.get("total_request_ms"),
            ms,
            ms,
        ],
    )
    ident = int(cur.lastrowid or 0)
    content = rec.get("_content")
    if content is not None:
        conn.execute(
            "INSERT INTO request_content VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ident,
                ms,
                content["status"],
                content["question"],
                content["answer"],
                int(content["question_truncated"]),
                int(content["answer_truncated"]),
            ),
        )
    return ident


def insert_event(
    conn: sqlite3.Connection, ts: int, kind: str, model: str | None, detail: str | None
) -> None:
    conn.execute(
        "INSERT INTO events (ts, kind, model, detail) VALUES (?, ?, ?, ?)",
        (ts, kind, model, detail),
    )


def prune(conn: sqlite3.Connection, retention_days: int, now_ms: int) -> int:
    cutoff = now_ms - retention_days * 86_400_000
    n = conn.execute("DELETE FROM request_stats WHERE completed_at_ms < ?", (cutoff,)).rowcount
    conn.execute("DELETE FROM metrics_snapshots WHERE ts < ?", (cutoff,))
    conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
    conn.execute(
        "DELETE FROM request_content WHERE request_id NOT IN (SELECT id FROM request_stats)"
    )
    return int(n)


def clear_all(conn: sqlite3.Connection) -> None:
    for table in (
        "request_content",
        "request_stats",
        "events",
        "metrics_snapshots",
        "daily_rollup",
    ):
        conn.execute(f"DELETE FROM {table}")


def now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# Single writer
# --------------------------------------------------------------------------- #


@dataclass
class PipelineCounters:
    stats_written: int = 0
    stats_dropped_queue_full: int = 0
    stats_extraction_errors: int = 0
    stats_write_errors: int = 0


@dataclass
class _Job:
    kind: str  # request | event | snapshot | prune | clear | barrier
    payload: Any = None
    done: asyncio.Future[Any] | None = field(default=None)


class StatsWriter:
    """Owns the only write connection; everything is written from one task."""

    def __init__(
        self, path: str, queue_size: int, retention_days: int, content_retention_days: int = 7
    ) -> None:
        self.path = path
        self.retention_days = retention_days
        self.content_retention_days = content_retention_days
        self.content_queue_bytes = 0
        self.content_generation = 0
        self.queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=queue_size)
        self.counters = PipelineCounters()
        self.accepting = True
        self._conn: sqlite3.Connection | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = threading.Lock()
        self.on_cleared: list[Callable[[], None]] = []

    # -- producers (event loop thread) ------------------------------------- #

    def _put(self, job: _Job) -> bool:
        if not self.accepting:
            self.counters.stats_dropped_queue_full += job.kind == "request"
            return False
        try:
            self.queue.put_nowait(job)
            return True
        except asyncio.QueueFull:
            if job.kind == "request":
                self.counters.stats_dropped_queue_full += 1
            else:
                log.warning("writer queue full; dropped a %s job", job.kind)
            return False

    def submit(self, record: dict[str, Any]) -> None:
        content = record.get("_content")
        size = (
            sum(len((content.get(k) or "").encode("utf-8")) for k in ("question", "answer"))
            if content
            else 0
        )
        if content is not None and self.content_queue_bytes + size > 8 * 1024 * 1024:
            record["_content"] = {**content, "question": None, "answer": None, "status": "dropped"}
            size = 0
        record["_content_size"] = size
        if self._put(_Job("request", record)):
            self.content_queue_bytes += size

    def submit_event(self, kind: str, model: str | None, detail: str | None) -> None:
        self._put(_Job("event", (now_ms(), kind, model, detail)))

    def submit_snapshot(self, row: tuple[Any, ...]) -> None:
        self._put(_Job("snapshot", row))

    async def _call(self, kind: str, payload: Any = None) -> Any:
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        if not self._put(_Job(kind, payload, fut)):
            raise RuntimeError("writer unavailable")
        return await fut

    async def flush(self) -> None:
        await self._call("barrier")

    async def clear(self) -> None:
        self.content_generation += 1
        await self._call("clear")

    async def delete_content(self, ident: int | None = None) -> None:
        # Prevent an in-flight request from restoring content after a bulk clear.
        if ident is None:
            self.content_generation += 1
        await self._call("delete_content", ident)

    async def prune(self) -> int:
        return int(await self._call("prune"))

    # -- consumer ---------------------------------------------------------- #

    def start(self) -> None:
        self._conn = connect(self.path)
        self._task = asyncio.create_task(self._run(), name="stats-writer")

    async def _run(self) -> None:
        while True:
            batch = [await self.queue.get()]
            while len(batch) < 256:
                try:
                    batch.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            results = await asyncio.to_thread(self._write_batch, batch)
            for job, result in zip(batch, results, strict=True):
                if job.done is not None and not job.done.done():
                    if isinstance(result, BaseException):
                        job.done.set_exception(result)
                    else:
                        job.done.set_result(result)
                if job.kind == "request":
                    self.content_queue_bytes -= job.payload.get("_content_size", 0)
                self.queue.task_done()
            if any(j.kind == "clear" for j in batch):
                for cb in self.on_cleared:
                    cb()

    def _write_batch(self, batch: list[_Job]) -> list[Any]:
        with self._lock:
            return self._write_batch_locked(batch)

    def _write_batch_locked(self, batch: list[_Job]) -> list[Any]:
        conn = self._conn
        if conn is None:
            return [RuntimeError("closed") for _ in batch]
        results: list[Any] = []
        for job in batch:
            try:
                results.append(self._apply(conn, job))
            except Exception as exc:  # never include payload in logs
                if job.kind == "request":
                    self.counters.stats_write_errors += 1
                log.error("stats write failed (%s): %s", job.kind, type(exc).__name__)
                results.append(RuntimeError(type(exc).__name__))
        try:
            conn.commit()
        except sqlite3.Error as exc:
            log.error("commit failed: %s", type(exc).__name__)
        return results

    def _apply(self, conn: sqlite3.Connection, job: _Job) -> Any:
        if job.kind == "request":
            conn.execute("SAVEPOINT row")  # the row and its rollup go in together
            try:
                insert_request(conn, job.payload)
            except BaseException:
                conn.execute("ROLLBACK TO row")
                conn.execute("RELEASE row")
                raise
            conn.execute("RELEASE row")
            self.counters.stats_written += 1
            return None
        if job.kind == "event":
            insert_event(conn, *job.payload)
            return None
        if job.kind == "snapshot":
            conn.execute("INSERT INTO metrics_snapshots VALUES (?, ?, ?, ?, ?, ?)", job.payload)
            return None
        if job.kind == "delete_content":
            where = "" if job.payload is None else " WHERE request_id = ?"
            params = () if job.payload is None else (job.payload,)
            conn.execute(
                "UPDATE request_content SET question=NULL, answer=NULL, status='deleted'" + where,
                params,
            )
            return None
        if job.kind == "prune":
            conn.execute(
                "UPDATE request_content SET question=NULL, answer=NULL, status='expired' "
                "WHERE captured_at_ms < ? AND status NOT IN ('disabled', 'deleted', 'expired')",
                (now_ms() - self.content_retention_days * 86_400_000,),
            )
            return prune(conn, self.retention_days, now_ms())
        if job.kind == "clear":
            clear_all(conn)
            insert_event(conn, now_ms(), "stats_cleared", None, None)
            return None
        return None  # barrier

    async def stop(self, timeout: float) -> None:
        """Stop accepting, drain with a timeout, commit and close."""
        self.accepting = False
        if self._task is None:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.queue.join(), timeout)
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        left = 0
        while True:
            try:
                job = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            left += job.kind == "request"
        if left:
            self.counters.stats_dropped_queue_full += left
            log.warning("shutdown: %d queued stats records dropped", left)
        await asyncio.to_thread(self._close)

    def _close(self) -> None:
        with self._lock:  # waits for a batch still running in its thread
            if self._conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.commit()
                self._conn.close()
                self._conn = None
