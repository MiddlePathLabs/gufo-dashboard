# Privacy and data retention

## Stored data

Table `request_stats` contains one row per recorded request:

```
id, started_at_ms, completed_at_ms, endpoint, model, gufo_request_id,
is_streaming, is_vision, image_count, http_status, error_code, finish_reason,
prompt_tokens, cached_tokens, prefill_tokens, completion_tokens, reasoning_tokens,
prompt_tokens_per_second, completion_tokens_per_second, prefill_ms, decode_ms,
ttft_ms, ttft_source, queue_ms, mean_inter_token_ms, max_inter_token_ms,
prefill_chunks, cache_hit, cache_miss_reason, cache_common_prefix_tokens,
cache_restore_ms, cache_restore_bytes, draft_tokens, draft_tokens_accepted,
queue_depth_at_submit, client_queue_depth_at_submit, resident_requests_at_admission,
requested_logical_concurrency, physical_execution_width, execution_plan,
context_length, context_used_pct, proxy_ttfb_ms, proxy_first_token_ms,
total_request_ms, extra_metrics
```

plus `events` (`gufo_up`, `gufo_down`, `model_changed`, `counter_reset`,
`stats_cleared` with a short machine detail), `metrics_snapshots` (Gufo
`/metrics` totals and gauges) and `daily_rollup` (per-day, per-model sums).

String columns (`model`, `execution_plan`, `cache_miss_reason`,
`finish_reason`, `error_code`, `gufo_request_id`) are kept only if they are
short and match `^[A-Za-z0-9_.:/-]+$`. `extra_metrics` holds only numeric or
boolean `usage.gufo` fields with snake_case keys (max 64).

## Excluded content

With content capture off (the default), the application does not intentionally
store or log request/response bodies, prompts, or outputs. In either mode, it
excludes separate reasoning fields, images or image URLs (only an image *count*),
attachments, tool definitions, arguments or results, `Authorization` / API keys / cookies, client IPs,
query strings, upstream error messages (only `error.code`), `/slots` content.
Uvicorn's access log is off; the app logs method, route, status, duration and
Gufo request ID only. The default-mode canary test (`tests/test_privacy.py`)
checks the SQLite main/WAL/SHM files and all captured logs.

## Privacy boundaries

Statistics are metadata storage, not anonymization. Opt-in text capture also
stores personal message content. Model names, request IDs, timestamps,
error codes, and numeric usage are retained. An identifier that matches the
validation rules can still contain sensitive information. Avoid placing secrets
or user content in model names, request IDs, or other metadata fields.

The proxy must handle complete request and response content in memory to forward
it and extract statistics. Gufo, clients, reverse proxies, operating-system dumps,
and external logging tools have their own storage policies.

`tests/fixtures/gufo/` is a deliberate exception to runtime content retention:
it contains raw responses and request metadata from controlled test prompts.
The capture script writes generated text to disk. Review every recapture before
committing it. Screenshots may also reveal model names and activity timings.

## Retention and deletion

`RETENTION_DAYS` defaults to 90. Daily aggregate totals survive raw-row pruning.
Clear stats deletes captured text, request rows, rollups, snapshots, and events,
then adds a
`stats_cleared` event. It does not securely erase SQLite pages, WAL files, backups,
or filesystem snapshots. Stop the application before removing the database files
when starting with a completely fresh dataset.


## Optional text capture

Enable capture with `CAPTURE_CONTENT=true` in the Compose `.env` file or Python
process environment; see [configuration instructions](deployment.md#text-capture-configuration).
This enables a `request_content` table linked to request IDs.
It stores the latest user text and visible assistant answer, capture status,
capture timestamp, and truncation flags. System/developer messages, earlier chat
turns, tool definitions/arguments/results, separate reasoning fields, image URLs,
image bytes, and attachments are not captured. Plain `/v1/completions` and native
`/completion` prompt strings are stored as supplied; context already flattened
into that string cannot be separated. Text pasted into a message may contain
secrets; no automatic secret masking is performed.

Streaming text is assembled alongside forwarding. Cancelled, interrupted,
unparseable, or unsupported captures are labelled; successful captures are
labelled complete. Complete describes capture completion, not answer quality.
Each side is bounded by `CONTENT_MAX_BYTES` (default 64 KiB, maximum 1 MiB).
Queued text has an additional 8 MiB budget; excess content is dropped with a
status marker while its metrics can still be recorded. The writer queue can also
drop an entire request record under load. This is a troubleshooting history,
not a guaranteed or tamper-proof audit trail.

Content retention defaults to seven days. Startup/hourly pruning removes expired
text, while APIs hide it immediately after expiration. Status markers remain
until request metadata is pruned. Pruning a parent request removes its content
even if the separate content retention window is longer. Single-request and bulk content deletion keep
statistics; Clear stats deletes content too. Bulk clears suppress content from
requests that were already in flight when the clear began. New requests may add
content afterward. Turning capture off does not delete previous captures.

Content is fetched on demand with `Cache-Control: no-store` and rendered as plain
text. No transcript is saved in browser storage. Everyone with dashboard network
access can read it; UI concealment is not access control. Keep the database and
its backups private. Deletion is logical, not secure erasure of SQLite pages,
WAL files, disk blocks, backups, or snapshots. Screenshots and copied text also
create independent copies. No account system or compliance workflow is needed
for a single-user homelab, but other users should know when capture is enabled.
