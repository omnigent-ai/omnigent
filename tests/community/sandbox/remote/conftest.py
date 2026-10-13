"""Fixtures for the remote sandbox provider tests."""

from __future__ import annotations

import pytest

from omnigent.community.sandbox.remote import launcher
from tests.community.sandbox.remote._shim import FakeService


@pytest.fixture
def service() -> FakeService:
    return FakeService()


@pytest.fixture(autouse=True)
def _no_start_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher, "_START_RETRY_DELAYS_S", (0.0, 0.0))
