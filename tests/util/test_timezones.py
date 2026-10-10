"""Tests for the shared IANA timezone validator."""

from __future__ import annotations

import pytest

from omnigent.util.timezones import is_valid_timezone


@pytest.mark.parametrize("name", ["UTC", "America/Los_Angeles", "Asia/Kolkata", "Etc/GMT+5"])
def test_known_zone_keys_are_valid(name: str) -> None:
    assert is_valid_timezone(name) is True


@pytest.mark.parametrize("name", ["", "Not/A_Timezone", "../UTC", "PST", None, 42, ["UTC"]])
def test_unknown_or_non_string_values_are_invalid(name: object) -> None:
    assert is_valid_timezone(name) is False
