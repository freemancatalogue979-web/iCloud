"""HTTP API and the HTML console."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..config import Settings
from ..events import LogBus
from ..models import DriverInfo, ServiceInfo, StartRequest, GotoRequest
from ..session import SessionManager
from ..targets import all_targets, resolve_target
from ..targets import TargetNotAllowed

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

api = APIRouter(prefix="/api")


# --------------------------------------------------------------------------
# Driver availability probing
# --------------------------------------------------------------------------
def _playwright_browser_present() -> tuple[bool, str]:
    """Check for a downloaded Playwright Chromium without launching it."""
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
                 or Path.home() / ".cache" / "ms-playwright")
    if not cache.is_dir():
        return False, f"no browser cache at {cache}"
    chromes = sorted(p.name for p in cache.iterdir() if p.name.startswith("chromium"))
    if not chromes:
        return False, "cache exists but holds no chromium build"
    return True, ", ".join(chromes)


def _selenium_driver_present() -> tuple[bool, str]:
    """Look for chromedriver / geckodriver on PATH."""
    found = [n for n in ("chromedriver", "geckodriver", "msedgedriver") if shutil.which(n)]
    if found:
        return True, ", ".join(found)
    return False, "no webdriver on PATH"


def _package_installed(module: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def driver_infos() -> list[DriverInfo]:
    pw_pkg = _package_installed("playwright")
    pw_browser, pw_detail = _playwright_browser_present()
    se_pkg = _package_installed("selenium")
    se_browser, se_detail = _selenium_driver_present()

    return [
        DriverInfo(
            name="playwright",
            package_installed=pw_pkg,
            browser_available=pw_pkg and pw_browser,
            detail=pw_detail if not pw_browser else "ready",
        ),
        DriverInfo(
            name="selenium",
            package_installed=se_pkg,
            browser_available=se_pkg and se_browser,
            detail=se_detail if not se_browser else "ready",
        ),
    ]


# --------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------
def build_page_router(manager: SessionManager, settings: Settings):
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "app_name": settings.app_name,
                "version": __version__,
                "default_driver": settings.driver,
                "headless": settings.headless,
            },
        )

    return router


# --------------------------------------------------------------------------
# API factory
# --------------------------------------------------------------------------
def build_api_router(manager: SessionManager, settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "session_state": manager.session.state.value,
        }

    @router.get("/info", response_model=ServiceInfo)
    async def info() -> ServiceInfo:
        return ServiceInfo(
            app=settings.app_name,
            version=__version__,
            default_driver=settings.driver,
            headless=settings.headless,
            profile_dir=str(settings.profile_dir),
            targets=all_targets(),
            drivers=driver_infos(),
            network={"note": "Navigation is restricted to icloud.com hosts."},
        )

    @router.post("/session/start")
    async def start_session(payload: StartRequest) -> dict:
        status = await manager.start(
            driver=payload.driver,
            headless=payload.headless,
            target=payload.target,
            wait_for_signin=payload.wait_for_signin,
        )
        return status.model_dump(mode="json")

    @router.get("/session")
    async def get_session() -> dict:
        return manager.session.status().model_dump(mode="json")

    @router.post("/session/goto")
    async def goto(payload: GotoRequest) -> dict:
        status = await manager.goto(payload.target)
        return status.model_dump(mode="json")

    @router.get("/session/snapshot")
    async def snapshot() -> dict:
        snap = await manager.snapshot()
        if snap is None:
            raise HTTPException(status_code=409, detail="No active browser session.")
        return snap.model_dump(mode="json")

    @router.get("/session/screenshot")
    async def screenshot() -> Response:
        png = await manager.screenshot()
        if png is None:
            raise HTTPException(status_code=409, detail="No active browser session.")
        return Response(content=png, media_type="image/png")

    @router.post("/session/stop")
    async def stop_session() -> dict:
        status = await manager.stop()
        return status.model_dump(mode="json")

    @router.get("/events")
    async def events(request: Request, once: bool = False, limit: int = 80) -> StreamingResponse:
        """Server-Sent Events stream of the log bus.

        `once=1` replays recent history and closes — used for catch-up on
        reconnect, and it terminates deterministically so it is testable.
        """
        bus: LogBus = manager.bus

        async def replay() -> AsyncIterator[str]:
            for event in bus.history(limit=limit):
                yield event.sse()
            yield ": done\n\n"

        async def stream() -> AsyncIterator[str]:
            queue = bus.subscribe()
            try:
                # Replay recent history so a reconnecting client is caught up.
                for event in bus.history(limit=limit):
                    yield event.sse()
                yield ": connected\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                        yield event.sse()
                    except asyncio.TimeoutError:
                        # Heartbeat; also lets us notice a dead client.
                        yield ": keepalive\n\n"
                        if await request.is_disconnected():
                            break
            finally:
                bus.unsubscribe(queue)

        return StreamingResponse(
            replay() if once else stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @router.get("/logs")
    async def logs(since: int = 0, limit: int = 200) -> dict:
        items = manager.bus.history(since_seq=since, limit=limit)
        return {
            "events": [
                {
                    "seq": e.seq,
                    "ts": e.ts,
                    "level": e.level,
                    "message": e.message,
                    "data": e.data or {},
                }
                for e in items
            ]
        }

    @router.get("/resolve")
    async def resolve(target: str) -> dict:
        try:
            return resolve_target(target).model_dump(mode="json")
        except TargetNotAllowed as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
