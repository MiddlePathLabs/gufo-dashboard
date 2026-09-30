"""Polls Gufo's /ready, /v1/models and /metrics.

Tracks online state, the current model and context length, the "observed
ready duration" (Gufo exposes no uptime), counter resets, and the
reset-aware /metrics counter deltas used for the unattributed-token estimate.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from . import prom
from .db import StatsWriter, now_ms
from .extract import as_int, as_str, loads_strict

log = logging.getLogger("gufo_dashboard.poller")


@dataclass
class Baseline:
    ts: int
    window_end: int
    prompt: float = 0.0
    predicted: float = 0.0


class Poller:
    def __init__(
        self,
        base_url: str,
        client: httpx.AsyncClient,
        writer: StatsWriter,
        inflight_count: Callable[[], int],
        interval: float,
        api_key: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client
        self.writer = writer
        self.inflight_count = inflight_count
        self.interval = interval
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

        self.online: bool | None = None
        self.model: str | None = None
        self.context_lengths: dict[str, int] = {}
        self.ready_since_ms: int | None = None
        self.last_poll_ms: int | None = None
        self.last_error: str | None = None
        self.counters: dict[str, float] | None = None  # last seen totals
        self.gauges: dict[str, float | None] = {"prompt": None, "predicted": None}
        self.baseline: Baseline | None = None
        self._last_snapshot: tuple[Any, ...] | None = None
        self._task: asyncio.Task[None] | None = None

    # -- lifecycle ---------------------------------------------------------- #

    def restore_model(self, conn: sqlite3.Connection) -> None:
        """Remember the last known model so a change across restarts is noticed."""
        row = conn.execute(
            "SELECT model FROM (SELECT model, ts FROM events WHERE model IS NOT NULL "
            "UNION ALL SELECT model, completed_at_ms FROM request_stats WHERE model IS NOT NULL) "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if row:
            self.model = row[0]

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="gufo-poller")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception as exc:  # keep polling no matter what
                log.warning("poll failed: %s", type(exc).__name__)
            await asyncio.sleep(self.interval)

    def reset_baseline(self) -> None:
        self.baseline = None

    # -- polling ------------------------------------------------------------ #

    async def _get(self, path: str) -> httpx.Response | None:
        try:
            return await self.client.get(self.base_url + path, headers=self.headers, timeout=5.0)
        except httpx.HTTPError as exc:
            self.last_error = type(exc).__name__
            return None

    async def poll_once(self) -> None:
        now = now_ms()
        self.last_poll_ms = now
        ready_model: str | None = None
        online = False
        r = await self._get("/ready")
        if r is not None:
            try:
                body = loads_strict(r.content)
            except ValueError:
                body = None
            if r.status_code == 200 and isinstance(body, dict) and body.get("status") == "ready":
                online = True
                ready_model = as_str(body.get("model"), 128)
                self.last_error = None
            else:
                self.last_error = f"ready_http_{r.status_code}"
        self._set_online(online, ready_model, now)
        if not online:
            return

        r = await self._get("/v1/models")
        if r is not None and r.status_code == 200:
            try:
                data = loads_strict(r.content).get("data")
            except (ValueError, AttributeError):
                data = None
            if isinstance(data, list):
                for m in data:
                    if not isinstance(m, dict):
                        continue
                    name = as_str(m.get("id"), 128)
                    ctx = as_int(m.get("context_length"))
                    if name and ctx:
                        self.context_lengths[name] = ctx
                    if name and ready_model is None:
                        ready_model = name
                        self._set_model(name)

        r = await self._get("/metrics")
        if r is not None and r.status_code == 200:
            self._on_metrics(prom.parse_prometheus(r.text), now)

    def _set_online(self, online: bool, model: str | None, now: int) -> None:
        if online and self.online is not True:
            self.writer.submit_event("gufo_up", model, None)
            self.ready_since_ms = now
        elif not online and self.online is not False:
            self.writer.submit_event(
                "gufo_down", self.model, self.last_error and self.last_error[:64]
            )
            self.ready_since_ms = None
            self.gauges = {"prompt": None, "predicted": None}
        self.online = online
        if online and model:
            self._set_model(model)

    def _set_model(self, model: str) -> None:
        if self.model is not None and model != self.model:
            self.writer.submit_event("model_changed", model, as_str(self.model, 128))
        self.model = model

    def _on_metrics(self, samples: dict[str, float], now: int) -> None:
        cur_p = samples.get(prom.PROMPT_TOTAL)
        cur_d = samples.get(prom.PREDICTED_TOTAL)
        self.gauges = {
            "prompt": samples.get(prom.PROMPT_RATE),
            "predicted": samples.get(prom.PREDICTED_RATE),
        }
        if cur_p is None or cur_d is None:
            return
        prev = self.counters
        delta_p = delta_d = 0.0
        if prev is not None:
            reset = cur_p < prev["prompt"] or cur_d < prev["predicted"]
            if reset:
                self.writer.submit_event("counter_reset", self.model, None)
                self.ready_since_ms = now
                delta_p, delta_d = cur_p, cur_d  # counters restarted from zero
            else:
                delta_p, delta_d = cur_p - prev["prompt"], cur_d - prev["predicted"]
        self.counters = {"prompt": cur_p, "predicted": cur_d}

        if self.baseline is None:
            if self.inflight_count() == 0:
                self.baseline = Baseline(ts=now, window_end=now)
        else:
            self.baseline.prompt += delta_p
            self.baseline.predicted += delta_d
            self.baseline.window_end = now

        snap = (self.model, cur_p, cur_d, self.gauges["prompt"], self.gauges["predicted"])
        if snap != self._last_snapshot:
            self._last_snapshot = snap
            self.writer.submit_snapshot((now, *snap))

    # -- reporting ---------------------------------------------------------- #

    def status(self) -> dict[str, Any]:
        now = now_ms()
        return {
            "online": bool(self.online),
            "model": self.model,
            "context_length": self.context_lengths.get(self.model or ""),
            "ready_since_ms": self.ready_since_ms,
            "observed_ready_ms": (now - self.ready_since_ms) if self.ready_since_ms else None,
            "last_poll_ms": self.last_poll_ms,
            "last_error": self.last_error,
            "counters": self.counters,
            "gauges": {
                "last_request_prefill_tps": self.gauges["prompt"],
                "last_request_decode_tps": self.gauges["predicted"],
            },
        }


def compute_unattributed(
    baseline: Baseline | None,
    recorded: dict[str, int] | None,
    inflight: int,
    threshold: int,
) -> dict[str, Any]:
    """Counter deltas since the baseline minus tokens of recorded rows."""
    if baseline is None or recorded is None:
        return {"available": False, "visible": False}
    prompt = int(baseline.prompt) - recorded["prompt_tokens"]
    completion = int(baseline.predicted) - recorded["completion_tokens"]
    visible = inflight == 0 and max(prompt, completion) > threshold
    return {
        "available": True,
        "visible": visible,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "counter_prompt_delta": int(baseline.prompt),
        "counter_completion_delta": int(baseline.predicted),
        "recorded_prompt_tokens": recorded["prompt_tokens"],
        "recorded_completion_tokens": recorded["completion_tokens"],
        "recorded_rows": recorded["rows"],
        "cancelled_in_window": recorded["cancelled"],
        "baseline_ms": baseline.ts,
        "window_end_ms": baseline.window_end,
    }
