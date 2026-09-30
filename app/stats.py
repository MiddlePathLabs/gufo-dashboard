"""Aggregations for the dashboard API (read-only, per-model aware).

Rules:
- Weighted rates are primary (tokens per second from summed tokens / summed ms).
- Rates, TTFT, cache and acceptance are only ever computed per model.
- Missing data stays null; coverage is reported as `n` / `of`.
- `range=all` sums come from `daily_rollup` (survives retention); percentiles
  only cover retained raw rows and report `percentile_window_start_ms`.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .db import ROLLUP_FIELDS

RANGES_MS: dict[str, int | None] = {
    "1h": 3_600_000,
    "24h": 86_400_000,
    "7d": 7 * 86_400_000,
    "30d": 30 * 86_400_000,
    "all": None,
}
BUCKETS_MS = (
    10_000,
    30_000,
    60_000,
    120_000,
    300_000,
    600_000,
    900_000,
    1_800_000,
    3_600_000,
    7_200_000,
    10_800_000,
    21_600_000,
    43_200_000,
    86_400_000,
    7 * 86_400_000,
)

# Raw-row equivalents of daily_rollup columns (see db.rollup_contribution).
AGG_SQL = {
    "requests": "COUNT(*)",
    "errors": "SUM(CASE WHEN http_status >= 400 OR error_code = 'upstream_unreachable' THEN 1 ELSE 0 END)",
    "cancelled": "SUM(CASE WHEN finish_reason = 'client_cancelled' THEN 1 ELSE 0 END)",
    "streaming": "SUM(CASE WHEN is_streaming THEN 1 ELSE 0 END)",
    "vision": "SUM(CASE WHEN is_vision THEN 1 ELSE 0 END)",
    "prompt_tokens": "SUM(prompt_tokens)",
    "cached_tokens": "SUM(cached_tokens)",
    "completion_tokens": "SUM(completion_tokens)",
    "reasoning_tokens": "SUM(reasoning_tokens)",
    "share_cached": "SUM(CASE WHEN cached_tokens IS NOT NULL AND prompt_tokens IS NOT NULL THEN cached_tokens END)",
    "share_prompt": "SUM(CASE WHEN cached_tokens IS NOT NULL AND prompt_tokens IS NOT NULL THEN prompt_tokens END)",
    "prefill_tokens_w": "SUM(CASE WHEN prefill_tokens > 0 AND prefill_ms > 0 THEN prefill_tokens END)",
    "prefill_ms_w": "SUM(CASE WHEN prefill_tokens > 0 AND prefill_ms > 0 THEN prefill_ms END)",
    "decode_tokens_w": "SUM(CASE WHEN completion_tokens > 0 AND decode_ms > 0 THEN completion_tokens END)",
    "decode_ms_w": "SUM(CASE WHEN completion_tokens > 0 AND decode_ms > 0 THEN decode_ms END)",
    "draft_tokens": "SUM(CASE WHEN draft_tokens IS NOT NULL AND draft_tokens_accepted IS NOT NULL THEN draft_tokens END)",
    "draft_accepted": "SUM(CASE WHEN draft_tokens IS NOT NULL AND draft_tokens_accepted IS NOT NULL THEN draft_tokens_accepted END)",
    "draft_requests": "SUM(CASE WHEN draft_tokens IS NOT NULL AND draft_tokens_accepted IS NOT NULL THEN 1 ELSE 0 END)",
    "cache_hits": "SUM(CASE WHEN cache_hit = 1 THEN 1 ELSE 0 END)",
    "cache_known": "SUM(CASE WHEN cache_hit IS NOT NULL THEN 1 ELSE 0 END)",
    "restore_ms_sum": "SUM(CASE WHEN cache_hit = 1 THEN cache_restore_ms END)",
    "restore_n": "SUM(CASE WHEN cache_hit = 1 AND cache_restore_ms IS NOT NULL THEN 1 ELSE 0 END)",
    "ttft_sum": "SUM(ttft_ms)",
    "ttft_n": "COUNT(ttft_ms)",
    "ttft_proxy_n": "SUM(CASE WHEN ttft_source = 'proxy_stream' THEN 1 ELSE 0 END)",
    "latency_sum": "SUM(total_request_ms)",
    "latency_n": "COUNT(total_request_ms)",
    "latency_max": "MAX(total_request_ms)",
}
assert set(ROLLUP_FIELDS) | {"latency_max"} == set(AGG_SQL)

_AGG_SELECT = ", ".join(f"{sql} AS {name}" for name, sql in AGG_SQL.items())
_ROLLUP_SELECT = (
    ", ".join(f"SUM({f}) AS {f}" for f in ROLLUP_FIELDS) + ", MAX(latency_max) AS latency_max"
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def percentile(values: list[float], p: float) -> float | None:
    """Linear-interpolated percentile (p in 0..100)."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p / 100.0
    lo = math.floor(k)
    hi = math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _ratio(num: float | None, den: float | None, scale: float = 1.0) -> float | None:
    if num is None or not den:
        return None
    return scale * num / den


