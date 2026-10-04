# Deployment and configuration

## Linux with Docker Compose

The default service uses host networking and reaches Gufo through
`http://127.0.0.1:8080`. Run these commands from the project directory:

```bash
cp .env.example .env
mkdir -p data
APP_UID="$(id -u)" APP_GID="$(id -g)" docker compose up -d --build
```

Create `data` before starting Compose so Docker does not create a root-owned bind
mount. Existing deployments must use a UID/GID that can write their existing
files. The container runs as a non-root user, defaulting to UID/GID 1000.

```bash
docker compose logs --tail=100 -f
docker compose down
```

`down` stops the service; the bind-mounted data remains. The image's health check
uses `DASHBOARD_PORT` and checks `/api/health`. This confirms dashboard
availability, not Gufo readiness; `/api/status` reports upstream state.

## Bridge networking

For hosts where Linux host networking is unsuitable, save this as an alternative
Compose file, such as `compose.bridge.yml`:

```yaml
services:
  gufo-dashboard:
    build:
      context: .
      args:
        UID: ${APP_UID:-1000}
        GID: ${APP_GID:-1000}
    ports:
      - "127.0.0.1:8081:8081"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    env_file: .env
    environment:
      GUFO_BASE_URL: http://host.docker.internal:8080
      DASHBOARD_HOST: 0.0.0.0
      DASHBOARD_PORT: 8081
    volumes:
      - ./data:/data
    restart: unless-stopped
    logging:
      driver: local
      options:
        max-size: "10m"
        max-file: "3"
```

```bash
APP_UID="$(id -u)" APP_GID="$(id -g)" docker compose -f compose.bridge.yml up -d --build
```

The port mapping binds to loopback. To allow trusted LAN clients, bind it to your
host's private address. Gufo must be reachable from the container through the
host gateway; a Linux host service bound only to loopback may not be reachable
this way. A reachable LAN address can also be used as `GUFO_BASE_URL`.

## Python with uv

```bash
uv sync --locked
DASHBOARD_HOST=127.0.0.1 uv run --locked python -m app.main
```

Settings come from the environment. `.env` is not loaded automatically. You can
supply individual variables before the command, or use `uv run --env-file .env`
with a local configuration. Change `DATABASE_PATH` to
`./data/gufo-dashboard.sqlite` before using the Docker example `.env` locally.

## Python with pip

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt
.venv/bin/python -m pip install --no-deps .
DASHBOARD_HOST=127.0.0.1 .venv/bin/python -m app.main
```

`requirements.txt` pins runtime dependencies and includes their hashes. It is
exported from `uv.lock`; dependency updates must regenerate both files. Python
must be 3.12 or later. CI exercises 3.12, 3.13, and 3.14.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `GUFO_BASE_URL` | `http://127.0.0.1:8080` | HTTP(S) upstream base URL, without `/v1`; WebSocket connections derive WS(S) from it |
| `GUFO_API_KEY` | Empty | Bearer token for the dashboard's own status polls only |
| `DATABASE_PATH` | `./data/gufo-dashboard.sqlite` | SQLite path; Docker sets `/data/gufo-dashboard.sqlite` |
| `DASHBOARD_HOST` | `0.0.0.0` | Listen address; use `127.0.0.1` for local-only host-network or Python runs |
| `DASHBOARD_PORT` | `8081` | Listen port, 1–65535 |
| `DASHBOARD_ALLOWED_HOSTS` | `localhost` | Comma-separated exact hostnames accepted in requests; IP literals are always accepted |
| `CAPTURE_CONTENT` | `false` | Opt-in latest-user/visible-answer text capture; restart to change |
| `CONTENT_RETENTION_DAYS` | `7` | Positive text retention window, independent of metrics |
| `CONTENT_MAX_BYTES` | `65536` | UTF-8 bytes per captured side; 1 to 1048576 |
| `RETENTION_DAYS` | `90` | Raw requests, events, and snapshot retention; daily rollups remain |
| `POLL_INTERVAL_SECONDS` | `5` | Delay after each completed status poll cycle |
| `UPSTREAM_CONNECT_TIMEOUT` | `10` | Upstream connection timeout in seconds |
| `UPSTREAM_READ_TIMEOUT` | Empty | No read timeout by default, to allow long prefills |
| `STATS_QUEUE_SIZE` | `10000` | Maximum queued writer jobs; overflow can drop statistics |
| `SHUTDOWN_DRAIN_TIMEOUT` | `5` | Seconds allowed to drain queued writes |
| `UNATTRIBUTED_THRESHOLD` | `64` | Show estimated unattributed tokens only above this threshold |

