"""Application entry point.

    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Settings, get_settings
from .events import LogBus
from .session import SessionManager
from .web.routes import build_api_router, build_page_router

WEB_DIR = Path(__file__).resolve().parent / "web"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    bus = LogBus()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Bind the bus to the running loop so driver threads can publish safely.
        bus.bind_loop(asyncio.get_running_loop())
        settings.profile_dir.mkdir(parents=True, exist_ok=True)
        bus.info(f"{settings.app_name} v{__version__} ready",
                 driver=settings.driver, headless=settings.headless)
        bus.info("No browser has been launched yet — press Start in the console.")
        try:
            yield
        finally:
            await manager.stop()
            bus.info("Shutdown complete")

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Drives a real browser to icloud.com. Playwright is the primary "
            "driver, Selenium the fallback. Sign-in is always performed by a "
            "human in the opened window — the app never handles Apple "
            "credentials."
        ),
        lifespan=lifespan,
    )

    manager = SessionManager(settings=settings, bus=bus)
    app.state.settings = settings
    app.state.bus = bus
    app.state.manager = manager

    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
    app.include_router(build_page_router(manager, settings))
    app.include_router(build_api_router(manager, settings))

    return app


app = create_app()


def main() -> None:
    """CLI: python -m app.main"""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
