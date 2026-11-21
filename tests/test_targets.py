"""The allowlist is the security boundary for navigation — test it hard."""

from __future__ import annotations

import pytest

from app.targets import NAMED_TARGETS, TargetNotAllowed, all_targets, is_allowed_host, resolve_target


def test_home_target_is_icloud_com() -> None:
    target = resolve_target("home")
    assert target.url == "https://www.icloud.com/"
    assert target.name == "home"


@pytest.mark.parametrize(
    "value",
    ["home", "signin", "photos", "mail", "drive", "notes", "calendar", "find", "settings"],
)
def test_named_targets_resolve(value: str) -> None:
    target = resolve_target(value)
    assert target.url.startswith("https://")
    assert target.label


@pytest.mark.parametrize("value", ["HOME", " Home ", "Home"])
def test_named_lookup_is_case_and_space_insensitive(value: str) -> None:
    assert resolve_target(value).name == "home"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.icloud.com/photos",
        "https://icloud.com/",
        "https://setup.icloud.com/",
        "https://anything.icloud.com/x/y?z=1",
        "https://idmsa.apple.com/appleauth/auth/signin",
    ],
)
def test_allowed_custom_urls(url: str) -> None:
    assert resolve_target(url).url == url


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.com/icloud",
        "https://noticloud.com/",
        "https://icloud.com.evil.com/",
        "https://apple.com/",
        "https://evil.apple.com.phish.com/",
        "http://example.org/",
        "ftp://www.icloud.com/",
        "javascript:alert(1)",
        "//www.icloud.com/",
        "www.icloud.com",
    ],
)
def test_disallowed_targets_raise(url: str) -> None:
    with pytest.raises(TargetNotAllowed):
        resolve_target(url)


def test_error_carries_the_offending_value() -> None:
    with pytest.raises(TargetNotAllowed) as excinfo:
        resolve_target("https://evil.com/")
    assert excinfo.value.value == "https://evil.com/"
    assert "allowlist" in str(excinfo.value)


@pytest.mark.parametrize("host", ["icloud.com", "www.icloud.com", "a.b.icloud.com"])
def test_is_allowed_host_accepts_icloud_family(host: str) -> None:
    assert is_allowed_host(host) is True


@pytest.mark.parametrize("host", ["evil.com", "icloud.com.evil.com", "", "apple.com"])
def test_is_allowed_host_rejects_others(host: str) -> None:
    assert is_allowed_host(host) is False


def test_icloud_com_evil_lookalike_is_rejected() -> None:
    """The classic suffix trap: a domain ending in 'icloud.com' but not a subdomain."""
    assert is_allowed_host("noticloud.com") is False
    with pytest.raises(TargetNotAllowed):
        resolve_target("https://noticloud.com/")


def test_all_targets_matches_named_table() -> None:
    assert all_targets() == NAMED_TARGETS
    assert "home" in all_targets()