Ports must be 1–65535. Timeouts, retention, polling intervals, and queue sizes
must be positive; timeouts and polling intervals must also be finite.
`UNATTRIBUTED_THRESHOLD` may be zero but cannot be negative. The content byte
limit must be between 1 and 1048576, inclusive. Invalid settings stop startup
with an error naming the variable. `enable_poller` and the body inspection limit are programmatic `Settings` options, not environment variables.

`GUFO_API_KEY` does not authenticate dashboard users and is not injected into
proxied requests. Each client must send whatever credentials Gufo requires.

## Named hosts and log rotation

Direct IP URLs and `localhost` work by default. If you access the service through
`http://gufo.home:8081`, set `DASHBOARD_ALLOWED_HOSTS=localhost,gufo.home`.
For an authenticated gateway, include its hostname too. Entries are exact
hostnames without schemes, ports, paths, or wildcards. Unlisted named hosts
receive HTTP 400 on both dashboard and proxy routes. This reduces DNS rebinding
exposure; it does not authenticate callers.

The supplied Compose service uses Docker's `local` logging driver with three
10 MB files per container. Recreate the container after changing logging options.
Apply the same logging block to custom Compose deployments, including the bridge
example above, if you want the same limits.

## Text capture configuration

For Docker Compose, put the following flags in `.env` beside `docker-compose.yml`.
The service's `env_file: .env` loads them into the container; editing the YAML is
unnecessary. If `.env` already exists, edit it rather than replacing it with the
example file.

```dotenv
CAPTURE_CONTENT=true
CONTENT_RETENTION_DAYS=7
CONTENT_MAX_BYTES=65536
```

From the project directory, recreate the container to load the changed values:

```bash
docker compose up -d --build --force-recreate
```

If your deployment uses a custom UID/GID, preserve its `APP_UID` and `APP_GID`
values when running that command. `docker compose restart` restarts the existing
container with its existing environment; it does not load changes from `.env`.
For the bridge-network example, use the same `-f compose.bridge.yml` argument you
used to start it.

For a local Python run, set the process environment:

```bash
DASHBOARD_HOST=127.0.0.1 CAPTURE_CONTENT=true CONTENT_RETENTION_DAYS=7 CONTENT_MAX_BYTES=65536 uv run --locked python -m app.main
```

Alternatively, use `uv run --locked --env-file .env python -m app.main` after
setting a local `DATABASE_PATH` and the desired listen address in that file.
Restart the Python process after changing settings.

`CAPTURE_CONTENT` accepts `true`, `1`, or `yes`, case-insensitively, to enable
capture. Other values disable it. Use `true` and `false` in configuration files.
The retention value is a positive integer in days; the size limit is an integer
from 1 to 1048576 bytes per side. Defaults are seven days and 64 KiB. Limits
truncate the saved copy, not the response delivered to the client.

Check the toolbar for the amber **CONTENT CAPTURE ON** pill or inspect `/api/status`:

```bash
curl http://localhost:8081/api/status
```

Its `content_capture` object reports `enabled`, `retention_days`, and `max_bytes`.
Settings apply to future proxied requests; old text is not recoverable. To disable
capture, set `CAPTURE_CONTENT=false` and recreate the container or restart the
Python process. Existing text remains available until expired or deleted.

The content window cannot outlive its parent request metadata: if
`RETENTION_DAYS` is shorter than `CONTENT_RETENTION_DAYS`, pruning the request
also removes its captured text. Keep dashboard access private; the app has no
built-in authentication. See [Privacy](privacy.md) for the text-only scope.

## Backups and upgrades

For a simple consistent backup, stop the dashboard and copy the complete `data`
directory to a private location. Preserve any SQLite WAL files alongside the
main database. Use SQLite's backup API for an online backup; do not copy just the
main file while writes are active. Backups include any captured questions and
answers. Retention and deletion in the running app do not alter older backups.

