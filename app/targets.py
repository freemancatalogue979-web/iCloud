"""Navigation targets and the URL allowlist.

Everything here is pure — no I/O, no browser — so the guard that decides what
the console may visit is directly unit-testable.
"""

from __future__ import annotations

from urllib.parse import urlparse

from .config import ALLOWED_HOSTS
from .models import Target


class TargetNotAllowed(ValueError):
    """Raised when a requested destination is outside the allowlist."""

    def __init__(self, value: str, reason: str) -> None:
        super().__init__(f"{value!r} is not an allowed destination ({reason})")
        self.value = value
        self.reason = reason


# Named shortcuts shown in the UI. `home` is the entry point the task asks for.
NAMED_TARGETS: dict[str, Target] = {
    "home": Target(
        name="home",
        url="https://www.icloud.com/",
        label="iCloud home",
    ),
    "signin": Target(
        name="signin",
        url="https://idmsa.apple.com/appleauth/auth/signin",
        label="Apple sign-in",
    ),
    "photos": Target(
        name="photos",
        url="https://www.icloud.com/photos",
        label="iCloud Photos",
    ),
    "mail": Target(
        name="mail",
        url="https://www.icloud.com/mail",
        label="iCloud Mail",
    ),
    "drive": Target(
        name="drive",
        url="https://www.icloud.com/iclouddrive",
        label="iCloud Drive",
    ),
    "notes": Target(
        name="notes",
        url="https://www.icloud.com/notes",
        label="Notes",
    ),
    "calendar": Target(
        name="calendar",
        url="https://www.icloud.com/calendar",
        label="Calendar",
    ),
    "find": Target(
        name="find",
        url="https://www.icloud.com/find",
        label="Find My",
    ),
    "settings": Target(
        name="settings",
        url="https://www.icloud.com/settings",
        label="iCloud settings",
    ),
}


def is_allowed_host(host: str) -> bool:
    """True if `host` is an explicitly allowed host or an icloud.com subdomain."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host in ALLOWED_HOSTS:
        return True
    return host.endswith(".icloud.com")


def resolve_target(value: str) -> Target:
    """Resolve a named key or an absolute URL to a validated Target.

    Raises TargetNotAllowed for anything that is not icloud.com family.
    """
    if value is None:
        raise TargetNotAllowed("None", "no target given")

    raw = value.strip()
    if not raw:
        raise TargetNotAllowed("", "empty target")

    key = raw.lower()
    if key in NAMED_TARGETS:
        return NAMED_TARGETS[key]

    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise TargetNotAllowed(raw, "scheme must be http or https")

    host = parsed.hostname or ""
    if not is_allowed_host(host):
        raise TargetNotAllowed(raw, f"host {host!r} is not on the allowlist")

    label = f"{host}{parsed.path or '/'}"
    return Target(name="custom", url=raw, label=label)


def all_targets() -> dict[str, Target]:
    """Copy of the named target table, for the UI."""
    return dict(NAMED_TARGETS)
