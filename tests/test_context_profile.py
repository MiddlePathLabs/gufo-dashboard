"""Optional host controller is exposed only through fixed context profiles."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app.config import Settings

from .conftest import Dash, ServerThread, make_dash


@pytest.fixture
def controlled_dash(
    tmp_path: Path, fake_server: ServerThread
) -> Iterator[tuple[Dash, list[dict[str, Any]]]]:
    received: list[dict[str, Any]] = []

    async def state(_: Request) -> JSONResponse:
        return JSONResponse(
            {"context_tokens": 131072, "profiles": [65536, 131072, 262144], "switching": False}
        )

    async def switch(request: Request) -> JSONResponse:
        received.append(await request.json())
        return JSONResponse({"context_tokens": received[-1]["context_tokens"], "restarted": True})

    helper = ServerThread(
        Starlette(routes=[Route("/state", state), Route("/switch", switch, methods=["POST"])])
    ).start()
    dash = make_dash(tmp_path, fake_server.url, gufo_control_url=helper.url)
    yield dash, received
    dash.server.stop()
    helper.stop()


def test_context_profiles_are_forwarded(controlled_dash: tuple[Dash, list[dict[str, Any]]]) -> None:
    dash, received = controlled_dash
    with dash.client() as client:
        state = client.get("/api/context-profile")
        response = client.post("/api/context-profile", json={"context_tokens": 262144})
    assert state.json() == {
        "available": True,
        "context_tokens": 131072,
        "profiles": [65536, 131072, 262144],
        "switching": False,
    }
    assert response.status_code == 200
    assert received == [{"context_tokens": 262144}]


@pytest.mark.parametrize("invalid", [0, 511, 16_777_217, True, 65536.0, "65536", None])
def test_context_profile_rejects_other_values(
    controlled_dash: tuple[Dash, list[dict[str, Any]]], invalid: Any
) -> None:
    dash, received = controlled_dash
    with dash.client() as client:
        response = client.post("/api/context-profile", json={"context_tokens": invalid})
    assert response.status_code == 422
    assert received == []


def test_context_control_disabled_by_default(dash: Dash) -> None:
    with dash.client() as client:
        assert client.get("/api/context-profile").json() == {"available": False}
        assert (
            client.post("/api/context-profile", json={"context_tokens": 65536}).status_code == 404
        )


def test_context_control_url_must_be_loopback() -> None:
    with pytest.raises(ValueError, match="GUFO_CONTROL_URL"):
        Settings(gufo_control_url="http://example.com:8093")


@pytest.fixture
def odd_controller(
    tmp_path: Path, fake_server: ServerThread
) -> Iterator[tuple[Dash, dict[str, Any]]]:
    reply: dict[str, Any] = {}

    async def state(_: Request) -> Response:
        return Response(reply["state"], media_type="application/json")

    async def switch(_: Request) -> Response:
        return Response(reply["body"], status_code=reply["status"], media_type="application/json")

    helper = ServerThread(
        Starlette(routes=[Route("/state", state), Route("/switch", switch, methods=["POST"])])
    ).start()
    dash = make_dash(tmp_path, fake_server.url, gufo_control_url=helper.url)
    yield dash, reply
    dash.server.stop()
    helper.stop()


@pytest.mark.parametrize("body", ["[]", '"ok"', "not json"])
def test_context_state_must_be_an_object(
    odd_controller: tuple[Dash, dict[str, Any]], body: str
) -> None:
    dash, reply = odd_controller
    reply["state"] = body
    with dash.client() as client:
        assert client.get("/api/context-profile").json() == {"available": False}


@pytest.mark.parametrize(
    ("status", "body", "expected_status", "expected"),
    [
        (204, "", 200, {"context_tokens": 65536}),
        (202, "ok", 200, {"context_tokens": 65536}),
        (409, '{"detail":"busy"}', 409, {"detail": "busy"}),
        (404, '{"detail":"not found"}', 502, {"detail": "not found"}),
        (500, '["x"]', 502, {"detail": "context switch failed"}),
    ],
)
def test_context_switch_handles_controller_replies(
    odd_controller: tuple[Dash, dict[str, Any]],
    status: int,
    body: str,
    expected_status: int,
    expected: dict[str, Any],
) -> None:
    dash, reply = odd_controller
    reply.update(status=status, body=body)
    with dash.client() as client:
        response = client.post("/api/context-profile", json={"context_tokens": 65536})
    assert response.status_code == expected_status
    assert response.json() == expected


def test_context_profile_forwards_any_sane_size(
    controlled_dash: tuple[Dash, list[dict[str, Any]]],
) -> None:
    dash, received = controlled_dash
    with dash.client() as client:
        assert client.post("/api/context-profile", json={"context_tokens": 100000}).is_success
    # The controller, not the dashboard, decides whether it is advertised.
    assert received == [{"context_tokens": 100000}]
