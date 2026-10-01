"""Export only allowlisted cache diagnostics from a local Docker Gufo container.

Run on the Docker host, once per timer tick. The dashboard reads the atomic JSON
snapshot and requires no Docker socket, raw logs, or elevated container access.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Allow running directly from a source checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.cache_pressure import CacheLogSummary

INSPECT = (
    '{"id":{{json .Id}},"running":{{json .State.Running}},'
    '"version":{{json (index .Config.Labels "org.opencontainers.image.version")}},'
    '"args":{{json .Config.Cmd}},"entrypoint":{{json .Config.Entrypoint}}}'
)


def log_level(args: list[str]) -> str | None:
    level = "info" if "serve" in args else None
    for index, arg in enumerate(args):
        if arg in {"-v", "--verbose"}:
            level = "debug"
        elif arg == "--log-level" and index + 1 < len(args):
            level = args[index + 1]
        elif arg.startswith("--log-level="):
            level = arg.split("=", 1)[1]
    return level if level in {"debug", "info", "warn", "error"} else None


def observe(container: str, limit: int) -> dict:
    meta = json.loads(
        subprocess.check_output(
            ["docker", "inspect", "--format", INSPECT, container],
            stderr=subprocess.DEVNULL,
            timeout=10,
            text=True,
        )
    )
    summary = CacheLogSummary(
        meta.get("version"), log_level((meta.get("entrypoint") or []) + (meta.get("args") or []))
    )
    # Pin to the inspected ID so a container replacement cannot mix generations.
    proc = subprocess.Popen(
        ["docker", "logs", "--timestamps", "--tail", str(limit), meta["id"]],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            summary.feed(line)
        code = proc.wait(timeout=10)
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    summary.state.update(
        observer_ok=code == 0 and meta.get("running") is True, updated_at_ms=int(time.time() * 1000)
    )
    return summary.state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="gufo")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tail", type=int, default=20000)
    args = parser.parse_args()
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", args.container)
        or not 1 <= args.tail <= 100000
    ):
        parser.error("invalid container name or tail limit")
    try:
        state = observe(args.container, args.tail)
    except (OSError, ValueError, subprocess.SubprocessError):
        state = {"schema": 1, "observer_ok": False, "updated_at_ms": int(time.time() * 1000)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".gufo-cache-", dir=args.output.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, allow_nan=False)
        os.replace(temporary, args.output)
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
