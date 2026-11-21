"""iCloud page object: what to look for, and how to read what we found.

Apple changes its markup, so detection is expressed as a table of selector
*groups* plus a pure classifier over (URL, which groups matched). The
classifier has no I/O and is covered by tests; only the selector sweep needs a
live browser.
"""

from __future__ import annotations

from urllib.parse import urlparse

from .models import PageState

SIGNIN_HOST = "idmsa.apple.com"

# Each group maps a stable key to a comma-separated CSS selector list. Any one
# match counts as the group being present.
SELECTOR_GROUPS: dict[str, str] = {
    # Sign-in form on idmsa.apple.com
    "account_entry": "#account_name_textfield, input[name='appleId'], "
    "input[type='text'][data-testid*='appleId']",
    "password_entry": "#password_textfield, input[name='password'], input[type='password']",
    # Two-factor code entry
    "two_factor": "#char0, input[name='code0'], [data-testid='code0'], "
    "input[autocomplete='one-time-code']",
    # Post-sign-in chrome on www.icloud.com
    "user_initials": "[data-testid='userInitials'], #userInitials, .user-initials, "
    "#account-header-initials",
    "account_menu": "[data-testid='accountMenu'], #account-menu, .icloud-header-account, "
    "button[aria-label*='ccount']",
    "app_launcher": "[data-testid='appList'], #app-list, .app-list, [data-testid='apps']",
    # Error / interstitial states
    "error_page": "#error, .error-page, [data-testid='error'], #server-error",
}

SIGNIN_GROUPS = ("two_factor", "account_entry")
SIGNED_IN_GROUPS = ("user_initials", "account_menu", "app_launcher")


def host_of(url: str) -> str:
    """Lower-cased host of a URL, or '' if unparseable."""
    try:
        return (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def classify_page(url: str, matched: set[str] | None = None) -> PageState:
    """Decide the page state from the URL and the selector groups that matched.

    Pure function — safe to test exhaustively without a browser.
    """
    matched = matched or set()
    host = host_of(url)

    # A browser still sitting on about:blank / empty has not loaded anything.
    if not host:
        return PageState.UNKNOWN

    # 2FA outranks everything: it can appear alongside leftover sign-in fields.
    if "two_factor" in matched:
        return PageState.TWO_FACTOR_PROMPT

    if host == SIGNIN_HOST or "account_entry" in matched or "password_entry" in matched:
        return PageState.SIGN_IN_REQUIRED

    if any(group in matched for group in SIGNED_IN_GROUPS) and host != SIGNIN_HOST:
        return PageState.SIGNED_IN

    return PageState.UNKNOWN


def matched_groups(presence: dict[str, bool]) -> set[str]:
    """Reduce a {group: bool} sweep result to the set of groups that matched."""
    return {name for name, found in presence.items() if found}
