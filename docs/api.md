# Dashboard API

The dashboard reserves `/api/*`. These endpoints return JSON and have no
built-in authentication. They expose statistics and retained captured text to anyone who can reach the
service. Gufo's `/health`, `/ready`, and `/v1/*` routes are separate proxied routes.
Interactive OpenAPI pages are disabled.

## Routes

| Method | Path | Parameters and result |
| --- | --- | --- |
| GET | `/api/health` | `{"status":"ok"}`; dashboard liveness only |
| GET | `/api/status` | Dashboard package `version`, upstream online state, model, observed ready duration, gauges, in-flight requests, pipeline counters, content-capture settings, and unattributed-token estimate |
| GET | `/api/models` | `models` array and `current` model |
| GET | `/api/summary` | `range`, `model`; counts and per-model metrics |
| GET | `/api/timeseries` | `range`, `model`, `bucket`; timestamps, totals, and per-model series |
| GET | `/api/activity` | `limit`, `before` or `after`, `model`, `filter`; merged request/event feed |
| GET | `/api/requests/{id}` | Stored request metadata; `404` if missing, pruned, or cleared |
| GET | `/api/requests/{id}/content` | On-demand text, status, truncation flags; `Cache-Control: no-store` |
| DELETE | `/api/requests/{id}/content` | Delete captured text while preserving request statistics |
| POST | `/api/content/clear` | Body `{"confirm":"clear"}`; clear captured text while preserving statistics |
| GET | `/api/speculative` | `range`, `model`; per-model draft totals and acceptance |
| GET | `/api/cache` | `range`, `model`; cache statistics and miss reasons |
| GET | `/api/events` | `limit`; recent lifecycle events in an `events` array |
| POST | `/api/stats/clear` | JSON body `{"confirm":"clear"}`; deletes history and returns `{"status":"cleared"}` |

## Parameters

- `range`: `1h`, `24h` (default), `7d`, `30d`, or `all`.
- `model`: optional exact identifier, up to 128 characters. Omitting it gives
  counts across models and separate performance metrics for each model.
- `bucket`: `auto` (default) or an integer number of milliseconds, at least 1000
  and at most 12 digits. Automatic bucketing targets at most about 120 buckets.
- `limit`: 1–500; default 100 for activity and 50 for events.
- `filter`: `all`, `errors`, `vision`, `streaming`, `cancelled`, `content`, or
  `partial`. `content` selects requests with retained question or answer text;
  `partial` selects those with partial or truncated text. Neither includes text
  previews. Filters apply to request rows; lifecycle events remain in the feed.

`/api/status` reports the installed package version. Source-only runs without
package metadata report `unknown (source checkout)`.

Times are UTC Unix epoch milliseconds. Rates use tokens per second. Ratios such
as cache hit rate and draft acceptance are fractions, not percentage strings.
Missing measurements are `null`; zero is a measured value.

For `range=all`, rollups preserve counts, totals, and weighted metrics after
retention. Percentiles and detailed breakdowns depend on retained request rows.
Timeseries may fall back to daily rollups; inspect `source` and `bucket_ms` rather
than assuming the requested bucket was used.

## Examples

```bash
curl http://localhost:8081/api/health
curl 'http://localhost:8081/api/summary?range=24h'
curl 'http://localhost:8081/api/activity?limit=25&filter=errors'
```

To filter a model without manually URL-encoding its name:

```bash
curl --get http://localhost:8081/api/summary \
  --data-urlencode 'range=7d' \
  --data-urlencode 'model=your-served-model'
```

## Activity pagination

Each activity item has `type` (`request` or `event`) and a `cursor` of the form
`timestamp:type:id`. The response contains `items` and `has_more`.

For older items, pass the last item's cursor as `before`. For incremental
polling, pass the newest seen cursor as `after`. Responses remain newest-first;
when a newer-page backlog exceeds the limit, the oldest unseen items are returned
first so repeated polling can drain the backlog without skipping entries.
Advance to the newest cursor in each returned page and repeat while `has_more`
is true. Empty pages do not advance the cursor.

Providing both `before` and `after` returns `400`. Query validation errors return
`422`. Missing request details return `404`. Upstream failures belong to the
proxy, where connection failures return `502`.

## Captured text

`GET /api/status` includes the active configuration:

```json
{"content_capture":{"enabled":true,"retention_days":7,"max_bytes":65536}}
```

`GET /api/requests/{id}/content` loads text separately from request metadata.
For example, a completed capture returns:

