# Gufo 0.10.0 captures

Captured from the loaded Gufo 0.10.0 server on October 9, 2026. The engine
commit and capture timestamp are in `gufo_build.json`; version and commit were
verified with the runtime image labels. Bodies use controlled prompts; model
names, response IDs, and creation timestamps are anonymized. Timing and usage
measurements remain intact. Response headers and request metadata are omitted.

The main capture uses the same sequence as `scripts/capture_fixtures.py`.
Between `metrics_before` and `metrics_after`, counters increase by 98 executed
prefill tokens, 98 cached prompt tokens, and 25 generated tokens over the ten
recorded requests; executed (98) plus cached (98) equals the 196 full prompt
tokens. The later Completions/native captures and both `messages_tools_*`
captures fall outside this counter window.

0.10.0 additions visible in these captures:

- `/v1/messages` streams: `message_start` (zero usage), `content_block_*`
  events (`text_delta`, `thinking_delta`, `input_json_delta`), then
  `message_delta` with the full `usage` and `stop_reason`, then
  `message_stop`. The `messages_stream` capture is a full-prompt cache hit
  (14 of 14 tokens cached). On Messages, `usage.input_tokens` includes cached
  tokens, matching chat `prompt_tokens`; upstream notes this diverges from
  Anthropic, which counts only uncached input.
- `/v1/messages` accepts `tools` and `tool_choice` (forced
  `{type: "tool", ...}` in the captures): the response carries a `tool_use`
  content block with parsed arguments (`get_weather`, `{"city": "Rome"}`) and
  `stop_reason: "tool_use"`. The streamed variant emits the arguments as one
  `input_json_delta` and is a full-prompt cache hit (292 of 292). Tool
  arguments are never captured as content.
- Consumed `/metrics` series and HELP-line units are unchanged from 0.7.0;
  `kv_cache_usage_ratio` remains ignored.

Native `/completion` streaming still returns 400; the older streamed-Messages
400 is gone (the parent fixtures retain it). Cache eviction and retained
capacity are log diagnostics, absent from these response metrics.
