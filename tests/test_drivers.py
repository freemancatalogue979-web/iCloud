"""Driver-layer tests.

The pure parts (failure classification, snapshot building, registry) are tested
directly. The launch tests make a REAL launch attempt against this machine and
assert the structured error contract — that exercises the actual `start()`
error path rather than a stand-in.
"""

from __future__ import annotations

import pytest

from app.drivers import PlaywrightDriver, SeleniumDriver, build_registry
from app.drivers.base import (
    ERR_BROWSER_DEPS_MISSING,
    ERR_BROWSER_NOT_INSTALLED,
    ERR_LAUNCH_FAILED,
    ERR_WEBDRIVER_NOT_FOUND,
    DriverContext,
    classify_failure,
)
from app.events import LogBus
from app.models import PageState


# --------------------------------------------------- classify_failure (pure)
@pytest.mark.parametrize(
    "message,expected_code",
    [
        ("Executable doesn't exist at /root/.cache/ms-playwright/chromium-1148/chrome-linux/chrome",
         ERR_BROWSER_NOT_INSTALLED),
        ("Please run: playwright install", ERR_BROWSER_NOT_INSTALLED),
        ("Host system is missing dependencies to run browsers", ERR_BROWSER_DEPS_MISSING),
        ("chrome: error while loading shared libraries: missing dependencies",
         ERR_BROWSER_DEPS_MISSING),
        (" chromedriver not found in PATH", ERR_WEBDRIVER_NOT_FOUND),
        ("something exploded", ERR_LAUNCH_FAILED),
    ],
)
def test_classify_failure_maps_messages(message: str, expected_code: str) -> None:
    code, text, _hint = classify_failure(RuntimeError(message))
    assert code == expected_code
    assert text


def test_classify_failure_falls_back_to_exception_name_when_message_empty() -> None:
    code, text, _ = classify_failure(RuntimeError(""))
    assert code == ERR_LAUNCH_FAILED
    assert text == "RuntimeError"


def test_missing_browser_message_carries_install_hint() -> None:
    code, _, hint = classify_failure(
        RuntimeError("Executable doesn't exist. Please run playwright install")
    )
    assert code == ERR_BROWSER_NOT_INSTALLED
    assert "playwright install" in hint


# ----------------------------------------------------- snapshot construction
def test_build_snapshot_classifies_from_presence() -> None:
    snap = PlaywrightDriver.build_snapshot(
        "https://idmsa.apple.com/appleauth/auth/signin",
        "  Sign in to iCloud  ",
        {"account_entry": True, "password_entry": False},
    )
    assert snap.page_state is PageState.SIGN_IN_REQUIRED
    assert snap.title == "Sign in to iCloud"
    assert snap.matched_selectors == ["account_entry"]


def test_build_snapshot_signed_in() -> None:
    snap = PlaywrightDriver.build_snapshot(
        "https://www.icloud.com/photos", "Photos", {"app_launcher": True}
    )
    assert snap.page_state is PageState.SIGNED_IN


# --------------------------------------------------------------- registry
def test_registry_exposes_both_drivers() -> None:
    registry = build_registry()
    assert registry.names() == ["playwright", "selenium"]
    assert registry.create("playwright", DriverContext()).name == "playwright"
    assert registry.create("selenium", DriverContext()).name == "selenium"


def test_registry_rejects_unknown_driver() -> None:
    with pytest.raises(KeyError, match="unknown driver"):
        build_registry().create("puppeteer", DriverContext())


def test_driver_logs_reach_the_bus() -> None:
    bus = LogBus()
    driver = PlaywrightDriver(DriverContext(bus=bus))
    driver.ctx.log("info", "hello", key="value")
    events = bus.history()
    assert len(events) == 1
    assert events[0].message == "hello"
    assert events[0].data == {"driver_data": {"key": "value"}}


# --------------------------------------------- REAL launch attempts
# These call into the genuine driver code. With no browser binary installed
# (the case in a bare container) they must return a structured, actionable
# failure instead of raising.
@pytest.mark.asyncio
async def test_playwright_start_reports_unavailable_browser(tmp_path) -> None:
    bus = LogBus()
    driver = PlaywrightDriver(DriverContext(headless=True, bus=bus))
    result = await driver.start()

    if result.ok:
        # A browser really is installed here — make sure the happy path works.
        assert result.browser == "chromium"
        assert driver.running is True
        await driver.stop()
        assert driver.running is False
        return

    assert result.ok is False
    assert result.error_code in (
        ERR_BROWSER_NOT_INSTALLED, ERR_BROWSER_DEPS_MISSING, ERR_LAUNCH_FAILED
    )
    assert result.message
    assert driver.running is False
    assert any(e.level == "error" for e in bus.history())


@pytest.mark.asyncio
async def test_selenium_start_reports_unavailable_webdriver(tmp_path) -> None:
    bus = LogBus()
    driver = SeleniumDriver(DriverContext(headless=True, bus=bus))
    result = await driver.start()

    if result.ok:
        assert driver.running is True
        await driver.stop()
        return

    assert result.ok is False
    assert result.error_code in (
        ERR_WEBDRIVER_NOT_FOUND, ERR_LAUNCH_FAILED, ERR_BROWSER_DEPS_MISSING
    )
    assert driver.running is False


@pytest.mark.asyncio
async def test_playwright_goto_without_start_returns_unknown_not_raises() -> None:
    driver = PlaywrightDriver(DriverContext(headless=True))
    snap = await driver.goto("https://www.icloud.com/")
    assert snap.page_state is PageState.UNKNOWN
    assert snap.url == "https://www.icloud.com/"


@pytest.mark.asyncio
async def test_playwright_snapshot_without_start_raises_not_running() -> None:
    driver = PlaywrightDriver(DriverContext(headless=True))
    with pytest.raises(RuntimeError):
        await driver.snapshot()
