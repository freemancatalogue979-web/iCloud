"""The visit-triggered flow: open iCloud -> click Sign In.

Driven by a stub manager so every branch runs without a browser binary.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models import PageSnapshot, PageState, SessionState
from client.flow import FlowStep, run_flow


@dataclass
class StubStatus:
    state: SessionState
    snapshot: PageSnapshot | None = None
    driver: str = "stub"
    error: str | None = None
    hint: str | None = None


@dataclass
class StubManager:
    """Records the calls the flow makes."""

    start_result: StubStatus
    click_result: StubStatus
    shots: int = 0
    start_calls: list[dict] = field(default_factory=list)
    click_calls: list[dict] = field(default_factory=list)

    async def start(self, **kwargs):
        self.start_calls.append(kwargs)
        return self.start_result

    async def click_signin(self, **kwargs):
        self.click_calls.append(kwargs)
        return self.click_result

    async def screenshot(self):
        self.shots += 1
        return b"\x89PNG stub"


LANDING = PageSnapshot(url="https://www.icloud.com/", title="iCloud",
                       page_state=PageState.UNKNOWN,
                       matched_selectors=["signin_button"])
SIGNIN = PageSnapshot(url="https://idmsa.apple.com/appleauth/auth/signin",
                      title="Sign in", page_state=PageState.SIGN_IN_REQUIRED,
                      matched_selectors=["account_entry"])
SIGNED_IN = PageSnapshot(url="https://www.icloud.com/photos", title="Photos",
                         page_state=PageState.SIGNED_IN,
                         matched_selectors=["app_launcher"])


# ------------------------------------------------------------ happy path
async def test_full_flow_launches_navigates_and_clicks() -> None:
    manager = StubManager(
        start_result=StubStatus(SessionState.RUNNING, LANDING),
        click_result=StubStatus(SessionState.AWAITING_SIGNIN, SIGNIN,
                                hint="sign in yourself"),
    )

    result = await run_flow(manager)

    assert result.ok is True
    assert result.step_names() == [
        FlowStep.LAUNCH.value,
        FlowStep.NAVIGATE.value,
        FlowStep.CLICK_SIGNIN.value,
        FlowStep.AWAIT_HUMAN.value,
        FlowStep.DONE.value,
    ]
    assert result.url == SIGNIN.url
    assert result.final_state == SessionState.AWAITING_SIGNIN.value


async def test_flow_targets_icloud_home_by_default() -> None:
    manager = StubManager(
        start_result=StubStatus(SessionState.RUNNING, LANDING),
        click_result=StubStatus(SessionState.AWAITING_SIGNIN, SIGNIN),
    )
    await run_flow(manager)

    assert manager.start_calls[0]["target"] == "home"
    # The flow must not ask for credentials or submit a form.
    assert manager.start_calls[0]["wait_for_signin"] is False


async def test_flow_captures_screenshots_for_the_iframe() -> None:
    manager = StubManager(
        start_result=StubStatus(SessionState.RUNNING, LANDING),
        click_result=StubStatus(SessionState.AWAITING_SIGNIN, SIGNIN),
    )
    await run_flow(manager)
    assert manager.shots == 2  # after navigate, and after the click


async def test_flow_can_skip_screenshot_capture() -> None:
    manager = StubManager(
        start_result=StubStatus(SessionState.RUNNING, LANDING),
        click_result=StubStatus(SessionState.AWAITING_SIGNIN, SIGNIN),
    )
    await run_flow(manager, capture_screenshot=False)
    assert manager.shots == 0


async def test_on_step_callback_sees_every_step() -> None:
    manager = StubManager(
        start_result=StubStatus(SessionState.RUNNING, LANDING),
        click_result=StubStatus(SessionState.AWAITING_SIGNIN, SIGNIN),
    )
    seen: list[str] = []

    async def on_step(step) -> None:
        seen.append(step.step.value)

    await run_flow(manager, on_step=on_step)
    assert seen == [
        "launch", "navigate", "click_signin", "await_human", "done"
    ]


# ------------------------------------------------- no click needed cases
async def test_already_on_sign_in_page_skips_the_click() -> None:
    """The landing page can redirect straight to the Apple form."""
    manager = StubManager(
        start_result=StubStatus(SessionState.AWAITING_SIGNIN, SIGNIN),
        click_result=StubStatus(SessionState.AWAITING_SIGNIN, SIGNIN),
    )

    result = await run_flow(manager)

    assert result.ok is True
    assert manager.click_calls == [], "must not click when already on the form"
    assert FlowStep.CLICK_SIGNIN.value not in result.step_names()
    assert FlowStep.AWAIT_HUMAN.value in result.step_names()


async def test_already_signed_in_short_circuits() -> None:
    manager = StubManager(
        start_result=StubStatus(SessionState.READY, SIGNED_IN),
        click_result=StubStatus(SessionState.READY, SIGNED_IN),
    )

    result = await run_flow(manager)

    assert result.ok is True
    assert manager.click_calls == []
    assert FlowStep.ALREADY_SIGNED_IN.value in result.step_names()
    assert result.final_state == SessionState.READY.value


# -------------------------------------------------------- failure paths
async def test_launch_failure_stops_before_navigating() -> None:
    manager = StubManager(
        start_result=StubStatus(SessionState.ERROR, None,
                                error="No Playwright browser binary found.",
                                hint="Run: python -m playwright install chromium"),
        click_result=StubStatus(SessionState.ERROR, None),
    )

    result = await run_flow(manager)

    assert result.ok is False
    assert result.error == "No Playwright browser binary found."
    assert "playwright install" in result.hint
    assert manager.click_calls == [], "must not try to click without a browser"
    assert result.step_names() == ["launch", "failed"]


async def test_click_failure_is_reported() -> None:
    manager = StubManager(
        start_result=StubStatus(SessionState.RUNNING, LANDING),
        click_result=StubStatus(SessionState.ERROR, LANDING,
                                error="Could not find or click the Sign In button.",
                                hint="Refresh the snapshot"),
    )

    result = await run_flow(manager)

    assert result.ok is False
    assert FlowStep.FAILED.value in result.step_names()
    assert "Sign In button" in result.error


async def test_unknown_page_state_still_attempts_the_click() -> None:
    """An unrecognised page should still get a click attempt, not a silent stop."""
    manager = StubManager(
        start_result=StubStatus(SessionState.RUNNING, LANDING),
        click_result=StubStatus(SessionState.AWAITING_SIGNIN, SIGNIN),
    )
    result = await run_flow(manager)
    assert len(manager.click_calls) == 1
    assert result.ok is True


async def test_result_records_final_state_and_steps() -> None:
    manager = StubManager(
        start_result=StubStatus(SessionState.RUNNING, LANDING),
        click_result=StubStatus(SessionState.AWAITING_SIGNIN, SIGNIN),
    )
    result = await run_flow(manager)

    assert all(s.ok for s in result.steps)
    assert result.final_state == "awaiting_signin"
