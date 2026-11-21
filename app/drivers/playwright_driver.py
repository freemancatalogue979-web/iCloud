"""Primary driver: Playwright (async).

Uses a persistent context by default so the Apple sign-in survives restarts —
you authenticate once, by hand, and later runs reuse the session.
"""

from __future__ import annotations

from typing import Any

from ..models import LaunchResult, PageSnapshot, PageState
from .base import (
    ERR_LAUNCH_FAILED,
    ERR_NAVIGATION_FAILED,
    ERR_NOT_RUNNING,
    BrowserDriver,
    DriverContext,
    classify_failure,
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class PlaywrightDriver(BrowserDriver):
    name = "playwright"

    def __init__(self, ctx: DriverContext) -> None:
        super().__init__(ctx)
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> LaunchResult:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            return LaunchResult(
                ok=False,
                error_code=ERR_LAUNCH_FAILED,
                message="The playwright package is not installed.",
                hint="Run: pip install playwright",
            )

        self.ctx.log("step", "Starting Playwright runtime")
        self._playwright = await async_playwright().start()

        launch_args = ["--disable-blink-features=AutomationControlled"]
        try:
            if self.ctx.profile_dir:
                # Persistent context: cookies + localStorage survive restarts.
                self.ctx.log("step", "Launching persistent Chromium context",
                             profile=self.ctx.profile_dir)
                self._context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=self.ctx.profile_dir,
                    headless=self.ctx.headless,
                    args=launch_args,
                    user_agent=USER_AGENT,
                    executable_path=self.ctx.browser_executable,
                    viewport={"width": 1440, "height": 900},
                    locale="en-US",
                    timezone_id="Africa/Lagos",
                )
                self._page = self._context.pages[0] if self._context.pages else (
                    await self._context.new_page()
                )
            else:
                self.ctx.log("step", "Launching ephemeral Chromium")
                self._browser = await self._playwright.chromium.launch(
                    headless=self.ctx.headless,
                    args=launch_args,
                    executable_path=self.ctx.browser_executable,
                )
                self._context = await self._browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1440, "height": 900},
                    locale="en-US",
                    timezone_id="Africa/Lagos",
                )
                self._page = await self._context.new_page()

            self._context.set_default_navigation_timeout(self.ctx.nav_timeout_ms)
            self._context.set_default_timeout(self.ctx.nav_timeout_ms)
            self._running = True

            version = getattr(self._browser, "version", None)
            if callable(version):
                version = await version()
            self.ctx.log("info", "Playwright Chromium ready",
                         headless=self.ctx.headless, version=version or "bundled")

            return LaunchResult(
                ok=True,
                message="Playwright Chromium launched.",
                browser="chromium",
                version=str(version or "bundled"),
            )

        except Exception as exc:
            code, message, hint = classify_failure(exc)
            self.ctx.log("error", f"Playwright launch failed: {message}", error_code=code)
            await self.stop()
            return LaunchResult(ok=False, error_code=code, message=message, hint=hint)

    async def stop(self) -> None:
        for closer, label in (
            (self._context, "context"),
            (self._browser, "browser"),
        ):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:
                    pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._page = self._context = self._browser = self._playwright = None
        was_running = self._running
        self._running = False
        if was_running:
            self.ctx.log("step", "Playwright stopped")

    # -- navigation --------------------------------------------------------
    async def goto(self, url: str) -> PageSnapshot:
        if not self._running or self._page is None:
            return PageSnapshot(url=url, title="", page_state=PageState.UNKNOWN)

        self.ctx.log("step", "Navigating", url=url)
        status: int | None = None
        try:
            response = await self._page.goto(url, wait_until="domcontentloaded")
            status = response.status if response else None
        except Exception as exc:
            code, message, _ = classify_failure(exc)
            self.ctx.log("error", f"Navigation failed: {message}",
                         url=url, error_code=code)
            snapshot = await self.snapshot()
            snapshot.url = snapshot.url or url
            return snapshot

        try:
            await self._page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass  # iCloud keeps polling; networkidle is best-effort

        snapshot = await self.snapshot()
        self.ctx.log("info", "Page loaded",
                     url=snapshot.url, title=snapshot.title,
                     status=status, page_state=snapshot.page_state.value)
        return snapshot

    async def snapshot(self) -> PageSnapshot:
        if not self._running or self._page is None:
            raise RuntimeError(ERR_NOT_RUNNING)

        url = self._page.url or ""
        try:
            title = await self._page.title()
        except Exception:
            title = ""

        presence: dict[str, bool] = {}
        for key, selector in self.selector_groups().items():
            try:
                locator = self._page.locator(selector).first
                presence[key] = await locator.count() > 0 and await locator.is_visible()
            except Exception:
                presence[key] = False

        return self.build_snapshot(url, title, presence)

    async def screenshot(self) -> bytes:
        if not self._running or self._page is None:
            raise RuntimeError(ERR_NOT_RUNNING)
        return await self._page.screenshot(full_page=False)

    # -- interaction -------------------------------------------------------
    async def probe_selectors(self, selectors) -> set[str]:
        """Return the subset of selectors that match a visible element."""
        found: set[str] = set()
        if not self._running or self._page is None:
            return found
        for selector in selectors:
            try:
                locator = self._page.locator(selector).first
                if await locator.count() > 0 and await locator.is_visible():
                    found.add(selector)
            except Exception:
                continue
        return found

    async def click(self, selector: str, timeout_ms: int = 10_000) -> bool:
        if not self._running or self._page is None:
            return False
        try:
            locator = self._page.locator(selector).first
            await locator.scroll_into_view_if_needed(timeout=timeout_ms)
            await locator.click(timeout=timeout_ms)
            self.ctx.log("info", "Clicked", selector=selector)
            return True
        except Exception as exc:
            self.ctx.log("warn", f"Click failed for {selector}: {exc}")
            return False

    # -- convenience -------------------------------------------------------
    async def wait_for_selector(self, selector: str, timeout_ms: int = 10_000) -> bool:
        if not self._running or self._page is None:
            return False
        try:
            await self._page.wait_for_selector(selector, timeout=timeout_ms)
            return True
        except Exception:
            return False

    async def current_url(self) -> str:
        if self._page is None:
            return ""
        return self._page.url or ""
