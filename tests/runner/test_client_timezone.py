"""Tests for the per-session client timezone the runner remembers."""

from __future__ import annotations

import pytest

from omnigent.runner import client_timezone
from omnigent.runner.client_timezone import (
    client_timezone_for,
    forget_client_timezone,
    remember_client_timezone,
)


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_timezone, "_session_client_timezones", {})


def test_remembers_the_latest_valid_zone_per_session() -> None:
    remember_client_timezone("conv_a", "America/Los_Angeles")
    remember_client_timezone("conv_b", "Asia/Tokyo")
    remember_client_timezone("conv_a", "Europe/Berlin")

    assert client_timezone_for("conv_a") == "Europe/Berlin"
    assert client_timezone_for("conv_b") == "Asia/Tokyo"
    assert client_timezone_for("conv_unknown") is None
    assert client_timezone_for(None) is None


@pytest.mark.parametrize("value", [None, "", "Not/A_Timezone", 7, {"tz": "UTC"}])
def test_unusable_values_leave_the_remembered_zone_alone(value: object) -> None:
    remember_client_timezone("conv_a", "America/Los_Angeles")

    remember_client_timezone("conv_a", value)

    assert client_timezone_for("conv_a") == "America/Los_Angeles"


def test_forget_drops_the_session_only() -> None:
    remember_client_timezone("conv_a", "America/Los_Angeles")
    remember_client_timezone("conv_b", "Asia/Tokyo")

    forget_client_timezone("conv_a")
    forget_client_timezone("conv_never_seen")

    assert client_timezone_for("conv_a") is None
    assert client_timezone_for("conv_b") == "Asia/Tokyo"
