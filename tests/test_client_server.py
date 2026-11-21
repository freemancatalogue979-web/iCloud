"""Client server on :9002 — the single page, the iframe, and the WebSocket.

The WebSocket tests exercise the real protocol against the real SessionManager.
The flow itself fails at the launch step here (no browser binary in this
sandbox), which is exactly the path worth asserting: the client must receive a
well-formed `flow_done` with a reason, not a dropped socket.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from client.server import create_app


@pytest.fixture
def client(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def drain_until(ws, predicate, limit: int = 60) -> list[dict]:
    """Read messages until one satisfies `predicate` (or the limit is hit)."""
    seen: list[dict] = []
    for _ in range(limit):
        message = ws.receive_json()
        seen.append(message)
        if predicate(message):
            return seen
    raise AssertionError(
        f"predicate not met within {limit} messages; got: "
        f"{[m.get('type') for m in seen]}"
    )


# ------------------------------------------------------------------ pages
def test_root_serves_the_single_client_page(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    # The page must contain the iframe and the WebSocket wiring.
    assert "<iframe" in res.text
    assert 'src="/view"' in res.text
    assert "WebSocket(" in res.text


def test_root_page_starts_the_flow_on_visit(client: TestClient) -> None:
    """The visit itself is the trigger — no button press required."""
    html = client.get("/").text
    assert 'cmd: "start"' in html or "cmd\":\"start" in html
    assert "ws.onopen" in html


def test_view_page_is_the_iframe_content(client: TestClient) -> None:
    res = client.get("/view")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "/shot.png" in res.text


def test_iframe_is_same_origin(client: TestClient) -> None:
    """A relative src keeps the iframe same-origin, so postMessage is safe."""
    html = client.get("/").text
    assert 'src="/view"' in html
    assert "http://" not in html.split("<iframe")[1].split(">")[0]


# ------------------------------------------------------------------ HTTP
def test_health(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["session_state"] == "idle"
    assert body["has_screenshot"] is False


def test_shot_png_is_404_before_any_capture(client: TestClient) -> None:
    assert client.get("/shot.png").status_code == 404


# ------------------------------------------------------------- websocket
def test_ws_handshake_sends_hello(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert "driver" in hello


def test_ws_sends_status_after_hello(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        messages = drain_until(ws, lambda m: m["type"] == "status")
        assert messages[-1]["state"] == "idle"


def test_ws_ping_pong(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        drain_until(ws, lambda m: m["type"] == "status")
        ws.send_text(json.dumps({"cmd": "ping"}))
        pong = drain_until(ws, lambda m: m["type"] == "pong")
        assert pong[-1]["type"] == "pong"


def test_ws_rejects_unknown_command(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        drain_until(ws, lambda m: m["type"] == "status")
        ws.send_text(json.dumps({"cmd": "fly"}))
        messages = drain_until(ws, lambda m: m["type"] == "error")
        assert "fly" in messages[-1]["message"]


def test_ws_rejects_invalid_json(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        drain_until(ws, lambda m: m["type"] == "status")
        ws.send_text("{not json")
        messages = drain_until(ws, lambda m: m["type"] == "error")
        assert "invalid JSON" in messages[-1]["message"]


def test_visit_triggers_the_flow_and_reports_completion(client: TestClient) -> None:
    """End-to-end: a visit runs launch -> navigate, and the client is told why
    it could not finish. This exercises the real SessionManager."""
    with client.websocket_connect("/ws") as ws:
        drain_until(ws, lambda m: m["type"] == "status")
        ws.send_text(json.dumps({"cmd": "start"}))

        messages = drain_until(ws, lambda m: m["type"] == "flow_done", limit=120)

        done = messages[-1]
        assert "ok" in done and "steps" in done
        assert "launch" in done["steps"]
        # No browser binary in this sandbox, so the flow stops at launch.
        if not done["ok"]:
            assert done["error"]
            assert "failed" in done["steps"]


def test_flow_steps_stream_incrementally(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        drain_until(ws, lambda m: m["type"] == "status")
        ws.send_text(json.dumps({"cmd": "start"}))

        messages = drain_until(ws, lambda m: m["type"] == "flow", limit=60)
        first = messages[-1]
        assert first["step"] == "begin"
        assert first["ok"] is True


def test_server_log_events_reach_the_socket(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        drain_until(ws, lambda m: m["type"] == "status")
        ws.send_text(json.dumps({"cmd": "start"}))
        messages = drain_until(
            ws, lambda m: m["type"] == "event", limit=120
        )
        assert any(m["type"] == "event" for m in messages)


def test_stop_command_is_accepted_without_a_session(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        drain_until(ws, lambda m: m["type"] == "status")
        ws.send_text(json.dumps({"cmd": "stop"}))
        messages = drain_until(
            ws, lambda m: m["type"] == "status" and m["state"] == "stopped"
        )
        assert messages[-1]["state"] == "stopped"


def test_click_signin_without_a_session_reports_error(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        drain_until(ws, lambda m: m["type"] == "status")
        ws.send_text(json.dumps({"cmd": "click_signin"}))
        messages = drain_until(ws, lambda m: m["type"] == "error", limit=40)
        assert "No active browser session" in messages[-1]["message"]
