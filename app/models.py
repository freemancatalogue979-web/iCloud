"""Pydantic schemas shared by the API and the browser drivers."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SessionState(str, Enum):
    """Lifecycle of one browser session."""

    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    AWAITING_SIGNIN = "awaiting_signin"
    READY = "ready"
    ERROR = "error"
    STOPPED = "stopped"


class PageState(str, Enum):
    """What the page currently shows, judged from URL + matched selectors."""

    SIGNED_IN = "signed_in"
    TWO_FACTOR_PROMPT = "two_factor_prompt"
    SIGN_IN_REQUIRED = "sign_in_required"
    UNKNOWN = "unknown"


class Target(BaseModel):
    """A resolved navigation destination."""

    name: str = Field(description="Short key, e.g. 'home' or 'photos'.")
    url: str = Field(description="Absolute https URL.")
    label: str = Field(description="Human-readable label for the UI.")


class PageSnapshot(BaseModel):
    """A point-in-time description of the active page."""

    url: str
    title: str = ""
    page_state: PageState = PageState.UNKNOWN
    matched_selectors: list[str] = Field(default_factory=list)
    captured_at: str = Field(default_factory=utcnow_iso)


class LaunchResult(BaseModel):
    """Outcome of starting a driver."""

    ok: bool
    error_code: str | None = None
    message: str = ""
    browser: str = ""
    version: str = ""
    hint: str = ""


class SessionStatus(BaseModel):
    """Everything the UI needs to render the current session."""

    state: SessionState = SessionState.IDLE
    driver: str | None = None
    target: str | None = None
    snapshot: PageSnapshot | None = None
    error: str | None = None
    hint: str | None = None
    started_at: str | None = None
    updated_at: str = Field(default_factory=utcnow_iso)
    screenshot_count: int = 0


class StartRequest(BaseModel):
    driver: Literal["playwright", "selenium"] | None = None
    headless: bool | None = None
    target: str = "home"
    wait_for_signin: bool = True


class GotoRequest(BaseModel):
    target: str = Field(description="A named target key or a full icloud.com URL.")


class DriverInfo(BaseModel):
    name: str
    package_installed: bool
    browser_available: bool
    detail: str = ""


class ServiceInfo(BaseModel):
    app: str
    version: str
    default_driver: str
    headless: bool
    profile_dir: str
    targets: dict[str, Target]
    drivers: list[DriverInfo]
    network: dict[str, Any] = Field(default_factory=dict)
