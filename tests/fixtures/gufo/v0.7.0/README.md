# Gufo 0.7.0 captures

Captured from the loaded Gufo 0.7.0 server on October 4, 2026. The engine
commit and capture timestamp are in `gufo_build.json`; version and commit were
verified with the runtime image labels. Bodies use controlled prompts; model
names, response IDs, and creation timestamps are anonymized. Timing and usage
measurements remain intact. Response headers and request metadata are omitted.

The main capture uses the same sequence as `scripts/capture_fixtures.py`.
Between `metrics_before` and `metrics_after`, counters increase by 94 executed
prefill tokens and 144 generated tokens over the nine recorded requests. The
new `llamacpp:prompt_tokens_cached_total` counter rises by 72 in the same
window; executed (94) plus cached (72) equals the 166 full prompt tokens. Later
non-streaming Completions/native captures and the three `*_stream_progress`
captures fall outside this counter window. Request totals are now also recorded
for finished or cancelled requests, so deltas include every completed stream.

0.7.0 additions visible in these captures:

- `/metrics` adds `gufo_device_lost_total`, `prompt_tokens_cached_total`,
  `prompt_seconds_total`, `tokens_predicted_seconds_total`, `n_tokens_max`,
  and the three `spec_decode_num_*` counters. The six series the dashboard
  consumes are unchanged, and the `prompt_tokens_total` HELP line still
  announces "excluding cache hits". `kv_cache_usage_ratio` is now real but
  remains deliberately ignored.
- `/v1/models` gains `architecture.input_modalities` (`["text","image"]` for
  the vision model).
- `usage.gufo` carries in-flight prefix-sharing keys (`shared_prefix_wait_ms`)
  and speculative verification rounds (`draft_rounds`); `timings` also carries
  `draft_rounds` on endpoints without a `usage.gufo` block.
- `/v1/messages` returns thinking as its own content block; with
  `max_tokens: 16` the answer is thinking-only, and captured text stays empty.

The three `*_stream_progress` captures exercise `return_progress: true` for
Chat, Completions, and Responses. These short captures received no server
keepalive; the regression test adds one explicitly.

A streaming-generation-failure capture was not reproducible: prompts beyond
the context window are refused with a pre-stream 400
(`context_length_exceeded`), and idle GPU loss is not triggerable in the
harness. Error-envelope codes remain covered by `error_bad_json`,
`error_unknown_model`, and the messages/native streaming refusals.

Messages and native completion streaming still return 400. Cache eviction and
retained capacity are log diagnostics, absent from these response metrics.
