"""The PROJECTION_PORT listener: chat completions default to "projection": true."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import Response
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from app import extract

from .conftest import MODEL, Dash, FakeGufo, ServerThread, make_projection_dash

CHAT = {"model": MODEL, "messages": [{"role": "user", "content": "Say hi"}], "max_tokens": 16}


@pytest.fixture
def pdash(tmp_path: Path, fake_server: ServerThread) -> Iterator[Dash]:
    d = make_projection_dash(tmp_path, fake_server.url)
    yield d
    d.server.stop()


def _projection_client(d: Dash) -> httpx.Client:
    port = d.settings.projection_port
    return httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30)


def _forwarded(fake: FakeGufo) -> dict:
    return json.loads(fake.requests[-1].body)


def test_projection_port_adds_field(pdash: Dash, fake: FakeGufo) -> None:
    with _projection_client(pdash) as client:
        r = client.post("/v1/chat/completions", json=CHAT)
    assert r.status_code == 200
    assert _forwarded(fake)["projection"] is True
    assert pdash.wait_rows(1)[0]["projection"] == 1


def test_projection_port_streaming_keeps_usage_injection(pdash: Dash, fake: FakeGufo) -> None:
    with _projection_client(pdash) as client:
        r = client.post("/v1/chat/completions", json={**CHAT, "stream": True})
    assert r.status_code == 200
    sent = _forwarded(fake)
    assert sent["projection"] is True
    assert sent["stream_options"] == {"include_usage": True}
    row = pdash.wait_rows(1)[0]
    assert row["projection"] == 1 and row["completion_tokens"] is not None


@pytest.mark.parametrize(("value", "stored"), [(False, 0), (True, 1)])
def test_projection_port_respects_explicit_field(
    pdash: Dash, fake: FakeGufo, value: bool, stored: int
) -> None:
    with _projection_client(pdash) as client:
        assert client.post("/v1/chat/completions", json={**CHAT, "projection": value}).is_success
    assert _forwarded(fake)["projection"] is value
    assert pdash.wait_rows(1)[0]["projection"] == stored


def test_projection_port_replaces_null(pdash: Dash, fake: FakeGufo) -> None:
    with _projection_client(pdash) as client:
        assert client.post("/v1/chat/completions", json={**CHAT, "projection": None}).is_success
    assert _forwarded(fake)["projection"] is True


def test_main_port_unchanged(pdash: Dash, fake: FakeGufo) -> None:
    raw = json.dumps(CHAT).encode()
    with pdash.client() as client:
        r = client.post(
            "/v1/chat/completions", content=raw, headers={"content-type": "application/json"}
        )
    assert r.status_code == 200
    assert fake.requests[-1].body == raw
    assert pdash.wait_rows(1)[0]["projection"] is None


@pytest.mark.parametrize(("value", "stored"), [(False, 0), (True, 1)])
def test_main_port_respects_explicit_projection(
    pdash: Dash, fake: FakeGufo, value: bool, stored: int
) -> None:
    with pdash.client() as client:
        assert client.post("/v1/chat/completions", json={**CHAT, "projection": value}).is_success
    assert _forwarded(fake)["projection"] is value
    assert pdash.wait_rows(1)[0]["projection"] == stored


@pytest.mark.parametrize(
    "path", ["/v1/responses", "/v1/messages", "/v1/completions", "/completion"]
)
def test_projection_port_refuses_unprojectable_endpoints(
    pdash: Dash, fake: FakeGufo, path: str
) -> None:
    with _projection_client(pdash) as client:
        r = client.post(path, json={"model": MODEL, "input": "hi", "prompt": "hi"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "projection_unsupported_endpoint"
    assert not [q for q in fake.requests if q.path == path]
    assert pdash.rows() == []


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'{"model": "m", "x": NaN}'])
def test_projection_port_refuses_unrewritable_body(
    pdash: Dash, fake: FakeGufo, body: bytes
) -> None:
    with _projection_client(pdash) as client:
        r = client.post(
            "/v1/chat/completions", content=body, headers={"content-type": "application/json"}
        )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "projection_not_applied"
    assert not [q for q in fake.requests if q.path == "/v1/chat/completions"]


def test_projection_port_passes_other_routes(pdash: Dash, fake: FakeGufo) -> None:
    with _projection_client(pdash) as client:
        assert client.get("/props").status_code in (200, 404)
        assert client.get("/api/health").status_code == 200
    assert fake.requests[-1].path == "/props"


def test_projection_port_lists_aliased_models(pdash: Dash, fake: FakeGufo) -> None:
    with _projection_client(pdash) as proj, pdash.client() as main:
        aliased = proj.get("/v1/models")
        plain = main.get("/v1/models").json()
    assert aliased.status_code == 200
    assert int(aliased.headers["content-length"]) == len(aliased.content)
    assert [m["id"] for m in aliased.json()["data"]] == [f"{MODEL}-projection"]
    assert [m["id"] for m in plain["data"]] == [MODEL]
    assert fake.requests[-1].path == "/v1/models"


def test_projection_port_single_model_lookup(pdash: Dash, fake: FakeGufo) -> None:
    def single(request: Request, body: bytes) -> Response | None:
        if request.url.path == f"/v1/models/{MODEL}":
            return Response(
                json.dumps({"id": MODEL, "object": "model"}), media_type="application/json"
            )
        return None

    fake.override = single
    with _projection_client(pdash) as client:
        r = client.get(f"/v1/models/{MODEL}-projection")
    assert r.json()["id"] == f"{MODEL}-projection"
    assert fake.requests[-1].path == f"/v1/models/{MODEL}"


def test_projection_port_unaliases_requested_model(pdash: Dash, fake: FakeGufo) -> None:
    with _projection_client(pdash) as client:
        r = client.post("/v1/chat/completions", json={**CHAT, "model": f"{MODEL}-projection"})
    assert r.status_code == 200
    sent = _forwarded(fake)
    assert sent["model"] == MODEL and sent["projection"] is True
    assert pdash.wait_rows(1)[0]["model"] == MODEL


def test_main_port_keeps_suffixed_model(pdash: Dash, fake: FakeGufo) -> None:
    with pdash.client() as client:
        client.post("/v1/chat/completions", json={**CHAT, "model": f"{MODEL}-projection"})
    assert _forwarded(fake)["model"] == f"{MODEL}-projection"


def test_empty_model_suffix_disables_alias(
    tmp_path: Path, fake: FakeGufo, fake_server: ServerThread
) -> None:
    d = make_projection_dash(tmp_path, fake_server.url, projection_model_suffix="")
    try:
        with _projection_client(d) as client:
            assert client.get("/v1/models").json()["data"][0]["id"] == MODEL
    finally:
        d.server.stop()


def test_projection_port_closes_websockets(pdash: Dash) -> None:
    port = pdash.settings.projection_port
    with (
        pytest.raises(InvalidStatus),
        connect(f"ws://127.0.0.1:{port}/v1/realtime", open_timeout=5),
    ):
        pass


def test_analyze_request_projection_default() -> None:
    body = json.dumps(CHAT).encode()
    assert extract.analyze_request("chat", body).projection is None
    info = extract.analyze_request("chat", body, default_projection=True)
    assert info.projection is True and json.loads(info.body)["projection"] is True
    other = extract.analyze_request("responses", body, default_projection=True)
    assert other.projection is None and other.body == body


def test_status_reports_listener(pdash: Dash) -> None:
    with pdash.client() as main, _projection_client(pdash) as proj:
        plain = main.get("/api/status").json()["listener"]
        steered = proj.get("/api/status").json()["listener"]
    port = pdash.settings.projection_port
    assert plain == {"projection": False, "dashboard_port": 8081, "projection_port": port}
    assert steered["projection"] is True and steered["projection_port"] == port


def test_status_listener_without_projection_port(dash: Dash) -> None:
    with dash.client() as client:
        listener = client.get("/api/status").json()["listener"]
    assert listener["projection"] is False and listener["projection_port"] is None


def test_projection_comparison_splits_traffic(pdash: Dash) -> None:
    with pdash.client() as main, _projection_client(pdash) as proj:
        assert main.post("/v1/chat/completions", json=CHAT).is_success
        assert main.post("/v1/chat/completions", json={**CHAT, "projection": False}).is_success
        assert proj.post("/v1/chat/completions", json=CHAT).is_success
        pdash.wait_rows(3)
        data = main.get("/api/projection", params={"range": "1h"}).json()
        assert proj.get("/api/projection", params={"range": "all"}).json()["per_model"]
    [m] = data["per_model"]
    assert m["model"] == MODEL
    assert m["plain"]["requests"] == 2 and m["projected"]["requests"] == 1
    assert m["projected"]["generated_tokens"] > 0
    assert m["projected"]["generated_avg"] == m["projected"]["generated_tokens"]


def test_projection_comparison_without_projected_rows(dash: Dash) -> None:
    with dash.client() as client:
        assert client.post("/v1/chat/completions", json=CHAT).is_success
        dash.wait_rows(1)
        [m] = client.get("/api/projection").json()["per_model"]
    assert m["projected"] is None and m["plain"]["requests"] == 1
