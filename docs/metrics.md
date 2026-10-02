# Metrics and compatibility

Endpoint behavior below describes Gufo 0.4.0, verified against the loaded server
and checked-in, anonymized captures at engine commit `6aa87fc`. Older captures
remain available to check backward compatibility.
It is not a guarantee for every Gufo release. See the
[fixture notes](../tests/fixtures/gufo/README.md) for capture details.

| Endpoint | Streams | Where the stats are | `usage.gufo` (TTFT, queue, cache, scheduler) |
| --- | --- | --- | --- |
| `/v1/chat/completions` | yes | non-stream: `usage` + `timings`; stream: finish chunk has `timings`; the `include_usage` event has full `usage` + `timings` | **yes** (non-stream, or stream with usage) |
| `/v1/completions` | yes | `usage`; `timings` in non-stream bodies and streaming terminal chunks | no |
| `/v1/responses` | yes | `usage` (incl. `reasoning_tokens`) + `timings` in the body or the `response.completed/incomplete/failed` event | no |
| `/v1/messages` | **no** (`'stream' must be false`) | Anthropic `usage` (`input_tokens`, `output_tokens`, `cache_read_input_tokens`) + `timings` | no |
| `/completion` | **no** | `usage` + `timings` + llama.cpp `stopped_*` flags | no |

Also polled: `/ready` (online + model), `/v1/models` (`context_length`),
`/metrics` (`llamacpp:prompt_tokens_total`, `tokens_predicted_total`, and the
latest nonzero prefill/decode rate gauges), plus admitted and deferred request
counts (`requests_processing`, `requests_deferred`). These counts include direct
traffic and are separate from the dashboard’s own in-flight requests.

How figures are computed:

- **Throughput** is weighted: `1000 × Σ tokens / Σ ms` over rows where both
  are > 0, plus per-request p50/p95. A full cache hit has no prefill rate
  (null, never 0).
- **Draft acceptance** = `Σ accepted / Σ drafted`. **Cache hit rate** =
  hits / requests that report `cache_hit`.
- **TTFT** = Gufo's `usage.gufo.ttft_ms`; else, for streams only, the
  proxy-measured time to the first non-empty content/reasoning delta
  (`ttft_source = proxy_stream`); else none. Proxy TTFB is never TTFT.
- **Rates, TTFT, cache and acceptance are always per model.** The "All
  models" view shows counts and token totals, and per-model series.

## Limitations

- No `usage.gufo` for Responses, Messages or Completions: TTFT is
  proxy-measured when streaming and unavailable otherwise; no queue, cache-hit
  or scheduler fields for those endpoints.
- Gufo reports no uptime; the dashboard shows **observed ready duration**
  (since it first saw Gufo ready after the last down / counter reset).
- `/v1/messages` and `/completion` do not stream in Gufo.
- KV usage (`kv_cache_usage_ratio`) and `/slots` are placeholders in Gufo and
  are ignored.
- `range=all` counts, token totals, weighted rates, cache hit rate and
  acceptance come from the daily rollup and are truly all-time; **percentiles,
  miss-reason breakdowns and the request list only cover retained raw rows**
  (labelled "last N d").
- Traffic sent to `:8080` directly contributes to **unattributed tokens**
  (a reset-aware `/metrics` delta minus recorded work). Gufo 0.4.0 counts
  executed prefill tokens, excluding cache hits; the dashboard subtracts
  `prefill_tokens`, falling back to `prompt_tokens - cached_tokens` when both
  are known. Older servers count full prompt tokens. The metrics HELP line
  selects the units; changing units starts a new baseline. The estimate stays
  hidden while proxy requests, upstream requests, or stats writes are pending
  and below a small threshold. A baseline waits for both proxy and upstream
  requests to become idle. Cancelled requests and missing stats can skew it.
- Streams whose `stream_options` has the wrong type (or whose body contains
  `NaN`/`Infinity`) are forwarded untouched and get reduced stats.
