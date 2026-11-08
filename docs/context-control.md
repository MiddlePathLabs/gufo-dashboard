# Optional context switching

Stock Gufo accepts `--context N` when it starts. It does not currently offer an
HTTP endpoint to change that value in a running process. The dashboard therefore
keeps context controls hidden unless you configure a separate controller that
can restart your Gufo service. Normal proxying and statistics work without one.

The controller is an adapter to **your process manager**, not a Gufo fork. This
repository includes [a loopback controller](../scripts/context_controller.py)
and [an example driver for a Linux systemd user service](../scripts/systemd/gufo_context_driver.sh).

## Linux systemd example

1. Make your existing Gufo user service read its context from an environment
   file. Keep its model path and other options as they are. For example:

   ```ini
   [Service]
   EnvironmentFile=%h/.config/gufo/context.env
   ExecStart=/absolute/path/to/gufo serve --host 127.0.0.1 --port 8080 llm --model /absolute/path/to/model.gguf --context ${GUFO_CONTEXT_TOKENS}
   ```

   Create `~/.config/gufo/context.env` with `GUFO_CONTEXT_TOKENS=131072` and
   reload the user service manager. Your existing unit may need additional Gufo
   options; the example only shows the context connection.

2. Run the controller as the same user that manages Gufo. Set the driver path
   and environment variables to match your service:

   ```bash
   export GUFO_CONTEXT_ENV="$HOME/.config/gufo/context.env"
   export GUFO_CONTEXT_UNIT=gufo.service
   python3 /absolute/path/to/gufo-dashboard/scripts/context_controller.py \
     --driver /absolute/path/to/gufo-dashboard/scripts/systemd/gufo_context_driver.sh \
     --profiles 32768,65536,131072
   ```

   `--profiles` is required: list the context sizes, in tokens, that your model
   and hardware can actually serve (512 to 16777216, at most 32 entries). The
   dashboard offers exactly this list in a dropdown, smallest first. The sizes
   above are only an example.

   The example driver rewrites only the `GUFO_CONTEXT_TOKENS` line, so the file
   may also hold other service variables. If a restart fails it restores the
   previous profile and restarts again; exit status 3 means that also failed and
   Gufo is stopped.

   Keep this process running with your service manager. The controller binds
   `127.0.0.1:8093` by default. The example driver must be executable. Stock
   Gufo may refuse a context above a model's native limit, and larger contexts
   need more memory for the KV cache; leave out sizes that fail to load.

3. Set `GUFO_CONTROL_URL=http://127.0.0.1:8093` in the dashboard environment
   and recreate the dashboard process or container. The standard Linux Compose
   deployment uses host networking, so the container can reach this loopback
   address. The context dropdown appears in Live operations once the controller
   responds.

### Network requirements

The controller can restart Gufo without authentication, so it listens only on
`127.0.0.1` and `GUFO_CONTROL_URL` must be a loopback `http://` address. The
dashboard therefore has to share the host's network namespace with the
controller:

- **Works:** the dashboard run directly on the host, or the default Linux
  Compose file (`network_mode: host`).
- **Does not work as shipped:** the [bridge-network deployment](deployment.md),
  Docker Desktop on macOS or Windows, or a dashboard on a different machine from
  Gufo. Inside those containers `127.0.0.1` is the container itself. Leave
  `GUFO_CONTROL_URL` empty there; everything else in the dashboard still works.

## Other process managers

The bundled driver targets a systemd user service. For Docker, a system-level
unit, launchd, supervisord, or a shell script, write a small executable with the
same two actions and pass it to `--driver`:

- `driver state` prints the context size the current process was started with
  and exits 0, or exits non-zero when it is unknown.
- `driver switch N` makes the next Gufo process start with `--context N`,
  restarts it, and exits 0 once the restart has been issued. Print a one-line
  reason to stderr and exit non-zero on failure; the dashboard shows that line.

The controller only passes values from `--profiles` to `switch`.

## What a switch interrupts

The restart interrupts active Gufo requests while the model reloads. Dashboard
history is stored separately and remains available. The controller has no
authentication; anyone who can reach the dashboard can restart Gufo at any of
the listed sizes.

## Controller contract

Another supervisor can use the same API without the bundled controller:

| Request | Response |
| --- | --- |
| `GET /state` | `{"context_tokens":65536,"profiles":[32768,65536,131072],"switching":false,"last_error":null}` |
| `POST /switch` with `{"context_tokens":32768}` | `202 {"context_tokens":32768,"switching":true}` once the switch has started, or `200 {"context_tokens":32768,"restarted":false}` when already at that size |

`context_tokens` is the configured context for the new or current process; it
may be `null` when no process is present. The bundled controller runs the
driver's `switch` action in the background and reports `"switching":true` until
it exits, so a slow model load is never cut short (`--switch-timeout`, default
600 seconds, is the upper bound). When a switch fails, `last_error` carries a
short reason (the driver's last stderr line) until the next switch starts.
The dashboard checks Gufo's own readiness and model context before reporting
the selected size as active.

Return `422` with `{"detail":"..."}` for a size that is not in `profiles`, and
another non-2xx status with a `detail` when a switch is refused. The dashboard
passes `409` and `422` through and reports any other failure as `502`. It only
offers the sizes in `profiles`, and itself rejects anything outside 512 to
16777216 tokens before contacting the controller.
