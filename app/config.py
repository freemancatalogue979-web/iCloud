"""Runtime configuration, loaded from the environment.

Reads an optional `.env` file and `ICLOUD_*` variables. Deliberately holds no
credential fields — see the note in `.env.example`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parent.parent

DriverName = Literal["playwright", "selenium"]

# Hosts the console is permitted to drive the browser to. Anything outside this
# set is rejected before it reaches a driver.
ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "icloud.com",
        "www.icloud.com",
        "setup.icloud.com",
        "fmipmobile.icloud.com",
        "idmsa.apple.com",  # Apple's sign-in domain, reached by redirect
    }
)


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, `#` comments, no shell semantics."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        os.environ.setdefault(key, value)


def _env_str(key: str, default: str) -> str:
    value = os.environ.get(key)
    return value if value not in (None, "") else default


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings."""

    app_name: str = "iCloud Browser Console"
    host: str = "0.0.0.0"
    port: int = 8000

    driver: DriverName = "playwright"
    headless: bool = False
    profile_dir: Path = REPO_ROOT / ".profile" / "icloud"
    browser_executable: str | None = None

    nav_timeout_ms: int = 30_000
    signin_wait_seconds: int = 180
    click_settle_ms: int = 1_000

    allowed_hosts: frozenset[str] = field(default_factory=lambda: ALLOWED_HOSTS)

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        _load_dotenv(env_file or (REPO_ROOT / ".env"))

        driver = _env_str("ICLOUD_DRIVER", "playwright").lower()
        if driver not in ("playwright", "selenium"):
            driver = "playwright"

        executable = os.environ.get("ICLOUD_BROWSER_EXECUTABLE") or None

        profile_raw = _env_str("ICLOUD_PROFILE_DIR", "")
        profile = Path(profile_raw) if profile_raw else REPO_ROOT / ".profile" / "icloud"
        if not profile.is_absolute():
            profile = REPO_ROOT / profile

        return cls(
            app_name=_env_str("ICLOUD_APP_NAME", "iCloud Browser Console"),
            host=_env_str("ICLOUD_HOST", "0.0.0.0"),
            port=_env_int("ICLOUD_PORT", 8000),
            driver=driver,  # type: ignore[arg-type]
            headless=_env_bool("ICLOUD_HEADLESS", False),
            profile_dir=profile,
            browser_executable=executable,
            nav_timeout_ms=_env_int("ICLOUD_NAV_TIMEOUT_MS", 30_000),
            signin_wait_seconds=_env_int("ICLOUD_SIGNIN_WAIT_SECONDS", 180),
            click_settle_ms=_env_int("ICLOUD_CLICK_SETTLE_MS", 1_000),
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide Settings, building it on first use."""
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def reset_settings() -> None:
    """Drop the cached Settings (used by tests)."""
    global _settings
    _settings = None
