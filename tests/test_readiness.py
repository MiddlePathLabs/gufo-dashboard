"""Startup compatibility, operator settings, and dashboard request validation."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from starlette.types import Scope

from app import db, version
from app.config import Settings
from app.main import Dispatcher, create_app
from scripts import capture_fixtures

from .conftest import Dash, ServerThread


@pytest.mark.parametrize("bucket", ["0000", "0001", "0999", "999", "-1000", "nope"])
@pytest.mark.parametrize("range_", ["1h", "all"])
def test_invalid_custom_bucket(dash: Dash, bucket: str, range_: str) -> None:
    with dash.client() as client:
        assert (
            client.get("/api/timeseries", params={"bucket": bucket, "range": range_}).status_code
            == 422
        )


def test_custom_bucket_and_version(dash: Dash) -> None:
    with dash.client() as client:
        response = client.get("/api/timeseries", params={"bucket": "1000", "range": "1h"})
        assert response.status_code == 200
        assert response.json()["bucket_ms"] == 1000
        assert client.get("/api/status").json()["version"] == version.dashboard_version()


@pytest.mark.parametrize("schema_version", [0, 3, 999])
def test_unknown_schema_rejected_without_ddl(tmp_path: Path, schema_version: int) -> None:
    path = str(tmp_path / "db.sqlite")
    db.init_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE request_content")
        conn.execute("UPDATE schema_version SET version=?", (schema_version,))
    with pytest.raises(RuntimeError, match=r"Unsupported database schema.*Back up"):
        db.init_db(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == schema_version
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE name='request_content'"
        ).fetchone()


def test_incompatible_schema_rejected_before_version_stamp(tmp_path: Path) -> None:
    path = str(tmp_path / "db.sqlite")
    db.init_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE request_stats DROP COLUMN reasoning_tokens")
        conn.execute("UPDATE schema_version SET version=1")
    with pytest.raises(RuntimeError, match="request_stats is missing reasoning_tokens"):
        db.init_db(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 1


def test_supported_upgrade_preserves_statistics(tmp_path: Path) -> None:
    path = str(tmp_path / "db.sqlite")
    db.init_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO request_stats (model, completed_at_ms) VALUES ('preserved', 1)")
        conn.execute("DROP TABLE request_content")
        conn.execute("UPDATE schema_version SET version=1")
    db.init_db(path)
    db.init_db(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT model FROM request_stats").fetchone()[0] == "preserved"
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM request_content").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DASHBOARD_PORT", "oops"),
        ("DASHBOARD_PORT", "0"),
        ("DASHBOARD_PORT", "65536"),
        ("RETENTION_DAYS", "-1"),
        ("CONTENT_RETENTION_DAYS", "0"),
        ("CONTENT_MAX_BYTES", "1048577"),
        ("STATS_QUEUE_SIZE", "0"),
        ("POLL_INTERVAL_SECONDS", "oops"),
        ("POLL_INTERVAL_SECONDS", "0"),
        ("POLL_INTERVAL_SECONDS", "nan"),
        ("POLL_INTERVAL_SECONDS", "inf"),
        ("UPSTREAM_CONNECT_TIMEOUT", "-1"),
        ("UPSTREAM_READ_TIMEOUT", "0"),
        ("SHUTDOWN_DRAIN_TIMEOUT", "-1"),
        ("UNATTRIBUTED_THRESHOLD", "-1"),
        ("DASHBOARD_ALLOWED_HOSTS", "*"),
        ("DASHBOARD_ALLOWED_HOSTS", "host:8081"),
    ],
)
def test_bad_environment_names_variable(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        Settings.from_env()


def test_optional_timeout_and_named_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPSTREAM_READ_TIMEOUT", "")
    monkeypatch.setenv("DASHBOARD_ALLOWED_HOSTS", "localhost, Gufo.Home.")
    settings = Settings.from_env()
    assert settings.upstream_read_timeout is None
    assert settings.dashboard_allowed_hosts == ("localhost", "gufo.home")


@pytest.mark.parametrize("path", ["/", "/api/status", "/health", "/v1/models"])
def test_rebinding_host_blocked_for_dashboard_and_proxy(dash: Dash, path: str) -> None:
    with dash.client() as client:
        assert client.get(path, headers={"Host": "attacker.example:8081"}).status_code == 400


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("localhost:8081", True),
        ("192.168.1.42:8081", True),
        ("[::1]:8081", True),
        ("gufo.home:8081", True),
        ("attacker.example:8081", False),
        ("localhost@attacker.example", False),
        ("localhost/path", False),
        ("localhost:99999", False),
        ("local host", False),
        ("localhost\t", False),
    ],
)
def test_host_validation(host: str, allowed: bool) -> None:
    app = create_app(Settings(dashboard_allowed_hosts=("localhost", "gufo.home")))
    assert isinstance(app, Dispatcher)
    scope: Scope = {"type": "http", "headers": [(b"host", host.encode())]}
    assert app.valid_host(scope) is allowed
    scope["headers"].append((b"host", b"localhost"))
    assert not app.valid_host(scope)


def test_source_checkout_version_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_: str) -> str:
        raise version.PackageNotFoundError

    monkeypatch.setattr(version, "version", missing)
    assert version.dashboard_version() == "unknown (source checkout)"


def test_fixture_capture_records_build(
    tmp_path: Path, fake_server: ServerThread, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture_fixtures, "OUT", tmp_path)
    monkeypatch.setenv("GUFO_BASE_URL", fake_server.url)
    monkeypatch.setattr(
        "sys.argv",
        ["capture_fixtures.py", "--gufo-version", "test-build", "--gufo-commit", "abc123"],
    )
    capture_fixtures.main()
    build: dict[str, Any] = json.loads((tmp_path / "gufo_build.json").read_text())
    assert build["gufo_version"] == "test-build"
    assert build["gufo_commit"] == "abc123"
    assert build["source"] == "operator-supplied"
    assert build["captured_at"]
    assert (tmp_path / "chat_nonstream.json").exists()