def _i(v: Any) -> int:
    return int(v or 0)


def resolve_range(range_: str, now: int) -> tuple[int | None, int]:
    if range_ not in RANGES_MS:
        raise ValueError("invalid range")
    span = RANGES_MS[range_]
    return (None if span is None else now - span), now


def _where(start: int | None, end: int | None, model: str | None) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if start is not None:
        clauses.append("completed_at_ms >= ?")
        params.append(start)
    if end is not None:
        clauses.append("completed_at_ms <= ?")
        params.append(end)
    if model == "":  # rows whose model is unknown (rollup key '')
        clauses.append("model IS NULL")
    elif model is not None:
        clauses.append("model = ?")
        params.append(model)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def counts_block(a: dict[str, Any]) -> dict[str, Any]:
    """Counts and token totals: safe to aggregate across models."""
    requests = _i(a.get("requests"))
    errors = _i(a.get("errors"))
    return {
        "requests": requests,
        "errors": errors,
        "error_rate": _ratio(errors, requests),
        "cancelled": _i(a.get("cancelled")),
        "streaming": _i(a.get("streaming")),
        "vision": _i(a.get("vision")),
        "prompt_tokens": _i(a.get("prompt_tokens")),
        "cached_tokens": _i(a.get("cached_tokens")),
        "generated_tokens": _i(a.get("completion_tokens")),
        "reasoning_tokens": _i(a.get("reasoning_tokens")),
        "latency_avg_ms": _ratio(a.get("latency_sum"), a.get("latency_n")),
        "latency_max_ms": a.get("latency_max"),
    }


def rates_block(a: dict[str, Any]) -> dict[str, Any]:
    """Per-model only: never call this on sums spanning several models."""
    prefill_tps = _ratio(a.get("prefill_tokens_w"), a.get("prefill_ms_w"), 1000.0)
    return {
        "decode_tps_weighted": _ratio(a.get("decode_tokens_w"), a.get("decode_ms_w"), 1000.0),
        "prefill_tps_weighted": prefill_tps,
        "ttft_avg_ms": _ratio(a.get("ttft_sum"), a.get("ttft_n")),
        "ttft_n": _i(a.get("ttft_n")),
        "ttft_proxy_stream_n": _i(a.get("ttft_proxy_n")),
        "cache_hits": _i(a.get("cache_hits")),
        "cache_misses": _i(a.get("cache_known")) - _i(a.get("cache_hits")),
        "cache_known_n": _i(a.get("cache_known")),
        "cache_hit_rate": _ratio(a.get("cache_hits"), a.get("cache_known")),
        "cached_token_share": _ratio(a.get("share_cached"), a.get("share_prompt")),
        "cache_restore_avg_ms": _ratio(a.get("restore_ms_sum"), a.get("restore_n")),
        "saved_prefill_s_estimate": (
            _ratio(a.get("cached_tokens"), prefill_tps)
            if prefill_tps and a.get("cached_tokens")
            else None
        ),
        "draft_tokens": _i(a.get("draft_tokens")),
        "draft_accepted": _i(a.get("draft_accepted")),
        "draft_requests": _i(a.get("draft_requests")),
        "draft_acceptance": _ratio(a.get("draft_accepted"), a.get("draft_tokens")),
    }


