"""Page classification is pure — cover every branch without a browser."""

from __future__ import annotations

import pytest

from app.icloud import (
    SELECTOR_GROUPS,
    SIGNIN_BUTTON_SELECTORS,
    classify_page,
    first_present_selector,
    host_of,
    matched_groups,
)
from app.models import PageState


def test_signin_host_means_sign_in_required() -> None:
    assert classify_page("https://idmsa.apple.com/appleauth/auth/signin") is (
        PageState.SIGN_IN_REQUIRED
    )


def test_account_field_means_sign_in_required() -> None:
    assert classify_page(
        "https://www.icloud.com/", {"account_entry"}
    ) is PageState.SIGN_IN_REQUIRED


def test_password_field_means_sign_in_required() -> None:
    assert classify_page(
        "https://www.icloud.com/", {"password_entry"}
    ) is PageState.SIGN_IN_REQUIRED


def test_two_factor_outranks_account_entry() -> None:
    """A 2FA prompt can appear with sign-in fields still in the DOM."""
    assert classify_page(
        "https://idmsa.apple.com/appleauth/auth/signin",
        {"two_factor", "account_entry", "password_entry"},
    ) is PageState.TWO_FACTOR_PROMPT


@pytest.mark.parametrize("group", ["user_initials", "account_menu", "app_launcher"])
def test_signed_in_indicators(group: str) -> None:
    assert classify_page("https://www.icloud.com/photos", {group}) is PageState.SIGNED_IN


def test_signed_in_requires_not_being_on_signin_host() -> None:
    """Even with account chrome, the sign-in host is never 'signed in'."""
    assert classify_page(
        "https://idmsa.apple.com/x", {"user_initials"}
    ) is PageState.SIGN_IN_REQUIRED


@pytest.mark.parametrize(
    "url",
    ["", "about:blank", "data:text/html,<p>hi</p>", "not a url"],
)
def test_blank_or_unparseable_is_unknown(url: str) -> None:
    assert classify_page(url) is PageState.UNKNOWN


def test_no_signals_is_unknown() -> None:
    assert classify_page("https://www.icloud.com/") is PageState.UNKNOWN


def test_matched_groups_filters_false() -> None:
    presence = {"account_entry": True, "password_entry": False, "app_launcher": True}
    assert matched_groups(presence) == {"account_entry", "app_launcher"}


def test_matched_groups_empty() -> None:
    assert matched_groups({}) == set()


def test_host_of() -> None:
    assert host_of("https://WWW.iCloud.com/path") == "www.icloud.com"
    assert host_of("") == ""
    assert host_of("about:blank") == ""


def test_selector_groups_are_non_empty_css() -> None:
    assert SELECTOR_GROUPS, "selector table must not be empty"
    for key, css in SELECTOR_GROUPS.items():
        assert css.strip(), f"selector group {key} is empty"
        for part in css.split(","):
            assert part.strip(), f"selector group {key} has an empty alternative"


# ------------------------------------------------ step 2: Sign In button
def test_the_reported_markup_selector_is_first_priority() -> None:
    """The exact class from the real landing page must win the click race."""
    assert SIGNIN_BUTTON_SELECTORS[0] == "ui-button.sign-in-button"


def test_signin_selectors_are_unique_and_non_empty() -> None:
    assert len(SIGNIN_BUTTON_SELECTORS) == len(set(SIGNIN_BUTTON_SELECTORS))
    assert all(s.strip() for s in SIGNIN_BUTTON_SELECTORS)


def test_signin_button_is_a_selector_group() -> None:
    assert "signin_button" in SELECTOR_GROUPS
    assert "ui-button.sign-in-button" in SELECTOR_GROUPS["signin_button"]


def test_first_present_selector_respects_priority_order() -> None:
    present = {"a[href*='/signin']", "ui-button.sign-in-button"}
    assert first_present_selector(SIGNIN_BUTTON_SELECTORS, present) == (
        "ui-button.sign-in-button"
    )


def test_first_present_selector_falls_back_to_a_link() -> None:
    present = {"a[href*='/signin']"}
    assert first_present_selector(SIGNIN_BUTTON_SELECTORS, present) == "a[href*='/signin']"


def test_first_present_selector_returns_none_when_absent() -> None:
    assert first_present_selector(SIGNIN_BUTTON_SELECTORS, set()) is None
    assert first_present_selector(SIGNIN_BUTTON_SELECTORS, None) is None


def test_first_present_selector_ignores_unknown_selectors() -> None:
    assert first_present_selector(SIGNIN_BUTTON_SELECTORS, {"button.nope"}) is None
