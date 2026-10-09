# Gufo Dashboard

A local statistics dashboard and HTTP, SSE, and WebSocket proxy for Gufo
inference servers. Route clients through the dashboard to track token usage,
throughput, latency, cache hits, and speculative decoding over time.

![Gufo Dashboard showing live metrics, throughput charts, request activity, and individual timings](docs/images/dashboard-demo.jpg)

*Real measurements from 12 controlled demo requests. Model and upstream request
identifiers are anonymized; these figures illustrate the interface, not a benchmark.*

## What it does

- Tracks requests across Chat Completions, Completions, Responses, Messages, and
  Gufo's native `/completion` endpoint.
- Proxies HTTP responses, SSE streams, and bidirectional WebSocket messages
  (including text and binary audio).
- Shows live in-flight requests, recent activity, errors, and individual timings.
- Optionally captures questions and answers in the selected request’s Content view,
  with separate retention and deletion controls.
- Keeps throughput, time to first token, cache hits, and draft acceptance separate
  for each model.
- Optionally shows snapshot-cache budgets and observed eviction events through
  a [host cache observer](docs/deployment.md#cache-pressure-observer).
- Stores statistics in SQLite with configurable retention and daily rollups.
- Runs with Python or Docker. The frontend uses plain HTML, CSS, JavaScript, and
  a bundled copy of Chart.js; there is no frontend build step or CDN dependency.

The application stores numeric metrics and validated identifiers. Optional text
capture adds the latest user message and visible answer for auditing. It is off
by default; images, attachments, tool payloads, separate reasoning fields, and
client credentials are excluded.
Read the [privacy reference](docs/privacy.md) for the exact boundaries.

**Deployment scope:** trusted local machines and private networks. There is no
built-in authentication or TLS. Anyone who can reach the service can query its
statistics and captured text, clear history, and access Gufo through the proxy.
The default bind address is `0.0.0.0`; use `DASHBOARD_HOST=127.0.0.1` for
local-only access.

## Quick start

You need a running Gufo server. Its default upstream address is
`http://127.0.0.1:8080`. The dashboard uses port `8081`. Gufo 0.5.0 or later
is recommended and 0.10.0 is the verified version; 0.4.0 is flagged "DO NOT
USE" upstream (tool-calling regression, fixed in 0.5.0).

### Docker Compose on Linux

From the project directory:

```bash
cp .env.example .env
mkdir -p data
APP_UID="$(id -u)" APP_GID="$(id -g)" docker compose up -d --build
```

Open [http://localhost:8081](http://localhost:8081).
The Compose service uses host networking to reach Gufo on the same Linux host.
The `data` directory must be writable by the container's configured UID/GID.
See [deployment](docs/deployment.md) for bridge networking, configuration, and
backups.

### Python

Requires Python 3.12 or later and `uv`:

```bash
uv sync --locked
DASHBOARD_HOST=127.0.0.1 uv run --locked python -m app.main
```

Local runs read the process environment; they do **not** automatically load
`.env`. The default database is `./data/gufo-dashboard.sqlite`. Docker Compose
loads `.env`, whose example uses `/data/gufo-dashboard.sqlite` inside the container.
A [pip setup](docs/deployment.md#python-with-pip) is also available.

## Connect your clients

Change the client's base URL from port `8080` to `8081`; keep its existing model
and credentials. For OpenAI-compatible clients, the new base URL is:

```text
http://localhost:8081/v1
```

Use the dashboard host's private address when connecting from another machine.
Requests sent directly to Gufo on port `8080` are not recorded individually.

The proxy forwards client authorization and response bytes. For streamed Chat
Completions and Completions, it requests usage statistics when needed and removes
the added usage event before returning the stream. See the
[forwarding rules](docs/architecture.md#forwarding-rules) for header handling and
other exceptions.

WebSocket clients can use `ws://localhost:8081` instead of `ws://localhost:8080`
with the same path, query, credentials, and requested subprotocols. When the
configured Gufo URL uses HTTPS, the upstream WebSocket connection uses WSS.
Text and binary messages are relayed without inspecting their contents.
WebSocket sessions do **not** appear in Activity or request history, and their
messages are never captured, even when `CAPTURE_CONTENT=true`. HTTP/SSE usage
statistics still work as described above.

WebSocket message boundaries are preserved, but wire-level fragmentation and
compression are negotiated independently on each side. The dashboard's
Uvicorn server defaults to a 16 MiB inbound WebSocket message limit; larger
client messages require changing the server's WebSocket configuration.

## Optional question-and-answer audit

![Optional content capture showing a demo question and generated answer](docs/images/content-demo.jpg)

*The Content view shows a controlled demo prompt and answer. Text capture is off
by default.*

Text capture is off by default. To enable it with Docker Compose, edit **`.env`
beside `docker-compose.yml`**:

```dotenv
CAPTURE_CONTENT=true
CONTENT_RETENTION_DAYS=7
CONTENT_MAX_BYTES=65536
```

The existing Compose service loads `.env` through `env_file`; no Compose YAML
change is needed. Apply the settings from the project directory:

```bash
docker compose up -d --build --force-recreate
```

A container restart alone does not reload its environment. For Python, pass the
setting in the process environment instead:

```bash
DASHBOARD_HOST=127.0.0.1 CAPTURE_CONTENT=true uv run --locked python -m app.main
```

| Flag | Default | Purpose |
| --- | --- | --- |
| `CAPTURE_CONTENT` | `false` | Enable capture for future requests routed through the dashboard |
| `CONTENT_RETENTION_DAYS` | `7` | Keep captured text for this many days |
| `CONTENT_MAX_BYTES` | `65536` | Limit each captured question and answer to this many UTF-8 bytes |

Select an **Activity** request and open **Content** to view the latest user
message and generated answer. Each selection starts on Metrics; text appears
only when you open Content. **With content** filters requests with retained text;
**Incomplete** filters partial or truncated captures. Lifecycle events remain
visible in both filters. Content becomes available after the request ends,
including when a streamed answer is interrupted.

Capture stores the latest user message and visible answer (first completion
choice). It excludes images, attachments, tool payloads, separate reasoning
fields, and system/developer messages. Plain completion prompts are captured as
supplied; any context already flattened into that string is inseparable. Earlier
requests cannot be recovered, and traffic sent directly to Gufo is not captured.

Text retention is separate from the default 90-day statistics retention. Partial
answers and truncated text are labelled. **Delete this content** removes one
request's text; **Clear captured content** removes all saved text while keeping
metrics. **Clear stats** deletes both text and statistics. Setting
`CAPTURE_CONTENT=false` and recreating the container stops new collection;
previously saved text remains until deleted or expired.

Anyone who can reach the dashboard can read or delete captured text. Keep access
and database backups private. See [deployment](docs/deployment.md#text-capture-configuration)
for settings and troubleshooting, and [privacy](docs/privacy.md#optional-text-capture)
for storage and deletion limits.

## Documentation

| Reference | Contents |
| --- | --- |
| [Deployment and configuration](docs/deployment.md) | Docker, Python, environment variables, backups, troubleshooting |
| [Architecture](docs/architecture.md) | Routing, forwarding, extraction, SQLite, lifecycle |
| [Metrics and compatibility](docs/metrics.md) | Endpoint coverage, calculations, missing values, limitations |
| [Dashboard API](docs/api.md) | Routes, parameters, pagination, response conventions |
| [Privacy](docs/privacy.md) | Stored fields, excluded content, retention, deletion |
| [Contributing](CONTRIBUTING.md) | Development setup, checks, fixture updates |
| [Security](SECURITY.md) | Deployment boundaries and vulnerability reporting |
| [GitHub readiness audit](docs/github-readiness.md) | Findings, fixes, validation, publication checklist |

## Development

```bash
uv sync --locked --extra dev
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy app
```

Tests use a local fake Gufo server and checked-in fixtures; a live inference
server or API key is not required. CI runs these checks on Python 3.12 and 3.13,
checks the exported dependency pins, and builds the Docker image.

## License

[MIT](LICENSE). The bundled Chart.js distribution has its own
[MIT license notice](app/static/vendor/LICENSE.chartjs.md).