def _agg_raw(
    conn: sqlite3.Connection, start: int | None, end: int | None, model: str | None
) -> dict[str, dict[str, Any]]:
    where, params = _where(start, end, model)
    rows = conn.execute(
        f"SELECT model, {_AGG_SELECT} FROM request_stats{where} GROUP BY model", params
    ).fetchall()
    return {(r["model"] or ""): dict(r) for r in rows}


def _agg_rollup(conn: sqlite3.Connection, model: str | None) -> dict[str, dict[str, Any]]:
    where, params = ("", []) if model is None else (" WHERE model = ?", [model])
    rows = conn.execute(
        f"SELECT model, {_ROLLUP_SELECT} FROM daily_rollup{where} GROUP BY model", params
    ).fetchall()
    return {r["model"]: dict(r) for r in rows}


def _sum_aggs(aggs: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for a in aggs:
        for k, v in a.items():
            if k == "model" or v is None:
                continue
            if k == "latency_max":
                out[k] = max(out.get(k) or 0, v)
            else:
                out[k] = (out.get(k) or 0) + v
    return out


def _percentiles(
    conn: sqlite3.Connection, start: int | None, end: int | None, model: str
) -> dict[str, Any]:
    where, params = _where(start, end, model)
    rows = conn.execute(
        f"SELECT ttft_ms, completion_tokens_per_second, prompt_tokens_per_second "
        f"FROM request_stats{where}",
        params,
    ).fetchall()
    ttft = [r[0] for r in rows if r[0] is not None]
    dec = [r[1] for r in rows if r[1] is not None]
    pre = [r[2] for r in rows if r[2] is not None]
    return {
        "ttft_p50_ms": percentile(ttft, 50),
        "ttft_p95_ms": percentile(ttft, 95),
        "decode_tps_p50": percentile(dec, 50),
        "decode_tps_p95": percentile(dec, 95),
        "decode_n": len(dec),
        "prefill_tps_p50": percentile(pre, 50),
        "prefill_tps_p95": percentile(pre, 95),
        "prefill_n": len(pre),
    }


def _window_start(conn: sqlite3.Connection, model: str | None) -> int | None:
    where, params = _where(None, None, model)
    row = conn.execute(f"SELECT MIN(completed_at_ms) FROM request_stats{where}", params).fetchone()
    return row[0] if row else None


def _model_aggs(
    conn: sqlite3.Connection, range_: str, model: str | None, now: int
) -> tuple[int | None, int, dict[str, dict[str, Any]]]:
    start, end = resolve_range(range_, now)
    if range_ == "all":
        return start, end, _agg_rollup(conn, model)
    return start, end, _agg_raw(conn, start, end, model)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


def summary(conn: sqlite3.Connection, range_: str, model: str | None, now: int) -> dict[str, Any]:
    start, end, aggs = _model_aggs(conn, range_, model, now)
    pct_start = start if range_ != "all" else None
    out: dict[str, Any] = {
        "range": range_,
        "model": model,
        "start_ms": start,
        "end_ms": end,
        **counts_block(_sum_aggs(list(aggs.values()))),
        "percentile_window_start_ms": (_window_start(conn, model) if range_ == "all" else start),
        "per_model": [],
    }
    for name, agg in sorted(aggs.items()):
        block = {
            "model": name or None,
            **counts_block(agg),
            **rates_block(agg),
            **_percentiles(conn, pct_start, end if range_ != "all" else None, name),
        }
        out["per_model"].append(block)
    if model is not None:
        single = (
            out["per_model"][0]
            if out["per_model"]
            else {
                "model": model,
                **counts_block({}),
                **rates_block({}),
                **_percentiles(conn, pct_start, None, model),
            }
        )
        out.update({k: v for k, v in single.items() if k != "model"})
    return out


def pick_bucket(span_ms: int, bucket: str) -> int:
    if bucket != "auto":
        size = int(bucket)
        if size < 1000:
            raise ValueError("bucket too small")
        return size
    for size in BUCKETS_MS:
        if span_ms / size <= 120:
            return size
    return BUCKETS_MS[-1]


def timeseries(
    conn: sqlite3.Connection, range_: str, model: str | None, now: int, bucket: str = "auto"
) -> dict[str, Any]:
    start, end = resolve_range(range_, now)
    source = "raw"
    if start is None:
        first_raw = _window_start(conn, model)
        where, params = ("", []) if model is None else (" WHERE model = ?", [model])
        row = conn.execute(f"SELECT MIN(first_ms) FROM daily_rollup{where}", params).fetchone()
        first_rollup = row[0] if row else None
        if first_rollup is not None and (first_raw is None or first_rollup < first_raw):
            source = "rollup"  # raw rows were pruned: fall back to daily sums
            start = first_rollup
        else:
            start = first_raw if first_raw is not None else now - 3_600_000
    size = 86_400_000 if source == "rollup" else pick_bucket(max(end - start, 1), bucket)
    t0 = (start // size) * size
    n = int((end - t0) // size) + 1
    xs = [t0 + i * size for i in range(n)]

    per_model: dict[str, dict[int, dict[str, Any]]] = {}
    ttft_vals: dict[tuple[str, int], list[float]] = {}
    if source == "raw":
        where, params = _where(t0, end, model)
        rows = conn.execute(
            f"SELECT model, (completed_at_ms - ?) / ? AS b, {_AGG_SELECT} "
            f"FROM request_stats{where} GROUP BY model, b",
            [t0, size, *params],
        ).fetchall()
        for r in rows:
            per_model.setdefault(r["model"] or "", {})[int(r["b"])] = dict(r)
        for r in conn.execute(
            f"SELECT model, (completed_at_ms - ?) / ? AS b, ttft_ms "
            f"FROM request_stats{where} AND ttft_ms IS NOT NULL",
            [t0, size, *params],
        ):
            ttft_vals.setdefault((r["model"] or "", int(r["b"])), []).append(r["ttft_ms"])
    else:
        where, params = ("", []) if model is None else (" WHERE model = ?", [model])
        rows = conn.execute(
            f"SELECT day, model, {', '.join(ROLLUP_FIELDS)}, latency_max FROM daily_rollup{where}",
            params,
        ).fetchall()
        for r in rows:
            day_ms = _day_ms(r["day"])
            b = int((day_ms - t0) // size)
            if 0 <= b < n:
                per_model.setdefault(r["model"], {})[b] = dict(r)

    totals = {k: [0] * n for k in ("requests", "errors", "prompt_tokens", "generated_tokens")}
    series: dict[str, dict[str, list[float | None]]] = {}
    for name, buckets in per_model.items():
        s: dict[str, list[float | None]] = {
            k: [None] * n
            for k in (
                "requests",
                "decode_tps",
                "prefill_tps",
                "ttft_avg_ms",
                "ttft_p50_ms",
                "draft_acceptance",
                "cache_hit_rate",
            )
        }
        for b, a in buckets.items():
            if not 0 <= b < n:
                continue
            c = counts_block(a)
            rt = rates_block(a)
            totals["requests"][b] += c["requests"]
            totals["errors"][b] += c["errors"]
            totals["prompt_tokens"][b] += c["prompt_tokens"]
            totals["generated_tokens"][b] += c["generated_tokens"]
            s["requests"][b] = c["requests"]
            s["decode_tps"][b] = rt["decode_tps_weighted"]
            s["prefill_tps"][b] = rt["prefill_tps_weighted"]
            s["ttft_avg_ms"][b] = rt["ttft_avg_ms"]
            s["ttft_p50_ms"][b] = percentile(ttft_vals.get((name, b), []), 50)
            s["draft_acceptance"][b] = rt["draft_acceptance"]
            s["cache_hit_rate"][b] = rt["cache_hit_rate"]
        series[name or "(unknown)"] = s
    return {
        "range": range_,
        "model": model,
        "source": source,
        "bucket_ms": size,
        "start_ms": t0,
        "end_ms": end,
        "t": xs,
        **totals,
        "per_model": series,
    }


def _day_ms(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000)


# -- activity feed ----------------------------------------------------------- #

_REQUEST_LIST_COLS = (
    "id, completed_at_ms, started_at_ms, endpoint, model, is_streaming, is_vision, image_count, "
    "http_status, error_code, finish_reason, prompt_tokens, cached_tokens, completion_tokens, "
    "completion_tokens_per_second, total_request_ms, cache_hit, ttft_ms, ttft_source"
)
_FILTERS = {
    "content": " AND EXISTS (SELECT 1 FROM request_content c WHERE c.request_id=request_stats.id AND (c.question IS NOT NULL OR c.answer IS NOT NULL))",
    "partial": " AND EXISTS (SELECT 1 FROM request_content c WHERE c.request_id=request_stats.id AND (c.status='partial' OR c.question_truncated=1 OR c.answer_truncated=1) AND (c.question IS NOT NULL OR c.answer IS NOT NULL))",
    "all": "",
    "errors": " AND (http_status >= 400 OR error_code IS NOT NULL)",
    "vision": " AND is_vision = 1",
    "streaming": " AND is_streaming = 1",
    "cancelled": " AND finish_reason = 'client_cancelled'",
}


def parse_cursor(cursor: str) -> tuple[int, str, int]:
    ts, typ, ident = cursor.split(":")
    if typ not in ("request", "event"):
        raise ValueError("bad cursor")
    return int(ts), typ, int(ident)


def _cursor(ts: int, typ: str, ident: int) -> str:
    return f"{ts}:{typ}:{ident}"


def activity(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    before: str | None = None,
    after: str | None = None,
    model: str | None = None,
    filter_: str = "all",
    content_cutoff_ms: int | None = None,
) -> dict[str, Any]:
    """Merged request + event feed ordered by (ts, type, id) descending.

    `before` pages backwards. `after` returns up to `limit` of the *oldest*
    items newer than the cursor (still newest-first), so repeated polling
    never skips anything; `has_more` says whether to poll again right away.
    """
    if filter_ not in _FILTERS:
        raise ValueError("invalid filter")
    limit = max(1, min(limit, 500))
    newer = after is not None
    raw_cursor = after if after is not None else before
    cur = parse_cursor(raw_cursor) if raw_cursor else None
    cmp_ = ">" if newer else "<"
    order = "ASC" if newer else "DESC"

    items: list[tuple[tuple[int, str, int], dict[str, Any]]] = []

    rq_where = " WHERE 1=1" + _FILTERS[filter_]
    rq_params: list[Any] = []
    if filter_ in ("content", "partial") and content_cutoff_ms is not None:
        rq_where += " AND EXISTS (SELECT 1 FROM request_content c WHERE c.request_id=request_stats.id AND c.captured_at_ms >= ?)"
        rq_params.append(content_cutoff_ms)
    if model is not None:
        rq_where += " AND model = ?"
        rq_params.append(model)
    if cur is not None:
        rq_where += f" AND (completed_at_ms, 'request', id) {cmp_} (?, ?, ?)"
        rq_params += list(cur)
    for r in conn.execute(
        f"SELECT {_REQUEST_LIST_COLS} FROM request_stats{rq_where} "
        f"ORDER BY completed_at_ms {order}, id {order} LIMIT ?",
        [*rq_params, limit + 1],
    ):
        d = dict(r)
        key = (d["completed_at_ms"], "request", d["id"])
        items.append((key, {"type": "request", "cursor": _cursor(*key), **d}))

    ev_where = " WHERE 1=1"
    ev_params: list[Any] = []
    if model is not None:
        ev_where += " AND (model IS NULL OR model = ?)"
        ev_params.append(model)
    if cur is not None:
        ev_where += f" AND (ts, 'event', id) {cmp_} (?, ?, ?)"
        ev_params += list(cur)
    for r in conn.execute(
        f"SELECT id, ts, kind, model, detail FROM events{ev_where} "
        f"ORDER BY ts {order}, id {order} LIMIT ?",
        [*ev_params, limit + 1],
    ):
        d = dict(r)
        key = (d["ts"], "event", d["id"])
        items.append((key, {"type": "event", "cursor": _cursor(*key), **d}))

    items.sort(key=lambda kv: kv[0], reverse=not newer)
    has_more = len(items) > limit
    page = [v for _, v in items[:limit]]
    if newer:
        page.reverse()
    return {"items": page, "has_more": has_more}


def request_detail(conn: sqlite3.Connection, ident: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM request_stats WHERE id = ?", (ident,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["extra_metrics"] = json.loads(d["extra_metrics"]) if d["extra_metrics"] else {}
    except ValueError:
        d["extra_metrics"] = {}
    for k in ("is_streaming", "is_vision", "cache_hit"):
        if d.get(k) is not None:
            d[k] = bool(d[k])
    return d


def models(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT model FROM request_stats WHERE model IS NOT NULL "
        "UNION SELECT model FROM daily_rollup WHERE model != '' ORDER BY 1"
    ).fetchall()
    return [r[0] for r in rows]


def events(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, ts, kind, model, detail FROM events ORDER BY ts DESC, id DESC LIMIT ?",
        (max(1, min(limit, 500)),),
    ).fetchall()
    return [dict(r) for r in rows]


def speculative(
    conn: sqlite3.Connection, range_: str, model: str | None, now: int
) -> dict[str, Any]:
    _, _, aggs = _model_aggs(conn, range_, model, now)
    out = []
    for name, a in sorted(aggs.items()):
        r = rates_block(a)
        out.append(
            {
                "model": name or None,
                "requests": _i(a.get("requests")),
                "draft_requests": r["draft_requests"],
                "draft_tokens": r["draft_tokens"],
                "draft_accepted": r["draft_accepted"],
                "draft_acceptance": r["draft_acceptance"],
                "decode_tps_weighted": r["decode_tps_weighted"],
            }
        )
    return {"range": range_, "model": model, "per_model": out}


def cache(conn: sqlite3.Connection, range_: str, model: str | None, now: int) -> dict[str, Any]:
    start, end, aggs = _model_aggs(conn, range_, model, now)
    where, params = _where(start, end, model)
    reasons: dict[str, dict[str, int]] = {}
    for r in conn.execute(
        f"SELECT model, cache_miss_reason, COUNT(*) FROM request_stats{where}"
        + (" AND" if where else " WHERE")
        + " cache_hit = 0 GROUP BY model, cache_miss_reason",
        params,
    ):
        reasons.setdefault(r[0] or "", {})[r[1] or "unknown"] = r[2]
    out = []
    for name, a in sorted(aggs.items()):
        r = rates_block(a)
        out.append(
            {
                "model": name or None,
                "requests": _i(a.get("requests")),
                "hits": r["cache_hits"],
                "misses": r["cache_misses"],
                "known_n": r["cache_known_n"],
                "hit_rate": r["cache_hit_rate"],
                "cached_tokens": _i(a.get("cached_tokens")),
                "cached_token_share": r["cached_token_share"],
                "restore_avg_ms": r["cache_restore_avg_ms"],
                "prefill_tps_weighted": r["prefill_tps_weighted"],
                "saved_prefill_s_estimate": r["saved_prefill_s_estimate"],
                "miss_reasons": reasons.get(name, {}),
            }
        )
    return {
        "range": range_,
        "model": model,
        "miss_reasons_window_start_ms": _window_start(conn, model) if range_ == "all" else start,
        "per_model": out,
    }


def unattributed_window(conn: sqlite3.Connection, start_ms: int, end_ms: int) -> dict[str, int]:
    row = conn.execute(
        "SELECT SUM(prompt_tokens), SUM(completion_tokens), "
        "SUM(CASE WHEN finish_reason = 'client_cancelled' THEN 1 ELSE 0 END), COUNT(*) "
        "FROM request_stats WHERE completed_at_ms >= ? AND completed_at_ms <= ?",
        (start_ms, end_ms),
    ).fetchone()
    return {
        "prompt_tokens": _i(row[0]),
        "completion_tokens": _i(row[1]),
        "cancelled": _i(row[2]),
        "rows": _i(row[3]),
    }
