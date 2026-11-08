"""Public controller protocol works with a stock-Gufo process-manager driver."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from scripts.context_controller import create_handler, current_context, parse_profiles


def _wait_idle(client: httpx.Client) -> dict[str, object]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state: dict[str, object] = client.get("/state").json()
        if not state["switching"]:
            return state
        time.sleep(0.02)
    raise AssertionError("switch did not finish")


def _serve(driver: Path, **kwargs: object) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(driver, **kwargs))  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_slow_switch_holds_lock_and_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = tmp_path / "gate"
    driver = tmp_path / "driver"
    driver.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys, time\n"
        "if sys.argv[1] == 'state': print(131072)\n"
        "else:\n"
        "    gate = pathlib.Path(os.environ['GUFO_TEST_GATE'])\n"
        "    while not gate.exists(): time.sleep(0.01)\n"
        "    print('restart at 65536 failed; restored 131072', file=sys.stderr)\n"
        "    sys.exit(1)\n"
    )
    driver.chmod(0o755)
    monkeypatch.setenv("GUFO_TEST_GATE", str(gate))
    server, thread = _serve(driver, profiles=(65536, 131072, 262144), state_ttl=0)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}") as client:
            assert client.post("/switch", json={"context_tokens": 65536}).status_code == 202
            assert client.get("/state").json()["switching"] is True
            assert client.post("/switch", json={"context_tokens": 262144}).status_code == 409
            gate.touch()
            final = _wait_idle(client)
            assert final["last_error"] == (
                "Gufo restart failed: restart at 65536 failed; restored 131072"
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_controller_uses_driver_and_advertised_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "context"
    state.write_text("131072\n")
    driver = tmp_path / "driver"
    driver.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "state = pathlib.Path(os.environ['GUFO_TEST_CONTEXT'])\n"
        "if sys.argv[1] == 'state': print(state.read_text().strip())\n"
        "elif sys.argv[1] == 'switch': state.write_text(sys.argv[2] + '\\n')\n"
    )
    driver.chmod(0o755)
    monkeypatch.setenv("GUFO_TEST_CONTEXT", str(state))
    assert current_context(driver) == 131072

    server, thread = _serve(driver, profiles=(65536, 131072))
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}") as client:
            assert client.get("/state").json() == {
                "context_tokens": 131072,
                "profiles": [65536, 131072],
                "switching": False,
                "last_error": None,
            }
            assert client.post("/switch", json={"context_tokens": 262144}).status_code == 422
            result = client.post("/switch", json={"context_tokens": 65536})
            assert result.status_code == 202
            assert result.json() == {"context_tokens": 65536, "switching": True}
            final = _wait_idle(client)
            assert final["context_tokens"] == 65536
            assert final["last_error"] is None
            unchanged = client.post("/switch", json={"context_tokens": 65536})
            assert unchanged.json() == {"context_tokens": 65536, "restarted": False}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


DRIVER = Path(__file__).parents[1] / "scripts/systemd/gufo_context_driver.sh"


def _driver_env(tmp_path: Path, env_file: Path, systemctl: str) -> dict[str, str]:
    mock_systemctl = tmp_path / "systemctl"
    mock_systemctl.write_text(systemctl)
    mock_systemctl.chmod(0o755)
    return {
        **os.environ,
        "GUFO_CONTEXT_ENV": str(env_file),
        "GUFO_CONTEXT_UNIT": "gufo.service",
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }


def test_systemd_example_driver_updates_environment_file(tmp_path: Path) -> None:
    env_file = tmp_path / "context.env"
    env_file.write_text("GUFO_CONTEXT_TOKENS=131072\n")
    env = _driver_env(tmp_path, env_file, "#!/bin/sh\nexit 0\n")
    driver = DRIVER
    assert (
        subprocess.run(
            [str(driver), "state"], env=env, capture_output=True, text=True
        ).stdout.strip()
        == "131072"
    )
    assert subprocess.run([str(driver), "switch", "65536"], env=env).returncode == 0
    assert env_file.read_text() == "GUFO_CONTEXT_TOKENS=65536\n"


def test_systemd_driver_keeps_other_environment_lines(tmp_path: Path) -> None:
    env_file = tmp_path / "gufo.env"
    env_file.write_text("GUFO_MODEL=/m.gguf\nGUFO_CONTEXT_TOKENS=131072\nGUFO_API_KEY=k\n")
    env_file.chmod(0o640)
    env = _driver_env(tmp_path, env_file, "#!/bin/sh\nexit 0\n")
    assert subprocess.run([str(DRIVER), "switch", "262144"], env=env).returncode == 0
    assert env_file.read_text() == (
        "GUFO_MODEL=/m.gguf\nGUFO_API_KEY=k\nGUFO_CONTEXT_TOKENS=262144\n"
    )
    assert env_file.stat().st_mode & 0o777 == 0o640
    assert list(tmp_path.glob("gufo.env.*")) == []


@pytest.mark.parametrize(
    ("systemctl", "code", "final"),
    [
        # The new profile fails; restarting at the old one works.
        ('#!/bin/sh\ngrep -q 65536 "$GUFO_CONTEXT_ENV" && exit 1\nexit 0\n', 1, "131072"),
        # Both restarts fail: the unit is down and the driver says so.
        ("#!/bin/sh\nexit 1\n", 3, "131072"),
    ],
)
def test_systemd_driver_rollback(tmp_path: Path, systemctl: str, code: int, final: str) -> None:
    env_file = tmp_path / "context.env"
    env_file.write_text("GUFO_OTHER=1\nGUFO_CONTEXT_TOKENS=131072\n")
    env = _driver_env(tmp_path, env_file, systemctl)
    result = subprocess.run(
        [str(DRIVER), "switch", "65536"], env=env, capture_output=True, text=True
    )
    assert result.returncode == code
    assert "131072" in result.stderr
    assert env_file.read_text() == f"GUFO_OTHER=1\nGUFO_CONTEXT_TOKENS={final}\n"


def test_parse_profiles_accepts_any_sane_sizes() -> None:
    assert parse_profiles("100000,8192,1048576") == (8192, 100000, 1048576)
    assert parse_profiles("4096") == (4096,)


@pytest.mark.parametrize(
    "text", ["", "abc", "8192,8192", "256", "99999999", ",".join(["1024"] * 33)]
)
def test_parse_profiles_rejects_bad_lists(text: str) -> None:
    with pytest.raises(ValueError, match="--profiles"):
        parse_profiles(text)


def test_controller_reports_unlisted_running_size(tmp_path: Path) -> None:
    driver = tmp_path / "driver"
    driver.write_text("#!/bin/sh\necho 100000\n")
    driver.chmod(0o755)
    assert current_context(driver) == 100000


def test_systemd_driver_accepts_any_numeric_size(tmp_path: Path) -> None:
    env_file = tmp_path / "context.env"
    env_file.write_text("GUFO_CONTEXT_TOKENS=8192\n")
    env = _driver_env(tmp_path, env_file, "#!/bin/sh\nexit 0\n")
    assert subprocess.run([str(DRIVER), "switch", "100000"], env=env).returncode == 0
    state = subprocess.run([str(DRIVER), "state"], env=env, capture_output=True, text=True)
    assert state.stdout.strip() == "100000"
    for bad in ("", "0", "012", "1e5", "-1", "1;reboot"):
        assert subprocess.run([str(DRIVER), "switch", bad], env=env).returncode == 2
