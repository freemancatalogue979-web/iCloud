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

# The "Sign In" button on the www.icloud.com landing page, in priority order.
#
# Real markup (supplied 2026-11-21):
#   <ui-button class="push primary sign-in-button icloud-mouse" tabindex="0"
#              ontouchstart="void(0)" role="button" aria-haspopup="false">
#     <button type="button" tabindex="-1"></button>Sign In
#   </ui-button>
#
# It is a custom element whose inner <button> is tabindex="-1" and empty, so
# clicking the host <ui-button> is the reliable target. Wider fallbacks follow
# in case Apple ships a different build.
SIGNIN_BUTTON_SELECTORS: tuple[str, ...] = (
    "ui-button.sign-in-button",
    "ui-button.push.primary.sign-in-button",
    "ui-button[class*='sign-in-button']",
    "[class*='sign-in-button']",
    "ui-button.sign-in-button > button",
    "#sign-in",
    "[data-testid='sign-in']",
    "a[href*='/signin']",
    "a[href*='idmsa.apple.com']",
)

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
    # Landing-page entry point into the Apple sign-in flow. This is the
    # <ui-button class="push primary sign-in-button icloud-mouse"> element.
    "signin_button": ", ".join(SIGNIN_BUTTON_SELECTORS),
    # Error / interstitial states
    "error_page": "#error, .error-page, [data-testid='error'], #server-error",
}

SIGNIN_GROUPS = ("two_factor", "account_entry")
SIGNED_IN_GROUPS = ("user_initials", "account_menu", "app_launcher")


def first_present_selector(
    candidates: tuple[str, ...], present: set[str] | list[str] | None
) -> str | None:
    """Return the highest-priority candidate selector that is actually present.

    Pure and order-preserving, so the click strategy is testable without a
    browser. Returns None when nothing matched.
    """
    have = set(present or ())
    for selector in candidates:
        if selector in have:
            return selector
    return None


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
