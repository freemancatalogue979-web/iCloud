"""Browser driver backends."""

from __future__ import annotations

from .base import BrowserDriver, DriverContext, DriverRegistry, classify_failure
from .playwright_driver import PlaywrightDriver
from .selenium_driver import SeleniumDriver


def build_registry() -> DriverRegistry:
    """Registry with both backends available by name."""
    registry = DriverRegistry()
    registry.register(PlaywrightDriver.name, PlaywrightDriver)
    registry.register(SeleniumDriver.name, SeleniumDriver)
    return registry


__all__ = [
    "BrowserDriver",
    "DriverContext",
    "DriverRegistry",
    "PlaywrightDriver",
    "SeleniumDriver",
    "build_registry",
    "classify_failure",
]
