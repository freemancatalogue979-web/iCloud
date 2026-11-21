"""Client server — localhost:9002.

    python -m client.server

Serves exactly one HTML page at `/`. The page embeds an iframe and opens a
WebSocket; the moment the socket connects, the flow runs: launch a browser,
open icloud.com, click Sign In. Progress streams back over the same socket.

Endpoints
    GET  /           the client page (iframe + WebSocket)
    GET  /view       iframe content: live browser screenshot + state
    GET  /shot.png   latest captured screenshot
    GET  /health     liveness
    WS   /ws         commands in, events/status/flow steps out
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.websockets import WebSocketState

from app.config import Settings, get_settings
from app.events import Event, LogBus
from app.session import SessionManager
from client.flow import FlowResult, StepResult, run_flow

from . import DEFAULT_PORT, __version__

STATIC_DIR = Path(__file__).resolve().parent / "static"


class ShotStore:
    """Holds the most recent screenshot for the iframe to poll."""

    def __init__(self) -> None:
        self.png: bytes | None = None
        self.nonce: int = 0

    def put(self, png: bytes) -> None:
        self.png = png
        self.nonce += 1


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    bus = LogBus()
    manager = SessionManager(settings=settings, bus=bus)
    shots = ShotStore()
    flow_lock = asyncio.Lock()
    flow_task: dict[str, asyncio.Task[FlowResult] | None] = {"task": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bus.bind_loop(asyncio.get_running_loop())
        settings.profile_dir.mkdir(parents=True, exist_ok=True)
        bus.info(f"Client server v{__version__} ready")
        bus.info("Waiting for a page visit to start the flow.")
        try:
            yield
        finally:
            task = flow_task["task"]
            if task is not None and not task.done():
                task.cancel()
            await manager.stop()

    app = FastAPI(
        title="iCloud Client",
        version=__version__,
        description="Single-page client: iframe + WebSocket on localhost:9002.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.bus = bus
    app.state.manager = manager
    app.state.shots = shots

    # ---------------------------------------------------------------- pages
    @app.get("/", response_class=HTMLResponse)
    async def index() -> FileResponse:
        """The one and only HTML page in the project."""
        return FileResponse(STATIC_DIR / "client.html", media_type="text/html")

    @app.get("/view", response_class=HTMLResponse)
    async def view() -> FileResponse:
        """Content for the iframe: the live browser view."""
        return FileResponse(STATIC_DIR / "view.html", media_type="text/html")

    @app.get("/shot.png")
    async def shot() -> Response:
        if shots.png is None:
            return Response(status_code=404, content=b"no screenshot yet")
        return Response(
            content=shots.png,
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "session_state": manager.session.state.value,
            "has_screenshot": shots.png is not None,
            "shot_nonce": shots.nonce,
        }

    # ------------------------------------------------------------ websocket
    async def send_json(ws: WebSocket, payload: dict[str, Any]) -> bool:
        if ws.client_state is not WebSocketState.CONNECTED:
            return False
        try:
            await ws.send_text(json.dumps(payload, default=str))
            return True
        except Exception:
            return False

    def event_payload(event: Event) -> dict[str, Any]:
        return {
            "type": "event",
            "seq": event.seq,
            "ts": event.ts,
            "level": event.level,
            "message": event.message,
            "data": event.data or {},
        }

    def status_payload() -> dict[str, Any]:
        status = manager.session.status().model_dump(mode="json")
        return {
            "type": "status",
            "state": status["state"],
            "driver": status["driver"],
            "target": status["target"],
            "snapshot": status["snapshot"],
            "error": status["error"],
            "hint": status["hint"],
            "screenshot_count": status["screenshot_count"],
            "shot_nonce": shots.nonce,
        }

    async def bridge(ws: WebSocket) -> None:
        """Forward LogBus events to this socket until it closes."""
        queue = bus.subscribe()
        try:
            while True:
                event = await queue.get()
                if not await send_json(ws, event_payload(event)):
                    break
        finally:
            bus.unsubscribe(queue)

    async def do_flow(ws: WebSocket) -> None:
        """Run the visit flow once, streaming each step."""
        if flow_lock.locked():
            await send_json(ws, {"type": "flow", "note": "flow already running"})
            return

        async with flow_lock:
            async def on_step(step: StepResult) -> None:
                await send_json(ws, {
                    "type": "flow",
                    "step": step.step.value,
                    "ok": step.ok,
                    "detail": step.detail,
                })
                await send_json(ws, status_payload())

            await send_json(ws, {"type": "flow", "step": "begin", "ok": True})
            result = await run_flow(
                manager,
                target="home",
                driver=settings.driver,
                headless=settings.headless,
                on_step=on_step,
            )

            # Refresh the iframe image if the flow captured one.
            png = await manager.screenshot()
            if png:
                shots.put(png)

            await send_json(ws, {
                "type": "flow_done",
                "ok": result.ok,
                "steps": result.step_names(),
                "url": result.url,
                "error": result.error,
                "hint": result.hint,
                "final_state": result.final_state,
                "shot_nonce": shots.nonce,
            })
            await send_json(ws, status_payload())

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        bridge_task = asyncio.create_task(bridge(ws))

        try:
            await send_json(ws, {
                "type": "hello",
                "version": __version__,
                "driver": settings.driver,
                "headless": settings.headless,
                "message": "connected — visiting this page starts the flow",
            })
            # Catch the visitor up on anything already logged.
            for event in bus.history(limit=100):
                await send_json(ws, event_payload(event))
            await send_json(ws, status_payload())

            while True:
                raw = await ws.receive_text()
                try:
                    message = json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError:
                    await send_json(ws, {"type": "error", "message": "invalid JSON"})
                    continue

                command = (message.get("cmd") or "").strip()

                if command == "start":
                    task = flow_task["task"]
                    if task is not None and not task.done():
                        await send_json(ws, {"type": "error",
                                             "message": "flow already running"})
                        continue
                    flow_task["task"] = asyncio.create_task(do_flow(ws))

                elif command == "click_signin":
                    status = await manager.click_signin()
                    await send_json(ws, status_payload())
                    if status.error:
                        await send_json(ws, {"type": "error",
                                             "message": status.error})

                elif command == "stop":
                    await manager.stop()
                    await send_json(ws, status_payload())

                elif command == "refresh":
                    png = await manager.screenshot()
                    if png:
                        shots.put(png)
                    await send_json(ws, status_payload())

                elif command == "ping":
                    await send_json(ws, {"type": "pong"})

                else:
                    await send_json(ws, {"type": "error",
                                         "message": f"unknown cmd {command!r}"})

        except WebSocketDisconnect:
            pass
        except Exception as exc:  # pragma: no cover - defensive
            await send_json(ws, {"type": "error", "message": str(exc)})
        finally:
            bridge_task.cancel()
            task = flow_task["task"]
            if task is not None and not task.done():
                task.cancel()

    return app


app = create_app()


def main() -> None:
    """CLI: python -m client.server  (defaults to localhost:9002)"""
    import os

    import uvicorn

    port = int(os.environ.get("ICLOUD_CLIENT_PORT", DEFAULT_PORT))
    uvicorn.run("client.server:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
