# Metrics and compatibility

Endpoint behavior below describes the checked-in, anonymized Gufo fixtures.
It is not a guarantee for every Gufo release. See the
[fixture notes](../tests/fixtures/gufo/README.md) for capture details.

| Endpoint | Streams | Where the stats are | `usage.gufo` (TTFT, queue, cache, scheduler) |
| --- | --- | --- | --- |
| `/v1/chat/completions` | yes | non-stream: `usage` + `timings`; stream: finish chunk has `timings`; the `include_usage` event has full `usage` + `timings` | **yes** (non-stream, or stream with usage) |
| `/v1/completions` | yes | `usage` (+ `timings` non-stream); stream: `include_usage` event, no `timings` | no |
| `/v1/responses` | yes | `usage` (incl. `reasoning_tokens`) + `timings` in the body or the `response.completed/incomplete/failed` event | no |
| `/v1/messages` | **no** (`'stream' must be false`) | Anthropic `usage` (`input_tokens`, `output_tokens`, `cache_read_input_tokens`) + `timings` | no |
| `/completion` | **no** | `usage` + `timings` + llama.cpp `stopped_*` flags | no |

Also polled: `/ready` (online + model), `/v1/models` (`context_length`),
`/metrics` (`llamacpp:prompt_tokens_total`, `tokens_predicted_total`, and the
last request's prefill/decode rate gauges).

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
- Traffic sent to `:8080` directly shows up only as **unattributed tokens**
  (a reset-aware `/metrics` delta minus recorded rows) — an estimate, hidden
  while requests are in flight and below a small threshold.
- Streams whose `stream_options` has the wrong type (or whose body contains
  `NaN`/`Infinity`) are forwarded untouched and get reduced stats.
- If Gufo ever ignores `Accept-Encoding: identity` and compresses, bytes
  still pass through unchanged but stats for that response are skipped.
- A single SSE event larger than 16 MiB stops inspection for that stream
  (bytes still pass through).

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
