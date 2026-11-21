"""Fallback driver: Selenium WebDriver (sync API, bridged to async).

Selenium is fully synchronous, so every call is pushed onto a worker thread
with `asyncio.to_thread`. That keeps the session manager's interface identical
to the Playwright driver's.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..models import LaunchResult, PageSnapshot, PageState
from .base import (
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


class SeleniumDriver(BrowserDriver):
    name = "selenium"

    def __init__(self, ctx: DriverContext) -> None:
        super().__init__(ctx)
        self._driver: Any = None
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> LaunchResult:
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            return LaunchResult(
                ok=False,
                error_code="launch_failed",
                message="The selenium package is not installed.",
                hint="Run: pip install selenium",
            )

        self.ctx.log("step", "Configuring Chrome options", headless=self.ctx.headless)
        options = Options()
        if self.ctx.headless:
            options.add_argument("--headless=new")
        if self.ctx.profile_dir:
            options.add_argument(f"--user-data-dir={self.ctx.profile_dir}")
        if self.ctx.browser_executable:
            options.binary_location = self.ctx.browser_executable
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--window-size=1440,900")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])

        def _launch() -> Any:
            service = Service()  # resolves chromedriver from PATH
            return webdriver.Chrome(service=service, options=options)

        try:
            self._driver = await asyncio.to_thread(_launch)
        except Exception as exc:
            code, message, hint = classify_failure(exc)
            self.ctx.log("error", f"Selenium launch failed: {message}", error_code=code)
            return LaunchResult(ok=False, error_code=code, message=message, hint=hint)

        try:
            self._driver.set_page_load_timeout(self.ctx.nav_timeout_ms / 1000.0)
            caps = self._driver.capabilities or {}
            version = str(caps.get("browserVersion", "unknown"))
        except Exception:
            version = "unknown"

        self._running = True
        self.ctx.log("info", "Selenium Chrome ready",
                     headless=self.ctx.headless, version=version)
        return LaunchResult(
            ok=True, message="Selenium Chrome launched.", browser="chrome", version=version
        )

    async def stop(self) -> None:
        driver, self._driver = self._driver, None
        if driver is not None:
            try:
                await asyncio.to_thread(driver.quit)
            except Exception:
                pass
        was_running = self._running
        self._running = False
        if was_running:
            self.ctx.log("step", "Selenium stopped")

    # -- navigation --------------------------------------------------------
    async def goto(self, url: str) -> PageSnapshot:
        async with self._lock:
            if not self._running or self._driver is None:
                return PageSnapshot(url=url, title="", page_state=PageState.UNKNOWN)

            self.ctx.log("step", "Navigating", url=url)

            def _go() -> None:
                self._driver.get(url)

            try:
                await asyncio.to_thread(_go)
            except Exception as exc:
                code, message, _ = classify_failure(exc)
                self.ctx.log("error", f"Navigation failed: {message}",
                             url=url, error_code=code)

            snapshot = await self.snapshot()
            snapshot.url = snapshot.url or url
            self.ctx.log("info", "Page loaded",
                         url=snapshot.url, title=snapshot.title,
                         page_state=snapshot.page_state.value)
            return snapshot

    async def snapshot(self) -> PageSnapshot:
        if not self._running or self._driver is None:
            raise RuntimeError(ERR_NOT_RUNNING)

        from selenium.common.exceptions import WebDriverException
        from selenium.webdriver.common.by import By

        def _collect() -> tuple[str, str, dict[str, bool]]:
            url = self._driver.current_url or ""
            try:
                title = self._driver.title or ""
            except WebDriverException:
                title = ""
            presence: dict[str, bool] = {}
            for key, selector in self.selector_groups().items():
                presence[key] = False
                for css in (part.strip() for part in selector.split(",")):
                    if not css:
                        continue
                    try:
                        if self._driver.find_elements(By.CSS_SELECTOR, css):
                            presence[key] = True
                            break
                    except WebDriverException:
                        continue
            return url, title, presence

        url, title, presence = await asyncio.to_thread(_collect)
        return self.build_snapshot(url, title, presence)

    async def screenshot(self) -> bytes:
        if not self._running or self._driver is None:
            raise RuntimeError(ERR_NOT_RUNNING)
        png = await asyncio.to_thread(self._driver.get_screenshot_as_png)
        return png

    # -- interaction -------------------------------------------------------
    async def probe_selectors(self, selectors) -> set[str]:
        from selenium.common.exceptions import WebDriverException
        from selenium.webdriver.common.by import By

        def _probe() -> set[str]:
            found: set[str] = set()
            for selector in selectors:
                for css in (part.strip() for part in selector.split(",")):
                    if not css:
                        continue
                    try:
                        elements = self._driver.find_elements(By.CSS_SELECTOR, css)
                    except WebDriverException:
                        continue
                    if any(el.is_displayed() for el in elements):
                        found.add(selector)
                        break
            return found

        if not self._running or self._driver is None:
            return set()
        return await asyncio.to_thread(_probe)

    async def click(self, selector: str, timeout_ms: int = 10_000) -> bool:
        if not self._running or self._driver is None:
            return False

        from selenium.common.exceptions import WebDriverException
        from selenium.webdriver.common.by import By

        def _click() -> bool:
            for css in (part.strip() for part in selector.split(",")):
                if not css:
                    continue
                try:
                    elements = self._driver.find_elements(By.CSS_SELECTOR, css)
                except WebDriverException:
                    continue
                for element in elements:
                    if not element.is_displayed():
                        continue
                    try:
                        element.click()
                        return True
                    except WebDriverException:
                        # Some custom elements need a JS-level dispatch.
                        try:
                            self._driver.execute_script("arguments[0].click();", element)
                            return True
                        except WebDriverException:
                            continue
            return False

        ok = await asyncio.to_thread(_click)
        if ok:
            self.ctx.log("info", "Clicked", selector=selector)
        else:
            self.ctx.log("warn", f"Click failed for {selector}")
        return ok
