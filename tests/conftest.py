"""Shared fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings with an isolated profile dir and a short sign-in wait."""
    return Settings(
        profile_dir=tmp_path / "profile",
        signin_wait_seconds=4,
        nav_timeout_ms=5_000,
        headless=True,
    )
