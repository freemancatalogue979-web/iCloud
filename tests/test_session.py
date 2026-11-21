"""SessionManager state machine, driven by an in-process fake driver.

This exercises the real manager code — target validation, state transitions,
sign-in detection and refusal paths — without needing a browser binary.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.drivers.base import BrowserDriver, DriverContext
from app.events import LogBus
from app.models import LaunchResult, PageSnapshot, PageState, SessionState
from app.session import SessionManager


class FakeDriver(BrowserDriver):
    """Configurable stand-in that records what it was asked to do."""

    name = "fake"

    def __init__(self, ctx: DriverContext, *, launch_ok: bool = True,
                 snapshots: list[PageSnapshot] | None = None) -> None:
        super().__init__(ctx)
        self.launch_ok = launch_ok
        self.launch_message = "fake launch failure" if not launch_ok else ""
        self._snapshots = snapshots or [
            PageSnapshot(url="https://www.icloud.com/", title="iCloud",
                         page_state=PageState.SIGNED_IN, matched_selectors=["app_launcher"])
        ]
        self.visited: list[str] = []
        self.stopped = False

    async def start(self) -> LaunchResult:
        if not self.launch_ok:
            return LaunchResult(ok=False, error_code="launch_failed",
                                message=self.launch_message, hint="check your install")
        self._running = True
        return LaunchResult(ok=True, message="ok", browser="fake", version="0")

    async def goto(self, url: str) -> PageSnapshot:
        self.visited.append(url)
        snap = self._snapshots.pop(0) if len(self._snapshots) > 1 else self._snapshots[0]
        snap.url = snap.url or url
        return snap

    async def snapshot(self) -> PageSnapshot:
        return self._snapshots[0]

    async def screenshot(self) -> bytes:
        return b"\x89PNG fake"

    async def stop(self) -> None:
        self._running = False
        self.stopped = True


def make_manager(settings: Settings, driver_factory) -> tuple[SessionManager, LogBus]:
    bus = LogBus()
    manager = SessionManager(settings=settings, bus=bus)
    manager.registry.register("fake", driver_factory)
    return manager, bus


# ------------------------------------------------------------------ happy path
async def test_start_navigates_to_icloud_home(settings: Settings) -> None:
    manager, _ = make_manager(settings, lambda ctx: FakeDriver(ctx))
    status = await manager.start(driver="fake", wait_for_signin=False)

    assert status.state is SessionState.READY
    assert status.driver == "fake"
    assert status.target == "home"
    assert status.snapshot is not None
    assert status.snapshot.url == "https://www.icloud.com/"


async def test_start_uses_the_resolved_target(settings: Settings) -> None:
    holder: dict = {}

    def factory(ctx: DriverContext) -> FakeDriver:
        driver = FakeDriver(ctx)
        holder["driver"] = driver
        return driver

    manager, _ = make_manager(settings, factory)
    await manager.start(driver="fake", target="photos", wait_for_signin=False)
    assert holder["driver"].visited == ["https://www.icloud.com/photos"]


# ------------------------------------------------------- disallowed navigation
async def test_start_refuses_a_non_icloud_target(settings: Settings) -> None:
    manager, bus = make_manager(settings, lambda ctx: FakeDriver(ctx))
    status = await manager.start(driver="fake", target="https://evil.com/")

    assert status.state is SessionState.ERROR
    assert "allowlist" in (status.error or "")
    assert any("Refused to navigate" in e.message for e in bus.history())


async def test_goto_refuses_a_non_icloud_target(settings: Settings) -> None:
    manager, _ = make_manager(settings, lambda ctx: FakeDriver(ctx))
    await manager.start(driver="fake", wait_for_signin=False)

    status = await manager.goto("https://evil.com/")
    assert "allowlist" in (status.error or "")


# ------------------------------------------------------------- sign-in flow
async def test_sign_in_page_parks_the_session(settings: Settings) -> None:
    signin = PageSnapshot(url="https://idmsa.apple.com/appleauth/auth/signin",
                          title="Sign in", page_state=PageState.SIGN_IN_REQUIRED,
                          matched_selectors=["account_entry"])
    manager, _ = make_manager(
        settings, lambda ctx: FakeDriver(ctx, snapshots=[signin])
    )
    status = await manager.start(driver="fake", wait_for_signin=False)

    assert status.state is SessionState.AWAITING_SIGNIN
    assert status.hint and "credentials" in status.hint


async def test_two_factor_prompt_parks_the_session(settings: Settings) -> None:
    tfa = PageSnapshot(url="https://idmsa.apple.com/x", title="Verify",
                       page_state=PageState.TWO_FACTOR_PROMPT,
                       matched_selectors=["two_factor"])
    manager, _ = make_manager(settings, lambda ctx: FakeDriver(ctx, snapshots=[tfa]))
    status = await manager.start(driver="fake", wait_for_signin=False)

    assert status.state is SessionState.AWAITING_SIGNIN
    assert "two-factor" in (status.hint or "").lower()


# ------------------------------------------------------------ failure paths
async def test_failed_launch_surfaces_error_and_hint(settings: Settings) -> None:
    manager, _ = make_manager(
        settings, lambda ctx: FakeDriver(ctx, launch_ok=False)
    )
    status = await manager.start(driver="fake", wait_for_signin=False)

    assert status.state is SessionState.ERROR
    assert status.error == "fake launch failure"
    assert status.hint == "check your install"


async def test_unknown_driver_is_rejected(settings: Settings) -> None:
    manager, _ = make_manager(settings, lambda ctx: FakeDriver(ctx))
    status = await manager.start(driver="nosuch", wait_for_signin=False)
    assert status.state is SessionState.ERROR
    assert "unknown driver" in (status.error or "")


# --------------------------------------------------------------- lifecycle
async def test_stop_marks_stopped_and_closes_driver(settings: Settings) -> None:
    holder: dict = {}

    def factory(ctx: DriverContext) -> FakeDriver:
        driver = FakeDriver(ctx)
        holder["driver"] = driver
        return driver

    manager, _ = make_manager(settings, factory)
    await manager.start(driver="fake", wait_for_signin=False)
    status = await manager.stop()

    assert status.state is SessionState.STOPPED
    assert holder["driver"].stopped is True


async def test_double_start_is_ignored_while_running(settings: Settings) -> None:
    holder: dict = {}

    def factory(ctx: DriverContext) -> FakeDriver:
        driver = FakeDriver(ctx)
        holder["driver"] = driver
        return driver

    manager, bus = make_manager(settings, factory)
    await manager.start(driver="fake", wait_for_signin=False)
    first = holder["driver"]

    await manager.start(driver="fake", wait_for_signin=False)
    # Second call must not have built a new driver.
    assert holder["driver"] is first
    assert any("already running" in e.message for e in bus.history())


async def test_goto_without_a_session_is_a_no_op(settings: Settings) -> None:
    manager, bus = make_manager(settings, lambda ctx: FakeDriver(ctx))
    status = await manager.goto("home")
    assert status.state is SessionState.IDLE
    assert any("No active browser session" in e.message for e in bus.history())


async def test_screenshot_without_a_session_returns_none(settings: Settings) -> None:
    manager, _ = make_manager(settings, lambda ctx: FakeDriver(ctx))
    assert await manager.screenshot() is None


async def test_screenshot_counts_captures(settings: Settings) -> None:
    manager, _ = make_manager(settings, lambda ctx: FakeDriver(ctx))
    await manager.start(driver="fake", wait_for_signin=False)
    assert await manager.screenshot() == b"\x89PNG fake"
    assert manager.session.status().screenshot_count == 1


async def test_every_state_transition_is_logged(settings: Settings) -> None:
    manager, bus = make_manager(settings, lambda ctx: FakeDriver(ctx))
    await manager.start(driver="fake", wait_for_signin=False)
    states = [e.data.get("state") for e in bus.history() if e.level == "state"]
    assert states == ["starting", "running", "ready"]
