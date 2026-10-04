# Gufo fixtures

Responses derived from controlled Gufo captures. Published fixtures use generic
model names, response IDs, request IDs, and timestamps. Original timing and usage
measurements remain to exercise extraction and aggregation. Regenerate with
`scripts/capture_fixtures.py`, then sanitize identifiers before committing.

Each `<name>.meta.json` holds the request body, status and response headers;
the body is in `<name>.json`, `<name>.sse` or `<name>.txt`, in the captured response format, with identifying metadata replaced. All requests use `max_tokens: 16`, so every `finish_reason` is
`length` — there is no natural-`stop` sample.

| Fixture | What it shows |
| --- | --- |
| `chat_nonstream` | Full `usage` + `usage.gufo` + `timings`; cache hit from an earlier run (`prefill_tokens: 0`) |
| `chat_nonstream_cachehit` | Same prompt again: `cache_hit: true`, `prefill_tokens: 0`, `prefill_ms: 0`, no `cache_miss_reason` |
| `chat_stream_no_usage` | Finish chunk carries `timings` only (no `usage`, no `usage.gufo`) |
| `chat_stream_include_usage` | Extra `choices: []` usage event before `[DONE]`; other chunks unchanged |
| `chat_vision_nonstream` | 16×16 PNG `image_url`; cache miss (`input_changed`); Gufo gives no vision flag, only a larger `prompt_tokens` |
| `completions_stream_include_usage` | `usage` without `gufo` block; no `timings` anywhere |
| `responses_nonstream` / `responses_stream` | Responses-API usage (`input_tokens`, `output_tokens_details.reasoning_tokens`) + `timings`, no `gufo` block; terminal event here is `response.incomplete` |
| `messages_nonstream` | Anthropic-style usage (`cache_read_input_tokens`) + `timings`, no `gufo` block |
| `messages_stream`, `native_completion_stream` | 400 `'stream' must be false` — Gufo does not stream these endpoints |
| `error_bad_json`, `error_unknown_model` | Error envelope `{"error": {message, type, code}}` |
| `metrics_before` / `metrics_after` | Counter deltas (406 prompt / 144 predicted) equal the sum of `usage` over the requests between them, cached tokens included |
| `health`, `ready`, `models` | Status endpoints; `/v1/models` has `context_length` |

Added later (captured after `metrics_after`, so not part of the 406 / 144 delta):

| Fixture | What it shows |
| --- | --- |
| `completions_nonstream` | `/v1/completions` non-stream: `usage` (no `gufo`) + `timings` |
| `native_completion_nonstream` | `/completion` non-stream: `usage` (no `gufo`) + `timings` + llama.cpp `stopped_*` flags, `tokens_evaluated` / `tokens_predicted` / `tokens_cached` |

## Recapturing and publication

The capture script sends 15 inference requests, including invalid requests, and
overwrites these files. Unlike the dashboard's runtime database, fixtures contain
raw generated content and request metadata. Use controlled prompts on a test
server and review every file before committing. The image data URI is replaced
with a placeholder in metadata; response content is not automatically redacted.

Cache state, timings, and counter totals vary between captures. Update these
notes and the tests' expected deltas when recapturing. No Gufo commit identifier
was recorded for the current samples, so compatibility claims are limited to
this captured behavior.


Future captures require `--gufo-version VERSION` and optionally accept
`--gufo-commit COMMIT`. The script writes `gufo_build.json` with this
operator-supplied identity and a UTC capture timestamp. Preserve that file when
publishing sanitized fixtures. The current samples predate this metadata;
recapturing is required to establish their replacement's build provenance.

## Gufo 0.4.0

The [versioned captures](v0.4.0/README.md) add verified build provenance,
prefill-only live counters, Completions terminal timings, and prompt-progress
streams. The parent fixtures retain the older full-prompt counter behavior.

## Gufo 0.7.0

The [versioned captures](v0.7.0/README.md) add the cached-prompt counter and
new `llamacpp:*` series, model input modalities, prefix-sharing usage keys,
`timings.draft_rounds`, and Messages thinking blocks, with unchanged consumed
series and HELP-line units.