Before upgrading, make a backup, review dependency and schema changes, then
rebuild the image or sync the locked Python environment. Run one application
process per database. Gufo 0.4.0 is flagged "DO NOT USE" upstream because of a
tool-calling regression fixed in 0.5.0; run Gufo 0.5.0 or later (0.7.0 is the
verified version). On startup, schema version 2 adds the content table to
version-1 databases while preserving existing statistics. No previous transcript
text is reconstructed. Startup rejects unsupported schema versions or missing
required columns before changing the schema. Back up the database and use a
compatible release or a new `DATABASE_PATH` if this check fails. Do not edit the
version marker to bypass it. The schema has no general migration framework.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Gufo shows offline | Check `curl http://127.0.0.1:8080/ready` on the host, upstream address, networking, and polling key. `/ready` must return a ready status. |
| Empty charts | Send traffic to the dashboard port, then check time range and model selection. |
| Rate cards ask for a model | Select a model; performance metrics are not mixed across models. |
| Database permission error | Check the bind mount, directory ownership, and configured container UID/GID. |
| Docker container is unhealthy | Check `/api/health` on `DASHBOARD_PORT`, listen address, and container logs. |
| Statistics are missing under load | Inspect `/api/status` pipeline counters for dropped jobs, extraction errors, and write failures. |
| Text capture stays off after editing `.env` | Recreate the Compose container; a restart does not reload its environment. For Python, pass flags explicitly or use `uv run --env-file .env`. |
| A request has no captured text | Check capture state, whether it predates enablement, expiration/deletion, and whether it passed through the dashboard. Images and attachments have no saved content. |
| Captured text is partial, truncated, or dropped | Check the request status, byte limit, and writer load. The 8 MiB queued-text budget can drop content while preserving metrics. |
| Behavior changes after a Gufo upgrade | Review and recapture controlled fixtures, then run the tests. See [Contributing](../CONTRIBUTING.md). |

Clear stats is available in the dashboard's Gufo counters panel. It removes the
recorded history, including daily rollups and captured text. The toolbar’s
Clear captured content button deletes questions and answers while keeping metrics. Read the [privacy reference](privacy.md)
for deletion limits.


## Cache pressure observer

This optional Linux/Docker feature reads structured Gufo cache logs on the
host and writes a small numeric snapshot for the Cache panel, verified against
Gufo 0.4.0 through 0.7.0. It does not mount the Docker socket or raw logs into
the dashboard. Existing HTTP metrics need no log access. Choose the same Gufo
instance that `GUFO_BASE_URL` points to.

From the source checkout, test a single snapshot:

```bash
python3 scripts/observe_gufo_cache.py --container gufo --output data/gufo-cache-state.json
```

The host user must already have access to Docker. The script uses only Python’s
standard library. Set `GUFO_CACHE_STATE_PATH=/data/gufo-cache-state.json` in `.env`
for Compose, or use the absolute host snapshot path for Python runs. The existing
`data` bind mount exposes the snapshot. The dashboard UID must match the host
observer’s UID because snapshots have owner-only permissions.

For continued updates, install the included user service and timer:

```bash
mkdir -p ~/.config/systemd/user ~/.config/gufo-dashboard
cp scripts/systemd/gufo-cache-observer.service scripts/systemd/gufo-cache-observer.timer ~/.config/systemd/user/
```

Create `~/.config/gufo-dashboard/cache-observer.env` with absolute paths:

```dotenv
GUFO_CACHE_OBSERVER_SCRIPT=/absolute/checkout/scripts/observe_gufo_cache.py
GUFO_CONTAINER=gufo
GUFO_CACHE_OUTPUT=/absolute/checkout/data/gufo-cache-state.json
```

Enable the timer and recreate the dashboard after changing `.env`:

```bash
systemctl --user daemon-reload
systemctl --user enable --now gufo-cache-observer.timer
docker compose up -d --build
```

The timer refreshes every 10 seconds while the user manager runs. User-manager
lifetime depends on the machine’s existing login/linger configuration. Check
`systemctl --user status gufo-cache-observer.timer`; disable the feature with
`systemctl --user disable --now gufo-cache-observer.timer` and clear the path
setting. Observations become unavailable after 30 seconds without a successful
refresh, or when the container is stopped. Log replay is bounded to the last
20,000 lines per tick; counts cover that retained window and reset with a new
container. They are not all-time eviction counters.

Keep Gufo’s default `info` level (or `debug`) for capacity startup lines and disk
LRU events. `warn` hides those INFO diagnostics; `error` also hides RAM eviction
warnings. Even at `debug`, routine RAM byte-limit removals and live occupied
snapshot slots are not reported. The Cache panel labels RAM entry-limit events,
capacity limits, and missing values accordingly. Disk values remain unavailable
if disk caching is not configured or its diagnostics are missing. No Gufo restart
or log-level change is required on a server already using `info`.
