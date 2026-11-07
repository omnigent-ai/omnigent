"""Tests for qwen executor timeout configuration."""

from __future__ import annotations

import os
import subprocess
import sys

_TIMEOUT_ENV = "HARNESS_QWEN_PROMPT_TIMEOUT_S"
_PRINT_TIMEOUT = (
    "from omnigent.inner.qwen_executor import _PROMPT_TIMEOUT_SECONDS; "
    "print(_PROMPT_TIMEOUT_SECONDS)"
)


def _subprocess_env(value: str | None) -> dict[str, str]:
    env = os.environ.copy()
    if value is None:
        env.pop(_TIMEOUT_ENV, None)
    else:
        env[_TIMEOUT_ENV] = value
    return env


def test_prompt_timeout_defaults_and_override() -> None:
    assert (
        subprocess.check_output(
            [sys.executable, "-c", _PRINT_TIMEOUT],
            env=_subprocess_env(None),
            text=True,
            timeout=30,
        ).strip()
        == "300.0"
    )
    assert (
        subprocess.check_output(
            [sys.executable, "-c", _PRINT_TIMEOUT],
            env=_subprocess_env("7200"),
            text=True,
            timeout=30,
        ).strip()
        == "7200.0"
    )


def test_prompt_timeout_malformed_value_fails_loud() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PRINT_TIMEOUT],
        env=_subprocess_env("not-a-number"),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode != 0
    assert _TIMEOUT_ENV in result.stderr
