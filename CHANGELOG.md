# Changelog

## 0.3.0 — 2026-10-04

- Verify Gufo 0.7.0 compatibility: the consumed `llamacpp:*` series and
  HELP-line units are unchanged, and versioned 0.7.0 captures extend the
  counter reconciliation to the new cached-prompt counter.
- Parse Gufo 0.7.0's automatic and maximum RAM cache budgets in the cache
  observer (state schema 2; schema-1 snapshots remain readable) and show them
  in the Cache panel, which now explains that an explicit `--cache-ram-bytes`
  may exceed the automatic budget.
- Record speculative verification rounds (`draft_rounds`) from terminal
  `timings` for endpoints without a `usage.gufo` block.
- Recommend Gufo 0.5.0 or later in documentation; 0.4.0 is flagged "DO NOT
  USE" upstream.
- Proxy Gufo WebSocket text and binary messages in both directions, forwarding
  authorization, queries, and subprotocols without capturing message content.
- Handle WebSocket disconnects without noisy errors, preserve Uvicorn lifecycle
  logs without logging WebSocket content, and reject non-HTTP(S) upstream URLs.

## 0.2.1 — 2026-10-02

- Separate execution sessions from conversation-cache limits and budgets in the
  Cache panel. Keep retained checkpoint occupancy explicitly unavailable.
- Add hover explanations for summary cards, chart metrics, activity fields,
  request timings, scheduler counters, cache diagnostics, and extra Gufo metrics.
  Explain formulas, units, coverage limits, and unknown measurements.

## 0.2.0 — 2026-10-01

- Support Gufo 0.4.0 prefill-only token counters while preserving older server
  accounting. Hide unattributed-token estimates while upstream work is pending.
- Show Gufo-wide processing and deferred request counts, with explicit feature
  availability when metrics are missing.
- Add an optional host cache observer and Cache panel diagnostics: capacity
  budgets, observed RAM entry-limit and disk LRU evictions, skipped captures,
  and recent event timestamps. Counts cover retained logs; occupied RAM slots
  and suppressed RAM byte-limit removals remain unavailable.
- Show transient cached / processed / total prefill progress for streams where
  clients request `return_progress: true`.
- Refresh dashboard assets after upgrades and document log-level coverage,
  observer setup, API fields, and privacy boundaries.
- Add versioned Gufo 0.4.0 captures and regression tests for counter accounting,
  streaming progress, cache diagnostics, stale data, and privacy filtering.

## 0.1.0 — 2026-09-30

Initial tagged release of the dashboard, inference proxy, statistics storage,
and optional question-and-answer capture.
