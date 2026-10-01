# Contributing

Bug reports, documentation corrections, and focused pull requests are welcome.
Read the [architecture](docs/architecture.md) and [privacy rules](docs/privacy.md)
before changing proxying, extraction, or logging behavior.

## Development setup

Requires Python 3.12 or later and `uv`:

```bash
uv sync --locked --extra dev
DASHBOARD_HOST=127.0.0.1 uv run --locked python -m app.main
```

A running Gufo server is needed to use the dashboard. Tests use a local fake
server with real HTTP connections, so they need permission to bind loopback
ports but no model, GPU, API key, or external server.

## Checks

```bash
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy app
uv build
```

Run these before opening a pull request. CI checks Python 3.12, 3.13, and 3.14 and builds
the Docker image. There is no JavaScript build pipeline; inspect frontend changes
in a browser at desktop and narrow widths, including keyboard navigation.

## Change guidelines

Keep pull requests focused and describe the user-visible result and validation.
Tests should cover behavior that could regress, especially stream forwarding,
usage-event filtering, disconnects, statistics accuracy, and privacy.

- Keep client-facing response bytes intact except for the documented usage-event
  filtering. Do not inject the polling API key into client requests.
- Treat upstream fields as optional and untrusted. Missing measurements remain
  null; do not turn them into zero.
- Keep performance metrics separate for each model.
- Keep capture off by default. Store only approved metadata in statistics rows;
  optional user/answer text belongs in `request_content` through `content.py`.
  Preserve the latest-user scope, byte limits, retention, and deletion behavior.
- Exclude images, attachments, tool payloads, separate reasoning fields, and
  credentials from capture. Never log prompts, answers, headers, URLs, or
  exception payloads. Render captured text as plain text and fetch it on demand.
- Preserve the default-mode canary checks in `tests/test_privacy.py`. For capture
  changes, run `tests/test_content.py` too; it covers text allowlists, forwarding,
  truncation, deletion, retention, migration, and clearing during an active stream.
- Update documentation when settings, API behavior, or compatibility changes.

## Dependencies

`pyproject.toml` declares supported dependencies. `uv.lock` resolves development
and runtime versions. `requirements.txt` exports runtime pins for pip and Docker.
After changing dependencies:

```bash
uv lock
uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file requirements.txt
uv sync --locked --extra dev
```

Weekly Dependabot PRs update GitHub Actions and the `uv` dependency manifest and
lockfile, including the development audit tool. Dependabot excludes the generated
`requirements.txt` export to avoid independent, incompatible pins. Regenerate it
with the command above on Python dependency PRs before merging; CI rejects a
stale export. GitHub Actions remain pinned to release commit SHAs.

For a deliberate full dependency upgrade, use `uv lock --upgrade`, regenerate
the export, and run the checks. Do not edit generated pins or hashes by hand.

## Gufo fixtures

Existing fixtures were captured with small controlled prompts. They contain raw
request metadata and generated responses, unlike the application's stored stats.
See the [fixture reference](tests/fixtures/gufo/README.md).

To capture against a local test server:

```bash
GUFO_BASE_URL=http://127.0.0.1:8080 uv run --locked python scripts/capture_fixtures.py \
  --gufo-version YOUR_GUFO_VERSION --gufo-commit YOUR_GUFO_COMMIT
```

`--gufo-version` is required; `--gufo-commit` is optional. The script saves both
with a UTC capture timestamp in `gufo_build.json`. Supply the build identity from
the server or its source checkout; it cannot be inferred from model names.

This sends 15 inference requests, including deliberately invalid requests, and
overwrites the fixture files. It requires an upstream that permits these requests
without authentication. Use a disposable test session. Review bodies, headers,
model names, timestamps, and generated text before committing. Update the capture
notes and metric-delta expectations when the results change.

## Reports and pull requests

Use a bug report for reproducible failures and a feature request for proposed
behavior. Include versions, the affected endpoint, expected and actual behavior,
and a small sanitized reproduction. Do not attach `.env`, databases, real prompts,
or private logs. Use [Security](SECURITY.md) for vulnerabilities.
