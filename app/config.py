"""Settings read from the environment (see .env.example)."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import overload


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None


@overload
def _float_env(name: str, default: float) -> float: ...


@overload
def _float_env(name: str, default: None) -> float | None: ...


def _float_env(name: str, default: float | None) -> float | None:
    value = os.environ.get(name)
    if value is None:
        return default
    if not value.strip() and default is None:
        return None
    try:
        result = float(value)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True)
class Settings:
    gufo_base_url: str = "http://127.0.0.1:8080"
    gufo_api_key: str = ""
    gufo_cache_state_path: str = ""
    database_path: str = "./data/gufo-dashboard.sqlite"
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8081
    dashboard_allowed_hosts: tuple[str, ...] = ("localhost",)
    capture_content: bool = False
    content_retention_days: int = 7
    content_max_bytes: int = 64 * 1024
    retention_days: int = 90
    poll_interval_seconds: float = 5.0
    upstream_connect_timeout: float = 10.0
    upstream_read_timeout: float | None = None
    stats_queue_size: int = 10_000
    shutdown_drain_timeout: float = 5.0
    unattributed_threshold: int = 64
    max_inspect_body_bytes: int = 64 * 1024 * 1024
    enable_poller: bool = True

    def __post_init__(self) -> None:
        if not 1 <= self.dashboard_port <= 65535:
            raise ValueError("DASHBOARD_PORT must be between 1 and 65535")
        for name, value in (
            ("RETENTION_DAYS", self.retention_days),
            ("CONTENT_RETENTION_DAYS", self.content_retention_days),
            ("STATS_QUEUE_SIZE", self.stats_queue_size),
            ("MAX_INSPECT_BODY_BYTES", self.max_inspect_body_bytes),
        ):
            if value < 1:
                raise ValueError(f"{name} must be positive")
        if not 1 <= self.content_max_bytes <= 1024 * 1024:
            raise ValueError("CONTENT_MAX_BYTES must be between 1 and 1048576")
        for name, seconds in (
            ("POLL_INTERVAL_SECONDS", self.poll_interval_seconds),
            ("UPSTREAM_CONNECT_TIMEOUT", self.upstream_connect_timeout),
            ("UPSTREAM_READ_TIMEOUT", self.upstream_read_timeout),
            ("SHUTDOWN_DRAIN_TIMEOUT", self.shutdown_drain_timeout),
        ):
            if seconds is not None and (not math.isfinite(seconds) or seconds <= 0):
                raise ValueError(f"{name} must be finite and positive")
        if self.unattributed_threshold < 0:
            raise ValueError("UNATTRIBUTED_THRESHOLD must be nonnegative")
        for host in self.dashboard_allowed_hosts:
            if not host or any(c in host for c in "*/:?#@ \t\r\n"):
                raise ValueError(
                    "DASHBOARD_ALLOWED_HOSTS must contain exact hostnames without ports"
                )

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        return cls(
            gufo_base_url=env.get("GUFO_BASE_URL", cls.gufo_base_url).rstrip("/"),
            gufo_api_key=env.get("GUFO_API_KEY", ""),
            gufo_cache_state_path=env.get("GUFO_CACHE_STATE_PATH", ""),
            database_path=env.get("DATABASE_PATH", cls.database_path),
            dashboard_host=env.get("DASHBOARD_HOST", cls.dashboard_host),
            dashboard_port=_int_env("DASHBOARD_PORT", cls.dashboard_port),
            dashboard_allowed_hosts=tuple(
                host.strip().lower().rstrip(".")
                for host in env.get("DASHBOARD_ALLOWED_HOSTS", "localhost").split(",")
            ),
            capture_content=env.get("CAPTURE_CONTENT", "false").lower() in ("true", "1", "yes"),
            content_retention_days=_int_env("CONTENT_RETENTION_DAYS", cls.content_retention_days),
            content_max_bytes=_int_env("CONTENT_MAX_BYTES", cls.content_max_bytes),
            retention_days=_int_env("RETENTION_DAYS", cls.retention_days),
            poll_interval_seconds=_float_env("POLL_INTERVAL_SECONDS", cls.poll_interval_seconds),
            upstream_connect_timeout=_float_env(
                "UPSTREAM_CONNECT_TIMEOUT", cls.upstream_connect_timeout
            ),
            upstream_read_timeout=_float_env("UPSTREAM_READ_TIMEOUT", None),
            stats_queue_size=_int_env("STATS_QUEUE_SIZE", cls.stats_queue_size),
            shutdown_drain_timeout=_float_env("SHUTDOWN_DRAIN_TIMEOUT", cls.shutdown_drain_timeout),
            unattributed_threshold=_int_env("UNATTRIBUTED_THRESHOLD", cls.unattributed_threshold),
        )
