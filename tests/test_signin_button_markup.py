"""Verify the Sign In selectors against the REAL landing-page markup.

The HTML below is the element as reported from www.icloud.com on 2026-11-21.
Parsing it with a real CSS engine (lxml + cssselect) proves the selectors this
app clicks actually match that element — no browser required.
"""

from __future__ import annotations

import pytest
from lxml import html as lxml_html

from app.icloud import SIGNIN_BUTTON_SELECTORS, first_present_selector

# Verbatim from the landing page.
LANDING_MARKUP = """
<div id="root">
  <ui-button class="push primary sign-in-button icloud-mouse" tabindex="0"
             ontouchstart="void(0)" role="button" aria-haspopup="false">
    <button type="button" tabindex="-1"></button>Sign In
  </ui-button>
</div>
"""


@pytest.fixture
def tree():
    return lxml_html.fromstring(LANDING_MARKUP)


def matches(tree, selector: str) -> list:
    return tree.cssselect(selector)


def test_primary_selector_matches_the_real_element(tree) -> None:
    found = matches(tree, "ui-button.sign-in-button")
    assert len(found) == 1
    assert found[0].tag == "ui-button"


def test_primary_selector_is_the_element_the_user_supplied(tree) -> None:
    element = matches(tree, "ui-button.sign-in-button")[0]
    classes = element.get("class", "").split()
    assert set(classes) == {"push", "primary", "sign-in-button", "icloud-mouse"}
    assert element.get("tabindex") == "0"
    assert element.get("role") == "button"
    assert element.get("aria-haspopup") == "false"


def test_matched_element_contains_the_sign_in_label(tree) -> None:
    element = matches(tree, "ui-button.sign-in-button")[0]
    assert "Sign In" in element.text_content()


def test_inner_button_fallback_also_matches(tree) -> None:
    """The fallback used when a click on the custom-element host is swallowed."""
    found = matches(tree, "ui-button.sign-in-button > button")
    assert len(found) == 1
    assert found[0].get("type") == "button"
    assert found[0].get("tabindex") == "-1"


@pytest.mark.parametrize(
    "selector",
    [
        "ui-button.sign-in-button",
        "ui-button.push.primary.sign-in-button",
        "ui-button[class*='sign-in-button']",
        "[class*='sign-in-button']",
        "ui-button.sign-in-button > button",
    ],
)
def test_redundancy_selectors_also_match(tree, selector: str) -> None:
    """Several candidates must match, so a markup tweak does not break step 2."""
    assert len(matches(tree, selector)) >= 1, selector


@pytest.mark.parametrize(
    "selector",
    ["#sign-in", "[data-testid='sign-in']", "a[href*='/signin']",
     "a[href*='idmsa.apple.com']"],
)
def test_link_fallbacks_do_not_match_this_page(tree, selector: str) -> None:
    """These are last-resort fallbacks for other page variants."""
    assert matches(tree, selector) == []


def test_priority_order_selects_the_ui_button(tree) -> None:
    present = {s for s in SIGNIN_BUTTON_SELECTORS if matches(tree, s)}
    assert first_present_selector(SIGNIN_BUTTON_SELECTORS, present) == (
        "ui-button.sign-in-button"
    )


def test_selector_group_string_matches_the_element(tree) -> None:
    """The comma-joined group used for snapshot detection must also match."""
    from app.icloud import SELECTOR_GROUPS

    group = SELECTOR_GROUPS["signin_button"]
    hits = [part.strip() for part in group.split(",") if matches(tree, part.strip())]
    assert "ui-button.sign-in-button" in hits


def test_no_signin_button_on_a_signed_in_page(tree) -> None:
    signed_in = lxml_html.fromstring(
        '<div id="root"><div data-testid="appList">apps</div></div>'
    )
    present = {s for s in SIGNIN_BUTTON_SELECTORS if matches(signed_in, s)}
    assert present == set()
    assert first_present_selector(SIGNIN_BUTTON_SELECTORS, present) is None
