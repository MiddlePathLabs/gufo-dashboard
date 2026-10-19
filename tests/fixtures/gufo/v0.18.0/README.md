# Gufo 0.18.0 captures

Captured from the loaded Gufo 0.18.0 server on October 19, 2026 (qwen3.8
flash text+image model). The engine commit and capture timestamp are in
`gufo_build.json`; version and commit were verified with the runtime image
labels. Bodies use controlled prompts; model names, response IDs, and
creation timestamps are anonymized. Timing and usage measurements remain
intact. Response headers and request metadata are omitted.

The main capture uses the same sequence as `scripts/capture_fixtures.py`.
Between `metrics_before` and `metrics_after`, counters increase by 154
executed prefill tokens, 294 cached prompt tokens, and 160 generated tokens
over the ten recorded requests; executed (154) plus cached (294) equals the
448 full prompt tokens. The later Completions/native captures and both
`messages_tools_*` captures fall outside this counter window.

0.11.0–0.18.0 changes visible in these captures:

- Messages `usage.input_tokens` excludes cache reads since 0.12.0 (#530):
  both Messages captures are full-prompt cache hits reporting
  `input_tokens: 0` with `cache_read_input_tokens: 42`. The recorder
  restores the full prompt as `input_tokens + cache_read_input_tokens`;
  chat and Responses tokens still include cached tokens (42 with 42 cached).
- `/metrics` adds `gufo_spec_lookup_tokens_total` and
  `gufo_spec_lookup_accepted_tokens_total` (prompt-lookup copies); the
  dashboard does not consume them. Consumed series and HELP-line units are
  unchanged.
- Cache startup logs use the merged `component-cache-v1` format (one
  `event=configured` line covering RAM and disk plus `backing_prepared`).
  These are log diagnostics, absent from response metrics; the cache
  observer parses them since dashboard 0.5.0.
- The forced-tool Messages captures show `stop_reason: "tool_use"` with
  parsed arguments (non-stream, uncached) and a streamed tool call that
  reaches the token cap (`max_tokens`); the streamed replay is a
  full-prompt cache hit whose arguments stream as `input_json_delta`.

Native `/completion` streaming still returns 400. Cache eviction and
retained capacity are log diagnostics, absent from these response metrics.
