"""Request inspection and response stats extraction.

Everything here is defensive: any field may be absent, null, the wrong type or
new. Text content (prompts, outputs, reasoning, tool data, error messages) is
only ever looked at transiently and is never copied into a record.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

INT_MAX = 2**63 - 1
_STR_RE = re.compile(r"^[A-Za-z0-9_.:/-]+$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_EXTRA_KEYS = 64

# Recorded endpoints (POST only) -> response shape.
RECORDED_ENDPOINTS: dict[str, str] = {
    "/v1/chat/completions": "chat",
    "/v1/completions": "completions",
    "/v1/responses": "responses",
    "/v1/messages": "messages",
    "/completion": "native",
}
INJECTABLE = {"chat", "completions"}
IMAGE_PART_TYPES = {"image_url", "input_image", "image"}


class _NonFinite:
    """Stand-in for NaN / Infinity so the rest of a document still parses."""

    __slots__ = ()


_NON_FINITE = _NonFinite()


def _parse_constant(_name: str) -> _NonFinite:
    return _NON_FINITE


def loads_strict(data: bytes | str) -> Any:
    """json.loads that turns NaN/Infinity into an invalid sentinel."""
    return json.loads(data, parse_constant=_parse_constant)


# --------------------------------------------------------------------------- #
# Value validation
# --------------------------------------------------------------------------- #


def _num(v: Any) -> int | float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if v < 0:
        return None
    if isinstance(v, int) and v > INT_MAX:
        return None
    return v


def as_int(v: Any) -> int | None:
    n = _num(v)
    if n is None:
        return None
    if isinstance(n, float):
        if not n.is_integer() or n > INT_MAX:
            return None
        return int(n)
    return n


def as_float(v: Any) -> float | None:
    n = _num(v)
    return None if n is None else float(n)


def as_bool(v: Any) -> bool | None:
    return v if isinstance(v, bool) else None


def as_str(v: Any, max_len: int = 64) -> str | None:
    if isinstance(v, str) and 0 < len(v) <= max_len and _STR_RE.match(v):
        return v
    return None


def _dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _first(*values: Any) -> Any:
    for v in values:
        if v is not None:
            return v
    return None


# --------------------------------------------------------------------------- #
# Request side
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RequestInfo:
    kind: str
    is_streaming: bool = False
    model: str | None = None
    image_count: int = 0
    usage_requested: bool = False  # caller asked for include_usage itself
    injected: bool = False  # we added include_usage and must strip the event
    body: bytes = b""


def _count_images(node: Any, depth: int = 0) -> int:
    if depth > 32:
        return 0
    if isinstance(node, list):
        return sum(_count_images(x, depth + 1) for x in node)
    if isinstance(node, dict):
        n = 1 if node.get("type") in IMAGE_PART_TYPES else 0
        for key in ("content", "input", "messages"):
            if key in node:
                n += _count_images(node[key], depth + 1)
        return n
    return 0


def analyze_request(kind: str, body: bytes) -> RequestInfo:
    """Parse the request JSON once; decide on usage injection.

    Only `stream`, `stream_options`, `model` and image parts are looked at.
    The parsed object is dropped before returning.
    """
    info = RequestInfo(kind=kind, body=body)
    try:
        obj = loads_strict(body)
    except (ValueError, RecursionError):
        return info
    if not isinstance(obj, dict):
        return info
    info.is_streaming = obj.get("stream") is True
    info.model = as_str(obj.get("model"), 128)
    info.image_count = _count_images(obj.get("messages")) + _count_images(obj.get("input"))

    if kind in INJECTABLE and info.is_streaming:
        missing = object()
        so = obj.get("stream_options", missing)
        if so is missing or isinstance(so, dict):
            if isinstance(so, dict) and so.get("include_usage") is True:
                info.usage_requested = True
            else:
                new_so = dict(so) if isinstance(so, dict) else {}
                new_so["include_usage"] = True
                obj["stream_options"] = new_so
                try:
                    info.body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode(
                        "utf-8"
                    )
                    info.injected = True
                except (TypeError, ValueError):
                    # e.g. NaN/Infinity in the caller's body: never "repair" it.
                    info.body = body
    return info


# --------------------------------------------------------------------------- #
# Response side
# --------------------------------------------------------------------------- #

# usage.gufo keys that have their own column (everything else numeric/bool
# goes to extra_metrics).
_GUFO_INT = (
    "cache_common_prefix_tokens",
    "cache_restore_bytes",
    "prefill_chunks",
    "queue_depth_at_submit",
    "client_queue_depth_at_submit",
    "resident_requests_at_admission",
    "requested_logical_concurrency",
    "physical_execution_width",
)
_GUFO_FLOAT = ("queue_ms", "mean_inter_token_ms", "max_inter_token_ms")
GUFO_COLUMN_KEYS = {
    *_GUFO_INT,
    *_GUFO_FLOAT,
    "cache_hit",
    "cache_miss_reason",
    "cache_restore_ms",
    "prefill_tokens",
    "prefill_ms",
    "decode_ms",
    "ttft_ms",
    "execution_plan",
}


def normalize_usage(usage: Any, timings: Any, *, reasoning: bool = False) -> dict[str, Any]:
    """Map usage / usage.gufo / timings to record columns.

    Precedence usage.gufo > usage > timings. Only non-null values are
    returned so results of several events can be merged.
    """
    u = _dict(usage)
    t = _dict(timings)
    g = _dict(u.get("gufo"))
    r: dict[str, Any] = {
        "prompt_tokens": _first(as_int(u.get("prompt_tokens")), as_int(u.get("input_tokens"))),
        "cached_tokens": _first(
            as_int(_dict(u.get("prompt_tokens_details")).get("cached_tokens")),
            as_int(u.get("cached_tokens")),
            as_int(_dict(u.get("input_tokens_details")).get("cached_tokens")),
            as_int(u.get("cache_read_input_tokens")),
            as_int(t.get("cache_n")),
        ),
        "prefill_tokens": _first(as_int(g.get("prefill_tokens")), as_int(t.get("prompt_n"))),
        "completion_tokens": _first(
            as_int(u.get("completion_tokens")),
            as_int(u.get("output_tokens")),
            as_int(t.get("predicted_n")),
        ),
        "draft_tokens": _first(as_int(u.get("draft_tokens")), as_int(t.get("draft_n"))),
        "draft_tokens_accepted": _first(
            as_int(u.get("draft_tokens_accepted")), as_int(t.get("draft_n_accepted"))
        ),
        "prefill_ms": _first(as_float(g.get("prefill_ms")), as_float(t.get("prompt_ms"))),
        "decode_ms": _first(as_float(g.get("decode_ms")), as_float(t.get("predicted_ms"))),
        "ttft_ms": as_float(g.get("ttft_ms")),  # Gufo-reported TTFT only
        "cache_hit": as_bool(g.get("cache_hit")),
        "cache_miss_reason": as_str(g.get("cache_miss_reason")),
        "cache_restore_ms": _first(
            as_float(g.get("cache_restore_ms")), as_float(t.get("cache_restore_ms"))
        ),
        "execution_plan": as_str(g.get("execution_plan")),
        "_prompt_rate": _first(
            as_float(u.get("prompt_tokens_per_second")), as_float(t.get("prompt_per_second"))
        ),
        "_decode_rate": _first(
            as_float(u.get("completion_tokens_per_second")),
            as_float(t.get("predicted_per_second")),
        ),
    }
    for key in _GUFO_INT:
        r[key] = as_int(g.get(key))
    for key in _GUFO_FLOAT:
        r[key] = as_float(g.get(key))
    if reasoning:
        r["reasoning_tokens"] = as_int(
            _dict(u.get("output_tokens_details")).get("reasoning_tokens")
        )

    extra: dict[str, int | float | bool] = {}
    for key, value in g.items():
        if len(extra) >= MAX_EXTRA_KEYS:
            break
        if key in GUFO_COLUMN_KEYS or not isinstance(key, str) or not _KEY_RE.match(key):
            continue
        if isinstance(value, bool):
            extra[key] = value
        elif (n := _num(value)) is not None:
            extra[key] = n
    # Gufo 0.7 reports speculative verification rounds in timings even for
    # endpoints without a usage.gufo block; surface them like the other extras.
    if (rounds := as_int(t.get("draft_rounds"))) is not None and (
        "draft_rounds" in extra or len(extra) < MAX_EXTRA_KEYS
    ):
        extra.setdefault("draft_rounds", rounds)
    if extra:
        r["extra_metrics"] = extra
    return {k: v for k, v in r.items() if v is not None}


def merge(into: dict[str, Any], new: dict[str, Any]) -> None:
    for key, value in new.items():
        if key == "extra_metrics" and isinstance(into.get(key), dict):
            merged = {**into[key], **value}
            into[key] = dict(list(merged.items())[:MAX_EXTRA_KEYS])
        else:
            into[key] = value


def _choice_finish(obj: dict[str, Any]) -> str | None:
    choices = obj.get("choices")
    if isinstance(choices, list):
        for c in choices:
            fr = as_str(_dict(c).get("finish_reason"))
            if fr:
                return fr
    return None


def extract_chat(obj: Any) -> dict[str, Any]:
    """Chat completion / chat chunk / text_completion (non-stream or event)."""
    o = _dict(obj)
    r = normalize_usage(o.get("usage"), o.get("timings"))
    if fr := _choice_finish(o):
        r["finish_reason"] = fr
    if m := as_str(o.get("model"), 128):
        r["model"] = m
    return r


def extract_responses(obj: Any) -> dict[str, Any]:
    """Responses API `response` object (non-stream body or terminal event)."""
    o = _dict(obj)
    r = normalize_usage(o.get("usage"), o.get("timings"), reasoning=True)
    status = as_str(o.get("status"))
    reason = as_str(_dict(o.get("incomplete_details")).get("reason"))
    if status:
        r["finish_reason"] = as_str(f"{status}:{reason}") if reason else status
    if code := as_str(_dict(o.get("error")).get("code")):
        r["error_code"] = code
    if m := as_str(o.get("model"), 128):
        r["model"] = m
    return r


def extract_messages(obj: Any) -> dict[str, Any]:
    o = _dict(obj)
    r = normalize_usage(o.get("usage"), o.get("timings"))
    if sr := as_str(o.get("stop_reason")):
        r["finish_reason"] = sr
    if m := as_str(o.get("model"), 128):
        r["model"] = m
    return r


def extract_native(obj: Any) -> dict[str, Any]:
    """llama.cpp-style `/completion` (non-stream only in Gufo)."""
    o = _dict(obj)
    r = normalize_usage(o.get("usage"), o.get("timings"))
    # llama.cpp native counters as fallbacks when `usage` is missing.
    for col, key in (
        ("prompt_tokens", "tokens_evaluated"),
        ("completion_tokens", "tokens_predicted"),
        ("cached_tokens", "tokens_cached"),
    ):
        if col not in r and (v := as_int(o.get(key))) is not None:
            r[col] = v
    if o.get("stopped_eos") is True or o.get("stopped_word") is True:
        r["finish_reason"] = "stop"
    elif o.get("stopped_length") is True or o.get("stopped_limit") is True:
        r["finish_reason"] = "length"
    elif fr := as_str(o.get("finish_reason")):
        r["finish_reason"] = fr
    if m := as_str(o.get("model"), 128):
        r["model"] = m
    return r


BODY_EXTRACTORS = {
    "chat": extract_chat,
    "completions": extract_chat,
    "responses": extract_responses,
    "messages": extract_messages,
    "native": extract_native,
}


def extract_error_code(obj: Any) -> str | None:
    err = _dict(_dict(obj).get("error"))
    return _first(as_str(err.get("code")), as_str(err.get("type")))


# --------------------------------------------------------------------------- #
# Streaming inspection
# --------------------------------------------------------------------------- #

_RESPONSES_TERMINAL = {"response.completed", "response.incomplete", "response.failed"}
_RESPONSES_TOKEN = {
    "response.output_text.delta",
    "response.reasoning_summary_text.delta",
    "response.reasoning_text.delta",
}


def _nonempty_str(v: Any) -> bool:
    return isinstance(v, str) and v != ""


class StreamInspector:
    """Looks at one decoded SSE event at a time; keeps numbers only."""

    def __init__(self, kind: str, strip_usage_event: bool) -> None:
        self.kind = kind
        self.strip = strip_usage_event
        self.dropped = False
        self.first_token_seen = False
        self.prompt_progress: dict[str, int] | None = None
        self.fields: dict[str, Any] = {}

    def on_data(self, data: str) -> tuple[bool, bool]:
        """Inspect one event's data. Returns (drop_event, is_first_token)."""
        if data == "[DONE]":
            return False, False
        try:
            obj = loads_strict(data)
        except (ValueError, RecursionError):
            return False, False
        if not isinstance(obj, dict):
            return False, False
        drop = first = False
        if not self.first_token_seen and self.kind in ("chat", "completions", "responses"):
            progress = _dict(obj.get("prompt_progress"))
            total = as_int(progress.get("total"))
            cached = as_int(progress.get("cache"))
            processed = as_int(progress.get("processed"))
            if (
                total is not None
                and cached is not None
                and processed is not None
                and cached <= processed <= total
            ):
                self.prompt_progress = {"total": total, "cache": cached, "processed": processed}
        if self.kind in ("chat", "completions"):
            if not self.first_token_seen and self._chat_has_token(obj):
                first = True
            if "usage" in obj or "timings" in obj or _choice_finish(obj):
                merge(self.fields, extract_chat(obj))
            if self.strip and not self.dropped and self._is_usage_only(obj):
                drop = True
                self.dropped = True
        elif self.kind == "responses":
            etype = obj.get("type")
            if (
                not self.first_token_seen
                and etype in _RESPONSES_TOKEN
                and _nonempty_str(obj.get("delta"))
            ):
                first = True
            if etype in _RESPONSES_TERMINAL:
                merge(self.fields, extract_responses(obj.get("response")))
            elif etype == "error" and (code := as_str(obj.get("code"))):
                self.fields["error_code"] = code
        elif self.kind == "messages":
            # Gufo 0.10 streams Anthropic events: full usage and stop_reason
            # arrive on message_delta; tokens as text_delta / thinking_delta
            # (input_json_delta carries tool arguments, which are not tokens).
            etype = obj.get("type")
            delta = _dict(obj.get("delta"))
            dtype = delta.get("type")
            if etype == "message_delta":
                merge(
                    self.fields,
                    extract_messages(
                        {"usage": obj.get("usage"), "stop_reason": delta.get("stop_reason")}
                    ),
                )
            elif etype == "error" and (code := extract_error_code(obj)):
                self.fields["error_code"] = code
            if (
                not self.first_token_seen
                and etype == "content_block_delta"
                and dtype in ("text_delta", "thinking_delta")
                and _nonempty_str(
                    delta.get("text") if dtype == "text_delta" else delta.get("thinking")
                )
            ):
                first = True
        if first:
            self.first_token_seen = True
            self.prompt_progress = None
        return drop, first

    def _chat_has_token(self, obj: dict[str, Any]) -> bool:
        choices = obj.get("choices")
        if not isinstance(choices, list):
            return False
        for c in choices:
            c = _dict(c)
            if self.kind == "chat":
                d = _dict(c.get("delta"))
                if _nonempty_str(d.get("content")) or _nonempty_str(d.get("reasoning_content")):
                    return True
            elif _nonempty_str(c.get("text")):
                return True
        return False

    def _is_usage_only(self, obj: dict[str, Any]) -> bool:
        expected = "chat.completion.chunk" if self.kind == "chat" else "text_completion"
        return (
            obj.get("object") == expected
            and obj.get("choices") == []
            and isinstance(obj.get("usage"), dict)
        )


