"""Session manager: owns one browser session and its state machine.

One session at a time, because one human signs in at a time. The manager never
sees credentials — when the page reports SIGN_IN_REQUIRED it parks the session
in AWAITING_SIGNIN and polls until a human finishes in the real window.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .config import Settings
from .drivers import BrowserDriver, DriverContext, build_registry
from .events import LogBus
from .models import (
    LaunchResult,
    PageSnapshot,
    PageState,
    SessionState,
    SessionStatus,
    utcnow_iso,
)
from .targets import Target, TargetNotAllowed, resolve_target

POLL_INTERVAL_S = 2.0

# States in which a browser is already owned by a session, so `start` must
# refuse rather than orphan the existing one.
BUSY_STATES = frozenset(
    {
        SessionState.STARTING,
        SessionState.RUNNING,
        SessionState.AWAITING_SIGNIN,
        SessionState.READY,
    }
)


@dataclass
class Session:
    """Mutable state for the single active session."""

    state: SessionState = SessionState.IDLE
    driver: BrowserDriver | None = None
    driver_name: str | None = None
    target: str | None = None
    snapshot: PageSnapshot | None = None
    error: str | None = None
    hint: str | None = None
    started_at: str | None = None
    updated_at: str = field(default_factory=utcnow_iso)
    screenshot_count: int = 0
    _task: asyncio.Task[None] | None = field(default=None, repr=False)

    def status(self) -> SessionStatus:
        return SessionStatus(
            state=self.state,
            driver=self.driver_name,
            target=self.target,
            snapshot=self.snapshot,
            error=self.error,
            hint=self.hint,
            started_at=self.started_at,
            updated_at=self.updated_at,
            screenshot_count=self.screenshot_count,
        )


class SessionManager:
    """Coordinates driver lifecycle, navigation and sign-in waiting."""

    def __init__(self, settings: Settings, bus: LogBus | None = None) -> None:
        self.settings = settings
        self.bus = bus or LogBus()
        self.registry = build_registry()
        self.session = Session()
        self._lock = asyncio.Lock()

    # -- helpers -----------------------------------------------------------
    def _stamp(self) -> str:
        return utcnow_iso()

    def _set_state(self, state: SessionState, **extra: object) -> None:
        self.session.state = state
        self.session.updated_at = self._stamp()
        for key, value in extra.items():
            setattr(self.session, key, value)
        self.bus.emit("state", f"session -> {state.value}", state=state.value)

    # -- lifecycle ---------------------------------------------------------
    async def start(
        self,
        driver: str | None = None,
        headless: bool | None = None,
        target: str = "home",
        wait_for_signin: bool = True,
    ) -> SessionStatus:
        async with self._lock:
            if self.session.state in BUSY_STATES:
                self.bus.warn("A session is already running; stop it first")
                return self.session.status()

            driver_name = driver or self.settings.driver
            use_headless = self.settings.headless if headless is None else headless

            try:
                resolved: Target = resolve_target(target)
            except TargetNotAllowed as exc:
                self.bus.error(f"Refused to navigate: {exc}")
                self._set_state(SessionState.ERROR, error=str(exc), hint=None)
                return self.session.status()

            ctx = DriverContext(
                headless=use_headless,
                profile_dir=str(self.settings.profile_dir) if use_headless is False else None,
                browser_executable=self.settings.browser_executable,
                nav_timeout_ms=self.settings.nav_timeout_ms,
                bus=self.bus,
            )

            self._set_state(SessionState.STARTING, driver_name=driver_name,
                            target=resolved.name, error=None, hint=None)
            self.bus.info(f"Starting session", driver=driver_name,
                          target=resolved.url, headless=use_headless)

            try:
                instance = self.registry.create(driver_name, ctx)
            except KeyError as exc:
                self._set_state(SessionState.ERROR, error=str(exc))
                return self.session.status()

            result: LaunchResult = await instance.start()
            if not result.ok:
                self.session.driver = None
                self._set_state(SessionState.ERROR, error=result.message,
                                hint=result.hint or None)
                return self.session.status()

            self.session.driver = instance
            self.session.started_at = self._stamp()
            self._set_state(SessionState.RUNNING)

            snapshot = await instance.goto(resolved.url)
            self.session.snapshot = snapshot
            self.session.updated_at = self._stamp()

            if snapshot.page_state == PageState.SIGN_IN_REQUIRED:
                self._set_state(SessionState.AWAITING_SIGNIN)
                self.session.hint = (
                    "Apple sign-in required. Complete the sign-in and any "
                    "two-factor prompt yourself in the opened browser window — "
                    "this console never types Apple credentials."
                )
                self.bus.warn("Sign-in required — waiting for a human",
                              url=snapshot.url)
                if wait_for_signin:
                    self._spawn_signin_watch()
            elif snapshot.page_state == PageState.TWO_FACTOR_PROMPT:
                self._set_state(SessionState.AWAITING_SIGNIN)
                self.session.hint = "Enter the two-factor code in the browser window."
                if wait_for_signin:
                    self._spawn_signin_watch()
            else:
                self._set_state(SessionState.READY)

            return self.session.status()

    async def stop(self) -> SessionStatus:
        async with self._lock:
            task = self.session._task
            if task is not None and not task.done():
                task.cancel()
            self.session._task = None

            driver = self.session.driver
            if driver is not None:
                try:
                    await driver.stop()
                except Exception as exc:  # pragma: no cover - defensive
                    self.bus.error(f"Error stopping driver: {exc}")
            self.session.driver = None
            self._set_state(SessionState.STOPPED)
            return self.session.status()

    # -- navigation --------------------------------------------------------
    async def goto(self, target: str) -> SessionStatus:
        driver = self.session.driver
        if driver is None or not driver.running:
            self.bus.error("No active browser session to navigate")
            return self.session.status()

        try:
            resolved = resolve_target(target)
        except TargetNotAllowed as exc:
            self.bus.error(f"Refused to navigate: {exc}")
            self.session.error = str(exc)
            self.session.updated_at = self._stamp()
            return self.session.status()

        snapshot = await driver.goto(resolved.url)
        self.session.snapshot = snapshot
        self.session.target = resolved.name
        self.session.error = None

        if snapshot.page_state == PageState.SIGNED_IN:
            self._set_state(SessionState.READY)
        elif snapshot.page_state in (PageState.SIGN_IN_REQUIRED, PageState.TWO_FACTOR_PROMPT):
            self._set_state(SessionState.AWAITING_SIGNIN)
        else:
            self._set_state(SessionState.RUNNING)
        return self.session.status()

    # --------------------------------------------------------- interaction
    async def click_signin(self, wait_for_signin: bool = True) -> SessionStatus:
        """Click the landing-page Sign In button and follow the redirect.

        This only clicks. The Apple ID form that follows is filled in by a
        human — see the class docstring.
        """
        driver = self.session.driver
        if driver is None or not driver.running:
            self.bus.error("No active browser session to click in")
            self.session.error = "No active browser session."
            self.session.updated_at = self._stamp()
            return self.session.status()

        self.bus.step("Clicking the Sign In button")
        used = await driver.click_signin_button()
        if used is None:
            self.session.error = (
                "Could not find or click the Sign In button. The page may still "
                "be loading, or Apple changed the markup."
            )
            self.session.hint = "Refresh the snapshot, or navigate to 'signin' directly."
            self.session.updated_at = self._stamp()
            self._set_state(SessionState.ERROR, error=self.session.error,
                            hint=self.session.hint)
            return self.session.status()

        self.bus.info("Sign In clicked", selector=used)
        # Give the redirect to idmsa.apple.com a moment to land.
        await asyncio.sleep(self.settings.click_settle_ms / 1000.0)

        try:
            snap = await driver.snapshot()
        except Exception as exc:
            self.bus.error(f"Snapshot after click failed: {exc}")
            self.session.updated_at = self._stamp()
            return self.session.status()

        self.session.snapshot = snap
        self.session.error = None
        self.session.updated_at = self._stamp()
        self.bus.info("Landed after Sign In click",
                      url=snap.url, title=snap.title,
                      page_state=snap.page_state.value)

        if snap.page_state in (PageState.SIGN_IN_REQUIRED, PageState.TWO_FACTOR_PROMPT):
            self._set_state(SessionState.AWAITING_SIGNIN)
            self.session.hint = (
                "You are on the Apple sign-in page. Enter your Apple ID, "
                "password and two-factor code yourself in the opened browser "
                "window — this console never types Apple credentials."
            )
            if wait_for_signin:
                self._spawn_signin_watch()
        elif snap.page_state == PageState.SIGNED_IN:
            self._set_state(SessionState.READY, hint=None)
        else:
            self._set_state(SessionState.RUNNING)

        return self.session.status()

    async def snapshot(self) -> PageSnapshot | None:
        driver = self.session.driver
        if driver is None or not driver.running:
            return self.session.snapshot
        try:
            snap = await driver.snapshot()
        except Exception as exc:
            self.bus.error(f"Snapshot failed: {exc}")
            return self.session.snapshot
        self.session.snapshot = snap
        self.session.updated_at = self._stamp()
        return snap

    async def screenshot(self) -> bytes | None:
        driver = self.session.driver
        if driver is None or not driver.running:
            self.bus.error("No active browser session to screenshot")
            return None
        try:
            png = await driver.screenshot()
        except Exception as exc:
            self.bus.error(f"Screenshot failed: {exc}")
            return None
        self.session.screenshot_count += 1
        self.session.updated_at = self._stamp()
        self.bus.info("Screenshot captured", bytes=len(png))
        return png

    # -- sign-in watch -----------------------------------------------------
    def _spawn_signin_watch(self) -> None:
        if self.session._task is not None and not self.session._task.done():
            return
        self.session._task = asyncio.create_task(self._watch_for_signin())

    async def _watch_for_signin(self) -> None:
        """Poll until the human finishes signing in, or the wait times out.

        This never submits a form. It only observes.
        """
        deadline = self.settings.signin_wait_seconds
        elapsed = 0.0
        self.bus.info(f"Watching for manual sign-in (up to {deadline}s)")

        try:
            while elapsed < deadline:
                await asyncio.sleep(POLL_INTERVAL_S)
                elapsed += POLL_INTERVAL_S

                driver = self.session.driver
                if driver is None or not driver.running:
                    self.bus.warn("Session ended while awaiting sign-in")
                    return

                try:
                    snap = await driver.snapshot()
                except Exception as exc:
                    self.bus.error(f"Sign-in poll failed: {exc}")
                    return

                self.session.snapshot = snap
                self.session.updated_at = self._stamp()

                if snap.page_state == PageState.SIGNED_IN:
                    self._set_state(SessionState.READY, hint=None)
                    self.bus.info("Signed in — session ready", url=snap.url)
                    return

                if snap.page_state == PageState.TWO_FACTOR_PROMPT:
                    self.bus.info("Two-factor prompt detected", url=snap.url)

            self.bus.warn("Timed out waiting for sign-in")
            self.session.hint = (
                "Timed out waiting for sign-in. The browser is still open — "
                "finish signing in and the next action will pick it up."
            )
            self.session.updated_at = self._stamp()
        except asyncio.CancelledError:
            self.bus.info("Sign-in watch cancelled")
            raise