- If Gufo ever ignores `Accept-Encoding: identity` and compresses, bytes
  still pass through unchanged but stats for that response are skipped.
- A single SSE event larger than 16 MiB stops inspection for that stream
  (bytes still pass through).

## Streaming progress and cache diagnostics

`return_progress: true` passes through to Gufo for streaming Chat Completions,
Completions, and Responses. Empty progress deltas and SSE keepalive comments
pass through unchanged and do not start the first-token timer. Streaming
Completions now include terminal `timings`, enabling prefill and draft metrics
even when those values are absent from the usage event.

Gufo 0.4.0 reports cache diagnostics in server logs rather than HTTP metrics.
The optional [host cache observer](deployment.md#cache-pressure-observer) makes
capacity budgets, observed RAM entry-limit evictions, disk LRU evictions, skipped
captures, and recent event timestamps available in the Cache panel. Counts cover
the retained log window, not all-time totals. Rotation or the observer’s tail
limit can shorten that window. Live occupied-slot counts are unavailable; RAM
byte-limit removals are deliberately suppressed by Gufo and cannot be counted.
Capacity is a budget, not measured allocation.

The Cache panel separates execution sessions from conversation-cache retention.
In Gufo 0.5.0, the RAM checkpoint limit is 128 independently of `--sessions`;
one conversation can retain multiple checkpoints. The panel reads sessions,
checkpoint limits, and byte budgets from separate observer fields. It shows
retained RAM and disk checkpoint counts as unavailable because Gufo does not
report occupancy. The automatic RAM budget has a 32 GiB ceiling, but the actual
reported budget can be lower due to host and model memory constraints.

The observer reads Docker logs on the host and exports only selected numeric
fields and fixed event types. The dashboard never receives raw logs or a Docker
socket. At `warn`, Gufo hides startup capacity lines and INFO-tier disk LRU events;
at `error`, it also hides RAM entry-pressure warnings. Missing diagnostics remain
null. A stopped observer becomes unavailable after 30 seconds. HTTP statistics
continue to work without the observer. Request-level cache metrics remain in
request details; KV utilization and slot metadata remain placeholders.

When a client requests `return_progress: true`, the In flight card shows the
oldest matching prefill’s cached, processed, and total token counts. Progress is
transient: it leaves the card when generation starts and is never stored in the
request history. The proxy does not enable progress on the client’s behalf.

## Text capture compatibility

Optional text capture uses endpoint-specific allowlists alongside metric
extraction. The supported input/output shapes are:

| Endpoint | Captured input | Captured answer |
| --- | --- | --- |
| `/v1/chat/completions` | Latest user message's string or text blocks | First choice's assistant content; streamed content deltas |
| `/v1/completions` | String `prompt` | First choice's text; streamed text deltas |
| `/v1/responses` | String `input`, latest user input message, or direct text input blocks | Assistant output text; streamed `response.output_text.delta` events |
| `/v1/messages` | Latest user message's string or text blocks | Text content blocks |
| `/completion` | String `prompt` | Response `content` |

The parser also recognizes Messages/native streaming text shapes, but the
checked-in Gufo fixtures reject streaming for those endpoints. Capture support
does not add streaming capabilities to Gufo. Tokenized or batched completion
prompts are outside the initial string-prompt capture scope.

Questions and answers are available after the request ends, including interrupted
streams. Separate reasoning fields, tool payloads, images, and attachments are
excluded. Reasoning token counts can still appear in metrics. Formatted context
already embedded in a plain prompt string is captured as supplied.

Compressed responses and oversized inspection bodies/events can leave a partial
capture. Capture limits affect saved text only. Requests sent directly to Gufo,
old requests recorded without capture, and expired/deleted text cannot be
reconstructed. See [Privacy](privacy.md#optional-text-capture) for retention,
queue limits, and deletion details.
