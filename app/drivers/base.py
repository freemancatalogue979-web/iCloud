"""Driver interface shared by the Playwright and Selenium backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..events import LogBus
from ..icloud import (
    SELECTOR_GROUPS,
    SIGNIN_BUTTON_SELECTORS,
    classify_page,
    first_present_selector,
    matched_groups,
)
from ..models import LaunchResult, PageSnapshot, PageState

# Structured error codes so the UI can give an actionable hint instead of a
# raw traceback.
ERR_BROWSER_NOT_INSTALLED = "browser_not_installed"
ERR_BROWSER_DEPS_MISSING = "browser_deps_missing"
ERR_WEBDRIVER_NOT_FOUND = "webdriver_not_found"
ERR_LAUNCH_FAILED = "launch_failed"
ERR_NAVIGATION_FAILED = "navigation_failed"
ERR_NOT_RUNNING = "not_running"


@dataclass
class DriverContext:
    """What a driver is handed at construction."""

    headless: bool = False
    profile_dir: str | None = None
    browser_executable: str | None = None
    nav_timeout_ms: int = 30_000
    bus: LogBus | None = None

    def log(self, level: str, message: str, **data: Any) -> None:
        if self.bus is not None:
            self.bus.emit(level, message, driver_data=data or None)


def classify_failure(exc: BaseException) -> tuple[str, str, str]:
    """Map a driver exception to (error_code, message, hint).

    Centralised so both drivers report identical, actionable diagnostics.
    """
    text = str(exc)
    lowered = text.lower()

    if "executable doesn't exist" in lowered or "playwright install" in lowered:
        return (
            ERR_BROWSER_NOT_INSTALLED,
            "No Playwright browser binary found on this machine.",
            "Run: python -m playwright install --with-deps chromium",
        )
    if "missing dependencies" in lowered or "host system is missing" in lowered:
        return (
            ERR_BROWSER_DEPS_MISSING,
            "A browser binary is present but system libraries are missing.",
            "Run: python -m playwright install-deps chromium (needs sudo)",
        )
    if "chromedriver" in lowered and ("not found" in lowered or "no such file" in lowered):
        return (
            ERR_WEBDRIVER_NOT_FOUND,
            "No chromedriver executable was found for Selenium.",
            "Install chromedriver matching your Chrome version, or use the "
            "Playwright driver instead.",
        )
    return ERR_LAUNCH_FAILED, text.strip() or exc.__class__.__name__, ""


class BrowserDriver(ABC):
    """Async interface over an underlying browser.

    Both backends present this same shape so the session manager never needs to
    know which one is active.
    """

    name: str = "base"

    def __init__(self, ctx: DriverContext) -> None:
        self.ctx = ctx
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @abstractmethod
    async def start(self) -> LaunchResult: ...

    @abstractmethod
    async def goto(self, url: str) -> PageSnapshot: ...

    @abstractmethod
    async def snapshot(self) -> PageSnapshot: ...

    @abstractmethod
    async def screenshot(self) -> bytes: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def click(self, selector: str, timeout_ms: int = 10_000) -> bool:
        """Click the first element matching `selector`. True if it happened."""

    # -- shared click helper ----------------------------------------------
    async def click_signin_button(self, timeout_ms: int = 10_000) -> str | None:
        """Click the landing-page Sign In button.

        Probes the candidate selectors in priority order and clicks the first
        visible one. Returns the selector that worked, or None.
        """
        presence = await self.probe_selectors(SIGNIN_BUTTON_SELECTORS)
        selector = first_present_selector(SIGNIN_BUTTON_SELECTORS, presence)
        if selector is None:
            self.ctx.log("warn", "No Sign In button found on this page")
            return None

        self.ctx.log("step", "Clicking Sign In", selector=selector)
        if await self.click(selector, timeout_ms=timeout_ms):
            return selector

        # The custom element can swallow a click on the host; try its button.
        fallback = f"{selector} > button"
        self.ctx.log("step", "Retrying click on inner button", selector=fallback)
        if await self.click(fallback, timeout_ms=timeout_ms):
            return fallback

        self.ctx.log("error", "Sign In button found but click failed", selector=selector)
        return None

    @abstractmethod
    async def probe_selectors(self, selectors: tuple[str, ...] | list[str]) -> set[str]:
        """Return the subset of `selectors` that currently match a visible node."""

    # -- shared helpers ----------------------------------------------------
    @staticmethod
    def build_snapshot(url: str, title: str, presence: dict[str, bool]) -> PageSnapshot:
        """Turn a raw selector sweep into a classified snapshot."""
        matched = matched_groups(presence)
        return PageSnapshot(
            url=url or "",
            title=(title or "").strip(),
            page_state=classify_page(url, matched),
            matched_selectors=sorted(matched),
        )

    @staticmethod
    def selector_groups() -> dict[str, str]:
        return dict(SELECTOR_GROUPS)

    @staticmethod
    def state_is_terminal(state: PageState) -> bool:
        return state in (PageState.SIGNED_IN, PageState.TWO_FACTOR_PROMPT)


@dataclass
class DriverRegistry:
    """Name -> factory, so drivers can be selected by string at runtime."""

    factories: dict[str, Any] = field(default_factory=dict)

    def register(self, name: str, factory: Any) -> None:
        self.factories[name] = factory

    def names(self) -> list[str]:
        return sorted(self.factories)

    def create(self, name: str, ctx: DriverContext) -> BrowserDriver:
        if name not in self.factories:
            raise KeyError(f"unknown driver {name!r}; available: {self.names()}")
        return self.factories[name](ctx)
