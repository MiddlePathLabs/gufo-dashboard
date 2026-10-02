"""Aggregation, persistence, retention/rollup, activity cursors, writer pipeline."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from app import db, extract, stats
from app.db import StatsWriter

from .conftest import MODEL, Dash, fixture_json

OTHER = "fixture-model-b"
NOW = 1_790_900_000_000


def rec_from(
    fixture: str,
    *,
    kind: str = "chat",
    completed: int = NOW - 1000,
    model: str | None = None,
    **kw: Any,
) -> dict[str, Any]:
    fields = extract.BODY_EXTRACTORS[kind](fixture_json(fixture))
    if model:
        fields["model"] = model
    r = extract.finalize_record(
        fields,
        endpoint="/v1/chat/completions",
        request_model=None,
        is_streaming=kw.pop("is_streaming", False),
        image_count=0,
        http_status=kw.pop("http_status", 200),
        gufo_request_id="r1",
        started_at_ms=completed - 500,
        completed_at_ms=completed,
        proxy_ttfb_ms=10.0,
        proxy_first_token_ms=None,
        total_request_ms=kw.pop("total_request_ms", 500.0),
        context_lengths={},
        cancelled=False,
    )
    r.update(kw)
    return r


@pytest.fixture
def conn(tmp_path: Path) -> Any:
    path = str(tmp_path / "t.sqlite")
    db.init_db(path)
    c = db.connect(path)
    yield c
    c.close()


def insert(conn: sqlite3.Connection, *recs: dict[str, Any]) -> None:
    for r in recs:
        db.insert_request(conn, r)
    conn.commit()


# --------------------------------------------------------------------------- #
# Stats rules
# --------------------------------------------------------------------------- #


def test_weighted_decode_from_fixture(conn: sqlite3.Connection) -> None:
    insert(conn, rec_from("chat_nonstream"))
    s = stats.summary(conn, "24h", MODEL, NOW)
    # PLAN.md quotes 16 / 411.4 ms ≈ 38.89 tok/s, but the captured fixture's
    # decode_ms is 473.252382 (fixtures supersede the plan): 16 / 0.473252 s.
    assert s["decode_tps_weighted"] == pytest.approx(1000 * 16 / 473.252382)
    assert s["decode_tps_weighted"] == pytest.approx(33.8086, abs=1e-3)
    assert s["prefill_tps_weighted"] is None  # full cache hit, no prefill work
    assert s["cache_hit_rate"] == 1.0


def test_weighted_rates_percentiles_acceptance(conn: sqlite3.Connection) -> None:
    recs = [
        rec_from("chat_nonstream"),  # 16 tok / 473.25 ms, draft 15/8, hit
        rec_from("chat_nonstream_cachehit"),  # 16 / 384.67, draft 14/10, hit
        rec_from("chat_vision_nonstream"),  # 16 / 417.85, prefill 111 / 391.99, draft 16/11, miss
    ]
    insert(conn, *recs)
    s = stats.summary(conn, "24h", MODEL, NOW)
    assert s["decode_tps_weighted"] == pytest.approx(
        1000 * 48 / (473.252382 + 384.668241 + 417.854128)
    )
    assert s["prefill_tps_weighted"] == pytest.approx(1000 * 111 / 391.993328)
    per_req = sorted(r["completion_tokens_per_second"] for r in recs)
    assert s["decode_tps_p50"] == pytest.approx(per_req[1])
    assert s["decode_tps_p95"] == pytest.approx(per_req[1] + 0.9 * (per_req[2] - per_req[1]))
    assert s["draft_acceptance"] == pytest.approx((8 + 10 + 11) / (15 + 14 + 16))  # sum/sum
    assert s["cache_hit_rate"] == pytest.approx(2 / 3)
    assert s["cached_token_share"] == pytest.approx(84 / (42 + 42 + 111))
    assert s["ttft_n"] == 3 and s["ttft_proxy_stream_n"] == 0
    assert s["ttft_p50_ms"] == pytest.approx(8.618079)
    assert s["saved_prefill_s_estimate"] == pytest.approx(84 / s["prefill_tps_weighted"])
    assert s["requests"] == 3 and s["generated_tokens"] == 48 and s["prompt_tokens"] == 195


def test_models_never_mixed(conn: sqlite3.Connection) -> None:
    a = rec_from("chat_nonstream")  # ~33.8 tok/s
    b = rec_from("chat_nonstream", model=OTHER, decode_ms=100.0, completion_tokens_per_second=160.0)
    insert(conn, a, b)
    all_ = stats.summary(conn, "24h", None, NOW)
    assert "decode_tps_weighted" not in all_ and "draft_acceptance" not in all_
    assert all_["requests"] == 2 and all_["generated_tokens"] == 32
    by = {p["model"]: p for p in all_["per_model"]}
    assert by[MODEL]["decode_tps_weighted"] == pytest.approx(33.8086, abs=1e-3)
    assert by[OTHER]["decode_tps_weighted"] == pytest.approx(160.0)
    one = stats.summary(conn, "24h", OTHER, NOW)
    assert one["decode_tps_weighted"] == pytest.approx(160.0) and one["requests"] == 1
    ts = stats.timeseries(conn, "1h", None, NOW)
    assert set(ts["per_model"]) == {MODEL, OTHER}
    assert sum(ts["requests"]) == 2
    spec = stats.speculative(conn, "24h", None, NOW)
    assert len(spec["per_model"]) == 2


def test_error_rate_and_latency(conn: sqlite3.Connection) -> None:
    ok = rec_from("chat_nonstream", total_request_ms=100.0)
    err = rec_from(
        "chat_nonstream", http_status=404, error_code="model_not_found", total_request_ms=300.0
    )
    down = rec_from(
        "chat_nonstream", http_status=502, error_code="upstream_unreachable", total_request_ms=50.0
    )
    insert(conn, ok, err, down)
    s = stats.summary(conn, "24h", MODEL, NOW)
    assert s["errors"] == 2 and s["error_rate"] == pytest.approx(2 / 3)
    assert s["latency_avg_ms"] == pytest.approx(150.0) and s["latency_max_ms"] == 300.0


def test_missing_data_stays_null(conn: sqlite3.Connection) -> None:
    insert(conn, rec_from("messages_nonstream", kind="messages"))
    s = stats.summary(conn, "24h", MODEL, NOW)
    assert s["ttft_avg_ms"] is None and s["ttft_p50_ms"] is None and s["ttft_n"] == 0
    assert s["cache_hit_rate"] is None and s["cache_known_n"] == 0
    assert s["draft_acceptance"] == pytest.approx(0.7)


def test_cache_endpoint_miss_reasons(conn: sqlite3.Connection) -> None:
    insert(conn, rec_from("chat_vision_nonstream"), rec_from("chat_nonstream"))
    c = stats.cache(conn, "24h", MODEL, NOW)["per_model"][0]
    assert c["hits"] == 1 and c["misses"] == 1
    assert c["miss_reasons"] == {"input_changed": 1}
    assert c["restore_avg_ms"] == pytest.approx(7.958364)


# --------------------------------------------------------------------------- #
# Retention and rollup
# --------------------------------------------------------------------------- #


def test_rollup_survives_pruning(conn: sqlite3.Connection) -> None:
    old = NOW - 100 * 86_400_000
    insert(
        conn,
        rec_from("chat_vision_nonstream", completed=old),
        rec_from("chat_nonstream", completed=old + 1),
        rec_from("chat_nonstream_cachehit", completed=NOW - 5000),
    )
    before = stats.summary(conn, "all", MODEL, NOW)
    assert db.prune(conn, 90, NOW) == 2
    conn.commit()
    after = stats.summary(conn, "all", MODEL, NOW)
    for key in (
        "requests",
        "generated_tokens",
        "prompt_tokens",
        "decode_tps_weighted",
        "prefill_tps_weighted",
        "draft_acceptance",
        "cache_hit_rate",
    ):
        assert after[key] == pytest.approx(before[key]), key
    assert after["requests"] == 3
    assert after["percentile_window_start_ms"] == NOW - 5000
    assert after["decode_n"] == 1  # percentiles only cover retained rows
    assert stats.summary(conn, "30d", MODEL, NOW)["requests"] == 1
    ts = stats.timeseries(conn, "all", MODEL, NOW)
    assert ts["source"] == "rollup" and ts["bucket_ms"] == 86_400_000
    assert sum(ts["requests"]) == 3


def test_timeseries_auto_buckets(conn: sqlite3.Connection) -> None:
    insert(conn, rec_from("chat_nonstream"))
    for rng in ("1h", "24h", "7d", "30d"):
        ts = stats.timeseries(conn, rng, MODEL, NOW)
        assert 30 <= len(ts["t"]) <= 125, (rng, len(ts["t"]))
        assert sum(ts["requests"]) == 1


# --------------------------------------------------------------------------- #
# Activity cursors
# --------------------------------------------------------------------------- #


def test_activity_cursors_with_shared_timestamps(conn: sqlite3.Connection) -> None:
    ts_values = [NOW - 3000] * 4 + [NOW - 2000] * 3 + [NOW - 1000] * 3
    for t in ts_values:
        insert(conn, rec_from("chat_nonstream", completed=t))
    for t in (NOW - 3000, NOW - 2000, NOW - 2000, NOW):
        db.insert_event(conn, t, "gufo_up", None, None)
    conn.commit()
    total = len(ts_values) + 4

    full = stats.activity(conn, limit=500)["items"]
    assert len(full) == total
    keys = [stats.parse_cursor(i["cursor"]) for i in full]
    assert keys == sorted(keys, reverse=True)
    assert {i["type"] for i in full} == {"request", "event"}

    # page backwards
    seen: list[str] = []
    cursor = None
    while True:
        page = stats.activity(conn, limit=3, before=cursor)
        seen += [i["cursor"] for i in page["items"]]
        if not page["items"]:
            break
        cursor = page["items"][-1]["cursor"]
    assert seen == [i["cursor"] for i in full]

    # poll forwards from the oldest item
    cursor = full[-1]["cursor"]
    newer: list[str] = []
    while True:
        page = stats.activity(conn, limit=2, after=cursor)
        if not page["items"]:
            break
        items = page["items"]
        assert [stats.parse_cursor(i["cursor"]) for i in items] == sorted(
            (stats.parse_cursor(i["cursor"]) for i in items), reverse=True
        )
        newer = [i["cursor"] for i in items] + newer
        cursor = items[0]["cursor"]
    assert newer == [i["cursor"] for i in full[:-1]]


def test_activity_filters_and_events_respect_model(conn: sqlite3.Connection) -> None:
    insert(
        conn,
        rec_from("chat_nonstream"),
        rec_from("chat_nonstream", http_status=404, error_code="model_not_found"),
        rec_from("chat_nonstream", model=OTHER, is_vision=True, image_count=1),
    )
    db.insert_event(conn, NOW, "model_changed", OTHER, MODEL)
    db.insert_event(conn, NOW, "gufo_down", None, None)
    conn.commit()
    errs = stats.activity(conn, filter_="errors")["items"]
    assert [i["type"] for i in errs].count("request") == 1
    assert [i["type"] for i in errs].count("event") == 2  # events ignore the filter
    mine = stats.activity(conn, model=MODEL)["items"]
    assert [i.get("kind") for i in mine if i["type"] == "event"] == ["gufo_down"]
    vis = stats.activity(conn, filter_="vision")["items"]
    assert [i["model"] for i in vis if i["type"] == "request"] == [OTHER]


# --------------------------------------------------------------------------- #
# Writer pipeline
# --------------------------------------------------------------------------- #


def test_writer_queue_full_and_shutdown_drain(tmp_path: Path) -> None:
    path = str(tmp_path / "w.sqlite")
    db.init_db(path)

    async def run() -> StatsWriter:
        w = StatsWriter(path, queue_size=5, retention_days=90)
        for _ in range(8):  # not started yet: 5 fit, 3 are dropped
            w.submit(rec_from("chat_nonstream"))
        assert w.counters.stats_dropped_queue_full == 3
        w.start()
        await w.stop(5.0)
        return w

    w = asyncio.run(run())
    assert w.counters.stats_written == 5
    c = sqlite3.connect(path)
    assert c.execute("SELECT COUNT(*) FROM request_stats").fetchone()[0] == 5
    assert c.execute("SELECT requests FROM daily_rollup").fetchone()[0] == 5


def test_write_error_counted(tmp_path: Path) -> None:
    path = str(tmp_path / "w.sqlite")
    db.init_db(path)

    async def run() -> StatsWriter:
        w = StatsWriter(path, queue_size=10, retention_days=90)
        w.start()
        bad = rec_from("chat_nonstream")
        bad["completed_at_ms"] = "not-a-number"
        w.submit(bad)
        w.submit(rec_from("chat_nonstream"))
        await w.flush()
        await w.stop(5.0)
        return w

    w = asyncio.run(run())
    assert w.counters.stats_write_errors == 1 and w.counters.stats_written == 1


def test_queue_full_does_not_affect_response(dash: Dash, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = dash.ctx

    def reject_job(job: Any) -> None:
        raise asyncio.QueueFull

    # Exercise the real QueueFull handling without racing the writer consumer
    # or letting a placeholder item enter its database batch.
    with monkeypatch.context() as patch:
        patch.setattr(ctx.writer.queue, "put_nowait", reject_job)
        with dash.client() as c:
            r = c.post(
                "/v1/chat/completions", json={"model": MODEL, "messages": [], "max_tokens": 16}
            )
        assert r.status_code == 200 and r.json()["model"] == MODEL
        deadline = time.time() + 5
        while ctx.writer.counters.stats_dropped_queue_full == 0 and time.time() < deadline:
            time.sleep(0.01)
        assert ctx.writer.counters.stats_dropped_queue_full == 1
        with dash.client() as c:
            status = c.get("/api/status").json()
        assert status["pipeline"]["stats_dropped_queue_full"] == 1


def test_server_shutdown_drains(dash: Dash) -> None:
    with dash.client() as c:
        for _ in range(5):
            c.post("/v1/chat/completions", json={"model": MODEL, "messages": [], "max_tokens": 16})
    dash.server.stop()
    assert len(dash.rows()) == 5


def test_clear_requires_confirmation(dash: Dash) -> None:
    with dash.client() as c:
        c.post("/v1/chat/completions", json={"model": MODEL, "messages": [], "max_tokens": 16})
        dash.wait_rows(1)
        assert c.post("/api/stats/clear", json={}).status_code == 400
        assert c.post("/api/stats/clear", json={"confirm": "yes"}).status_code == 400
        assert c.post("/api/stats/clear", json={"confirm": "clear"}).json() == {"status": "cleared"}
        assert dash.rows() == []
        ev = c.get("/api/events").json()["events"]
        assert ev[0]["kind"] == "stats_cleared"
