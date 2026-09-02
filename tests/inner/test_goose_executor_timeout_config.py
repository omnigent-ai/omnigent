"""Tests for Goose executor timeout configuration."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

_PROMPT_TIMEOUT_ENV = "HARNESS_GOOSE_PROMPT_TIMEOUT_S"
_INIT_TIMEOUT_ENV = "HARNESS_GOOSE_INIT_TIMEOUT_S"

_PRINT_TIMEOUTS = (
    "from omnigent.inner.goose_executor import ("
    "_PROMPT_TIMEOUT_SECONDS, _INIT_TIMEOUT_SECONDS); "
    "print(_PROMPT_TIMEOUT_SECONDS); print(_INIT_TIMEOUT_SECONDS)"
)


def _subprocess_env(**overrides: str) -> dict[str, str]:
    env = os.environ.copy()
    env.pop(_PROMPT_TIMEOUT_ENV, None)
    env.pop(_INIT_TIMEOUT_ENV, None)
    env.update(overrides)
    return env


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _PRINT_TIMEOUTS],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_timeouts_default_when_unset() -> None:
    result = _run(_subprocess_env())

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["300.0", "30.0"]


@pytest.mark.parametrize(
    ("env_var", "override", "expected"),
    [
        (_PROMPT_TIMEOUT_ENV, "600", "600.0"),
        (_INIT_TIMEOUT_ENV, "60", "60.0"),
    ],
)
def test_timeout_override_honored(env_var: str, override: str, expected: str) -> None:
    result = _run(_subprocess_env(**{env_var: override}))

    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    if env_var == _PROMPT_TIMEOUT_ENV:
        assert lines[0] == expected
    else:
        assert lines[1] == expected


@pytest.mark.parametrize("env_var", [_PROMPT_TIMEOUT_ENV, _INIT_TIMEOUT_ENV])
def test_timeout_malformed_value_fails_loud(env_var: str) -> None:
    result = _run(_subprocess_env(**{env_var: "not-a-number"}))

    assert result.returncode != 0
    assert env_var in result.stderr.strip().splitlines()[-1]


@pytest.mark.parametrize("env_var", [_PROMPT_TIMEOUT_ENV, _INIT_TIMEOUT_ENV])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_timeout_rejects_non_positive_or_non_finite(env_var: str, value: str) -> None:
    result = _run(_subprocess_env(**{env_var: value}))

    assert result.returncode != 0
    assert env_var in result.stderr.strip().splitlines()[-1]


def test_timeouts_are_independent() -> None:
    result = _run(_subprocess_env(**{_PROMPT_TIMEOUT_ENV: "600"}))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["600.0", "30.0"]

    result = _run(_subprocess_env(**{_INIT_TIMEOUT_ENV: "60"}))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["300.0", "60.0"]