# --------------------------------------------------------------------------- #
# Final record
# --------------------------------------------------------------------------- #

RECORD_COLUMNS = (
    "started_at_ms",
    "completed_at_ms",
    "endpoint",
    "model",
    "gufo_request_id",
    "is_streaming",
    "is_vision",
    "image_count",
    "http_status",
    "error_code",
    "finish_reason",
    "prompt_tokens",
    "cached_tokens",
    "prefill_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "prompt_tokens_per_second",
    "completion_tokens_per_second",
    "prefill_ms",
    "decode_ms",
    "ttft_ms",
    "ttft_source",
    "queue_ms",
    "mean_inter_token_ms",
    "max_inter_token_ms",
    "prefill_chunks",
    "cache_hit",
    "cache_miss_reason",
    "cache_common_prefix_tokens",
    "cache_restore_ms",
    "cache_restore_bytes",
    "draft_tokens",
    "draft_tokens_accepted",
    "queue_depth_at_submit",
    "client_queue_depth_at_submit",
    "resident_requests_at_admission",
    "requested_logical_concurrency",
    "physical_execution_width",
    "execution_plan",
    "context_length",
    "context_used_pct",
    "proxy_ttfb_ms",
    "proxy_first_token_ms",
    "total_request_ms",
    "extra_metrics",
)


