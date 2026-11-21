"""End-to-end API tests against the real FastAPI app via TestClient."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


# ------------------------------------------------------------------- basics
def test_index_serves_the_console(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "iCloud Browser Console" in res.text
    assert "/static/app.js" in res.text


def test_static_assets_are_mounted(client: TestClient) -> None:
    for path in ("/static/app.js", "/static/style.css"):
        res = client.get(path)
        assert res.status_code == 200, path


def test_health(client: TestClient) -> None:
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["session_state"] == "idle"


def test_info_lists_targets_and_drivers(client: TestClient) -> None:
    body = client.get("/api/info").json()
    assert body["app"] == "iCloud Browser Console"
    assert "home" in body["targets"]
    assert body["targets"]["home"]["url"] == "https://www.icloud.com/"
    assert {d["name"] for d in body["drivers"]} == {"playwright", "selenium"}


def test_initial_session_is_idle(client: TestClient) -> None:
    body = client.get("/api/session").json()
    assert body["state"] == "idle"
    assert body["snapshot"] is None


# ------------------------------------------------------------------ resolve
def test_resolve_accepts_icloud(client: TestClient) -> None:
    res = client.get("/api/resolve", params={"target": "https://www.icloud.com/photos"})
    assert res.status_code == 200
    assert res.json()["url"] == "https://www.icloud.com/photos"


def test_resolve_rejects_other_hosts(client: TestClient) -> None:
    res = client.get("/api/resolve", params={"target": "https://evil.com/"})
    assert res.status_code == 400
    assert "allowlist" in res.json()["detail"]


# ------------------------------------------------- guarded session endpoints
def test_snapshot_without_session_is_409(client: TestClient) -> None:
    res = client.get("/api/session/snapshot")
    assert res.status_code == 409


def test_screenshot_without_session_is_409(client: TestClient) -> None:
    res = client.get("/api/session/screenshot")
    assert res.status_code == 409


def test_goto_without_session_reports_error_not_crash(client: TestClient) -> None:
    res = client.post("/api/session/goto", json={"target": "home"})
    assert res.status_code == 200
    assert res.json()["state"] == "idle"


def test_stop_without_session_is_safe(client: TestClient) -> None:
    res = client.post("/api/session/stop")
    assert res.status_code == 200
    assert res.json()["state"] == "stopped"


def test_logs_endpoint(client: TestClient) -> None:
    body = client.get("/api/logs").json()
    assert "events" in body
    # The lifespan emits a startup line.
    assert any("ready" in e["message"] for e in body["events"])


def test_sse_replay_stream_is_well_formed(client: TestClient) -> None:
    """`once=1` replays history and closes, so the frames are fully readable."""
    res = client.get("/api/events?once=1")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    assert res.text.rstrip().endswith(": done")
    assert "event: log" in res.text


def test_events_frames_are_json_parseable(client: TestClient) -> None:
    import json

    res = client.get("/api/events?once=1")
    frames = 0
    for line in res.text.splitlines():
        if line.startswith("data: "):
            payload = json.loads(line[len("data: "):])
            assert {"seq", "ts", "level", "message"} <= set(payload)
            frames += 1
    assert frames > 0, "expected at least one replayed log frame"


def test_replay_limit_is_honoured(client: TestClient) -> None:
    res = client.get("/api/events?once=1&limit=2")
    frames = [l for l in res.text.splitlines() if l.startswith("data: ")]
    assert len(frames) <= 2
