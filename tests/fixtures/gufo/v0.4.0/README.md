# Gufo 0.4.0 captures

Captured from the loaded Gufo 0.4.0 server on October 1, 2026. The engine
commit and capture timestamp are in `gufo_build.json`; version and commit were
verified with the binary and container labels. Bodies use controlled prompts;
model names, response IDs, and creation timestamps are anonymized. Timing and
usage measurements remain intact. Response headers and request metadata are
omitted. The older parent fixtures remain unchanged.

The main capture uses the same sequence as `scripts/capture_fixtures.py`.
Between `metrics_before` and `metrics_after`, counters increase by 154 executed
prefill tokens and 144 generated tokens. Full prompt totals are larger because
cached tokens are excluded from the new prompt counter. Later non-streaming
Completions/native captures and the three `*_stream_progress` captures fall
outside this counter window.

Completions terminal chunks now carry `timings`, including prefill and draft
counts. Progress captures exercise `return_progress: true` for Chat,
Completions, and Responses. Progress does not constitute a generated token.
Keepalive comments are added explicitly in the regression test; these short
captures did not last long enough to receive a timed keepalive.

Messages and native completion streaming still return 400. Cache eviction and
retained capacity are log diagnostics, absent from these response metrics.
