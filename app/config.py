"""Settings read from the environment (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _float_or_none(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    return float(value)


@dataclass(frozen=True)
class Settings:
    gufo_base_url: str = "http://127.0.0.1:8080"
    gufo_api_key: str = ""
    database_path: str = "./data/gufo-dashboard.sqlite"
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8081
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
        if self.content_retention_days < 1 or not 1 <= self.content_max_bytes <= 1024 * 1024:
            raise ValueError(
                "content retention must be positive; content limit must be 1-1048576 bytes"
            )

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        return cls(
            gufo_base_url=env.get("GUFO_BASE_URL", cls.gufo_base_url).rstrip("/"),
            gufo_api_key=env.get("GUFO_API_KEY", ""),
            database_path=env.get("DATABASE_PATH", cls.database_path),
            dashboard_host=env.get("DASHBOARD_HOST", cls.dashboard_host),
            dashboard_port=int(env.get("DASHBOARD_PORT", cls.dashboard_port)),
            capture_content=env.get("CAPTURE_CONTENT", "false").lower() in ("true", "1", "yes"),
            content_retention_days=int(
                env.get("CONTENT_RETENTION_DAYS", cls.content_retention_days)
            ),
            content_max_bytes=int(env.get("CONTENT_MAX_BYTES", cls.content_max_bytes)),
            retention_days=int(env.get("RETENTION_DAYS", cls.retention_days)),
            poll_interval_seconds=float(
                env.get("POLL_INTERVAL_SECONDS", cls.poll_interval_seconds)
            ),
            upstream_connect_timeout=float(
                env.get("UPSTREAM_CONNECT_TIMEOUT", cls.upstream_connect_timeout)
            ),
            upstream_read_timeout=_float_or_none(env.get("UPSTREAM_READ_TIMEOUT")),
            stats_queue_size=int(env.get("STATS_QUEUE_SIZE", cls.stats_queue_size)),
            shutdown_drain_timeout=float(
                env.get("SHUTDOWN_DRAIN_TIMEOUT", cls.shutdown_drain_timeout)
            ),
            unattributed_threshold=int(
                env.get("UNATTRIBUTED_THRESHOLD", cls.unattributed_threshold)
            ),
        )
