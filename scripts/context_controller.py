#!/usr/bin/env python3
"""Optional loopback controller for Gufo context profiles.

The configured driver owns the process manager. It must implement ``state``
(print the configured context token count) and ``switch N`` (restart Gufo).
The operator lists the context sizes the served model supports with
``--profiles``; nothing else is accepted.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Sanity bounds for operator-supplied sizes, not a statement of model support.
MIN_CONTEXT = 512
MAX_CONTEXT = 16_777_216
MAX_PROFILES = 32


def parse_profiles(text: str) -> tuple[int, ...]:
    """Parse ``--profiles``: distinct token counts, returned in ascending order."""
    try:
        profiles = tuple(int(value) for value in text.split(","))
    except ValueError:
        raise ValueError("--profiles must be a comma-separated list of token counts") from None
    if not 1 <= len(profiles) <= MAX_PROFILES or len(set(profiles)) != len(profiles):
        raise ValueError(f"--profiles must list 1 to {MAX_PROFILES} distinct token counts")
    if any(not MIN_CONTEXT <= p <= MAX_CONTEXT for p in profiles):
        raise ValueError(f"--profiles values must be between {MIN_CONTEXT} and {MAX_CONTEXT}")
    return tuple(sorted(profiles))


def current_context(driver: Path) -> int | None:
    try:
        result = subprocess.run(
            [str(driver), "state"], capture_output=True, text=True, timeout=5, check=False
        )
        value = int(result.stdout.strip()) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    # Report whatever the process was started with, even if it is not one of
    # the advertised profiles, so the dashboard can show the real value.
    return value if value is not None and value > 0 else None


def create_handler(
    driver: Path,
    profiles: tuple[int, ...],
    switch_timeout: float = 600.0,
    state_ttl: float = 2.0,
) -> type[BaseHTTPRequestHandler]:
    switch_lock = threading.Lock()
    cache_lock = threading.Lock()
    cache: dict[str, Any] = {"at": None, "value": None}
    status: dict[str, str | None] = {"last_error": None}

    def cached_context() -> int | None:
        # Every open dashboard polls /state; share one driver call per TTL.
        with cache_lock:
            now = time.monotonic()
            if cache["at"] is None or now - cache["at"] >= state_ttl:
                cache["value"] = current_context(driver)
                cache["at"] = now
            value: int | None = cache["value"]
            return value

    def invalidate() -> None:
        with cache_lock:
            cache["at"] = None

    def run_switch(value: int) -> None:
        # Runs with switch_lock held. The driver is not killed early: a slow
        # model load must finish (or fail and roll back) before another switch.
        try:
            result = subprocess.run(
                [str(driver), "switch", str(value)],
                capture_output=True,
                text=True,
                timeout=switch_timeout,
                check=False,
            )
            if result.returncode:
                print(result.stderr[-2000:], flush=True)
                lines = result.stderr.strip().splitlines()
                reason = lines[-1][:200] if lines else "check controller logs"
                status["last_error"] = f"Gufo restart failed: {reason}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(type(exc).__name__, flush=True)
            status["last_error"] = "Gufo restart failed; check controller logs"
        finally:
            invalidate()
            switch_lock.release()

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status_code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, separators=(",", ":")).encode()
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path != "/state":
                self._json(404, {"detail": "not found"})
                return
            switching = switch_lock.locked()
            self._json(
                200,
                {
                    "context_tokens": cached_context(),
                    "profiles": profiles,
                    "switching": switching,
                    "last_error": None if switching else status["last_error"],
                },
            )

        def do_POST(self) -> None:
            if self.path != "/switch":
                self._json(404, {"detail": "not found"})
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                self._json(415, {"detail": "JSON required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 256:
                    raise ValueError
                payload = json.loads(self.rfile.read(length))
            except ValueError:
                self._json(400, {"detail": "invalid request"})
                return
            value = payload.get("context_tokens") if isinstance(payload, dict) else None
            if type(value) is not int or value not in profiles:
                self._json(422, {"detail": "unsupported context profile"})
                return
            if not switch_lock.acquire(blocking=False):
                self._json(409, {"detail": "context switch already running"})
                return
            if current_context(driver) == value:
                switch_lock.release()
                self._json(200, {"context_tokens": value, "restarted": False})
                return
            status["last_error"] = None
            invalidate()
            threading.Thread(target=run_switch, args=(value,), daemon=True).start()
            self._json(202, {"context_tokens": value, "switching": True})

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Gufo context controller")
    parser.add_argument(
        "--driver", type=Path, required=True, help="executable with state/switch actions"
    )
    parser.add_argument("--port", type=int, default=8093)
    parser.add_argument(
        "--profiles",
        required=True,
        help="comma-separated context sizes the served model supports, e.g. 32768,65536",
    )
    parser.add_argument(
        "--switch-timeout",
        type=float,
        default=600.0,
        help="seconds to wait for the driver's switch action, including model load",
    )
    args = parser.parse_args()
    if not args.driver.is_absolute() or not args.driver.is_file():
        parser.error("--driver must be an absolute path to an executable file")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.switch_timeout <= 0:
        parser.error("--switch-timeout must be positive")
    try:
        profiles = parse_profiles(args.profiles)
    except ValueError as exc:
        parser.error(str(exc))
    ThreadingHTTPServer(
        ("127.0.0.1", args.port), create_handler(args.driver, profiles, args.switch_timeout)
    ).serve_forever()


if __name__ == "__main__":
    main()