def _rate(tokens: int | None, ms: float | None, reported: float | None) -> float | None:
    """tokens/s with the zero-work rule: no tokens -> null, never 0."""
    if tokens is None or tokens <= 0:
        return None
    if reported is not None and reported > 0:
        return reported
    if ms is not None and ms > 0:
        return 1000.0 * tokens / ms
    return None


def finalize_record(
    fields: dict[str, Any],
    *,
    endpoint: str,
    request_model: str | None,
    is_streaming: bool,
    image_count: int,
    http_status: int,
    gufo_request_id: str | None,
    started_at_ms: int,
    completed_at_ms: int,
    proxy_ttfb_ms: float | None,
    proxy_first_token_ms: float | None,
    total_request_ms: float,
    context_lengths: dict[str, int],
    cancelled: bool,
) -> dict[str, Any]:
    rec: dict[str, Any] = dict.fromkeys(RECORD_COLUMNS)
    for key in RECORD_COLUMNS:
        if key in fields:
            rec[key] = fields[key]
    rec["prompt_tokens_per_second"] = _rate(
        rec["prefill_tokens"], rec["prefill_ms"], fields.get("_prompt_rate")
    )
    rec["completion_tokens_per_second"] = _rate(
        rec["completion_tokens"], rec["decode_ms"], fields.get("_decode_rate")
    )
    rec["model"] = fields.get("model") or request_model
    rec.update(
        started_at_ms=started_at_ms,
        completed_at_ms=completed_at_ms,
        endpoint=endpoint,
        gufo_request_id=as_str(gufo_request_id),
        is_streaming=is_streaming,
        is_vision=image_count > 0,
        image_count=image_count,
        http_status=http_status,
        proxy_ttfb_ms=proxy_ttfb_ms,
        proxy_first_token_ms=proxy_first_token_ms if is_streaming else None,
        total_request_ms=total_request_ms,
    )
    if cancelled:
        rec["finish_reason"] = "client_cancelled"
    # TTFT rule: Gufo's value, else proxy-measured for streams, else nothing.
    if rec["ttft_ms"] is not None:
        rec["ttft_source"] = "gufo"
    elif is_streaming and rec["proxy_first_token_ms"] is not None:
        rec["ttft_ms"] = rec["proxy_first_token_ms"]
        rec["ttft_source"] = "proxy_stream"
    ctx = context_lengths.get(rec["model"] or "")
    if ctx:
        rec["context_length"] = ctx
        if rec["prompt_tokens"] is not None:
            used = rec["prompt_tokens"] + (rec["completion_tokens"] or 0)
            rec["context_used_pct"] = 100.0 * used / ctx
    return rec
