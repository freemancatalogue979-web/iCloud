"""The visit-triggered flow: open iCloud, then click Sign In.

Kept free of FastAPI and WebSocket concerns so it can be tested against a stub
manager. `run_flow` only ever clicks — the Apple credential form that follows is
completed by a human in the opened browser window.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol

from app.models import PageState, SessionState


class FlowStep(str, Enum):
    LAUNCH = "launch"
    NAVIGATE = "navigate"
    CLICK_SIGNIN = "click_signin"
    AWAIT_HUMAN = "await_human"
    ALREADY_SIGNED_IN = "already_signed_in"
    DONE = "done"
    FAILED = "failed"


@dataclass
class StepResult:
    step: FlowStep
    ok: bool
    detail: str = ""


@dataclass
class FlowResult:
    ok: bool
    steps: list[StepResult] = field(default_factory=list)
    final_state: str = ""
    url: str = ""
    error: str = ""
    hint: str = ""

    def step_names(self) -> list[str]:
        return [s.step.value for s in self.steps]


class FlowManager(Protocol):
    """The slice of SessionManager the flow depends on."""

    async def start(self, **kwargs: Any) -> Any: ...

    async def click_signin(self, **kwargs: Any) -> Any: ...

    async def screenshot(self) -> bytes | None: ...


OnStep = Callable[[StepResult], Awaitable[None]]


async def _noop_step(_result: StepResult) -> None:
    return None


async def run_flow(
    manager: FlowManager,
    *,
    target: str = "home",
    driver: str | None = None,
    headless: bool | None = None,
    on_step: OnStep | None = None,
    capture_screenshot: bool = True,
) -> FlowResult:
    """Run the whole visit flow, reporting each step as it completes."""
    report = on_step or _noop_step
    result = FlowResult(ok=False)

    async def record(step: FlowStep, ok: bool, detail: str = "") -> StepResult:
        item = StepResult(step=step, ok=ok, detail=detail)
        result.steps.append(item)
        await report(item)
        return item

    # ---------------------------------------------------------- 1. launch
    status = await manager.start(
        driver=driver,
        headless=headless,
        target=target,
        wait_for_signin=False,
    )
    result.final_state = getattr(status.state, "value", str(status.state))

    if status.state is SessionState.ERROR:
        await record(FlowStep.LAUNCH, False, status.error or "launch failed")
        result.error = status.error or "launch failed"
        result.hint = status.hint or ""
        await record(FlowStep.FAILED, False, result.error)
        return result

    await record(FlowStep.LAUNCH, True, f"browser up ({status.driver})")

    # -------------------------------------------------------- 2. navigate
    snap = status.snapshot
    result.url = snap.url if snap else ""
    await record(FlowStep.NAVIGATE, True, result.url or "(no page)")

    if capture_screenshot:
        await manager.screenshot()

    # --------------------------------------------------- 3. decide + click
    page_state = snap.page_state if snap else PageState.UNKNOWN

    if page_state is PageState.SIGNED_IN:
        await record(FlowStep.ALREADY_SIGNED_IN, True, snap.url)
        result.ok = True
        result.final_state = SessionState.READY.value
        await record(FlowStep.DONE, True, "already signed in")
        return result

    if page_state in (PageState.SIGN_IN_REQUIRED, PageState.TWO_FACTOR_PROMPT):
        # The landing page redirected straight to the Apple form.
        await record(FlowStep.AWAIT_HUMAN, True,
                     "sign-in page already showing — no click needed")
        result.ok = True
        result.final_state = SessionState.AWAITING_SIGNIN.value
        await record(FlowStep.DONE, True, "waiting for the person to sign in")
        return result

    clicked = await manager.click_signin(wait_for_signin=True)
    result.final_state = getattr(clicked.state, "value", str(clicked.state))
    if clicked.snapshot is not None:
        result.url = clicked.snapshot.url

    if clicked.state is SessionState.ERROR:
        await record(FlowStep.CLICK_SIGNIN, False, clicked.error or "click failed")
        result.error = clicked.error or "click failed"
        result.hint = clicked.hint or ""
        await record(FlowStep.FAILED, False, result.error)
        return result

    await record(FlowStep.CLICK_SIGNIN, True, result.url)
    await record(FlowStep.AWAIT_HUMAN, True,
                 "enter your Apple ID and two-factor code in the browser window")
    if capture_screenshot:
        await manager.screenshot()

    result.ok = True
    await record(FlowStep.DONE, True, "Sign In clicked — waiting for you")
    return result
