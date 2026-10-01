# Changelog

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