```json
{
  "request_id": 42,
  "captured_at_ms": 1790800000000,
  "status": "complete",
  "question": "Why is my container offline?",
  "answer": "Check its network and listening port.",
  "question_truncated": 0,
  "answer_truncated": 0
}
```

`question` and `answer` are nullable strings. The truncation flags are SQLite
integer flags (`0` or `1`). `captured_at_ms` is the request completion timestamp.
Content becomes available when the proxy records the completed or interrupted
request. Captures exclude images, attachments, tool payloads, and separate
reasoning fields.

| Status | Meaning |
| --- | --- |
| `complete` | The capture reached its terminal response marker; truncation flags may still be set |
| `partial` | The request failed, was interrupted, or inspection could not finish reliably |
| `unsupported` | No supported user or answer text was found in a completed response |
| `disabled` | Capture was off for this request |
| `unavailable` | The retained request has no content record, such as a request from before this feature |
| `deleted` | Text was cleared, including suppression of capture for a request already in flight during bulk clearing |
| `expired` | The configured content retention window elapsed |
| `dropped` | The queued-text budget was full; metrics may still be retained |

For `unavailable`, the response contains only `status`, `question`, and `answer`.
A missing, pruned, or cleared parent request returns `404`. Content reads and
successful deletion responses use `Cache-Control: no-store`. Complete describes
capture completion, not answer quality. Expired text is hidden immediately;
startup/hourly pruning removes the stored text. Status markers can remain until
the parent request is pruned.

```bash
curl 'http://localhost:8081/api/activity?limit=25&filter=content'
curl 'http://localhost:8081/api/activity?limit=25&filter=partial'
curl http://localhost:8081/api/requests/42/content
```

To delete one request's captured text while keeping its statistics:

```bash
curl -X DELETE http://localhost:8081/api/requests/42/content
```

The result is `{"status":"deleted"}`; a missing parent request returns `404`.
To clear all captured text while keeping statistics:

```bash
curl -X POST http://localhost:8081/api/content/clear \
  -H 'Content-Type: application/json' \
  -d '{"confirm":"clear"}'
```

The result is `{"status":"cleared"}`. An invalid confirmation body returns
`400`. Bulk clearing suppresses content from requests already in flight; new
requests can add content afterward. Clear confirmation expresses intent and
provides no authentication. Deletion is logical; backups may retain old copies.

## Clearing history

`POST /api/stats/clear` is destructive. It clears request rows, rollups, snapshots,
events, and captured content, then records `stats_cleared` and resets the polling
baseline. The confirmation body is an intent check, not authentication. Requests already in
flight can add new metadata records after the clear operation; their text is
suppressed. New requests can add both metadata and text afterward.

The API is part of the current `0.1.x` application and has no versioned stability
contract. Response implementations live in `app/api.py` and `app/stats.py`.

## Gufo 0.4.0 status fields

`GET /api/status` adds `upstream_requests.processing` and
`upstream_requests.deferred`. These are Gufo-wide admitted and queued request
counts, including traffic that bypasses the proxy. Missing gauges and offline
state return null. `in_flight` still counts only dashboard-proxied requests.

`prompt_counter_excludes_cached` identifies the units of `counters.prompt`
and the unattributed prompt estimate. It is true when Gufo’s metrics HELP line
announces that cache hits are excluded. The dashboard then subtracts recorded
prefill work rather than full prompt totals. `recorded_prompt_tokens` in the
unattributed response uses those same units. Gufo 0.4.0’s token counters update
during generation; its speed gauges retain the latest nonzero request rates.


`capabilities.live_requests` reports whether both native request gauges were
observed. Older servers return false and null request counts. Internally and in
the API, `deferred` means requests waiting for a Gufo session; it is separate
from the proxy’s in-flight count and statistics-writer queue.

`cache_pressure` contains the optional host observer’s snapshot: `available`,
`status`, `gufo_version` (runtime image version), `log_level`, capacity budgets,
`sessions` (execution sessions), `snapshot_entry_limit` (RAM checkpoint limit),
observed eviction/skip counts, a bounded `events` list,
and observation/window timestamps. `capabilities.cache_pressure` is true only
for a fresh successful snapshot containing recognized RAM or disk diagnostics.
A version string alone never enables a feature. Missing, stale, unknown, or
unconfigured features are unavailable rather than zero. Event rows contain only
`ts_ms`, `tier`, `action`, `reason`, and optional numeric `bytes`.

`in_flight.items` adds `phase` (`waiting`, `prefill`, `generating`) and optional
`prompt_progress` (`cache`, `processed`, `total`). Only validated numeric progress
from proxied SSE streams is exposed. Progress is absent after the first generated
token and disappears with the completed request.
