"""Tests for the shared harness timeout environment parser."""

from __future__ import annotations

import pytest

from omnigent.inner.harness_timeouts import timeout_seconds_from_env

_NAME = "HARNESS_TEST_PROMPT_TIMEOUT_S"


def test_unset_variable_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_NAME, raising=False)

    assert timeout_seconds_from_env(_NAME, 300.0) == 300.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("7200", 7200.0), (" 1.5e3 ", 1500.0), ("0.25", 0.25)],
)
def test_positive_finite_value_overrides_default(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
) -> None:
    monkeypatch.setenv(_NAME, raw)

    assert timeout_seconds_from_env(_NAME, 300.0) == expected


@pytest.mark.parametrize("raw", ["", "not-a-number", "0", "-1", "nan", "inf", "-inf"])
def test_invalid_value_fails_loud_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(_NAME, raw)

    with pytest.raises(ValueError, match=f"{_NAME} must be a positive finite number of seconds"):
        timeout_seconds_from_env(_NAME, 300.0)
