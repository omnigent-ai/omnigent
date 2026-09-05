"""Tests for native tool-relay timeout configuration."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

_TIMEOUT_ENV = "HARNESS_TOOL_RELAY_TIMEOUT_S"
_PRINT_TIMEOUTS = (
    "from omnigent.claude_native_bridge import "
    "_TOOL_CALL_TIMEOUT_S, _TOOL_RELAY_POST_TIMEOUT_S; "
    "print(_TOOL_CALL_TIMEOUT_S, _TOOL_RELAY_POST_TIMEOUT_S)"
)
_PRINT_CHILD_TIMEOUT = (
    "from pathlib import Path; "
    "from omnigent.claude_native_bridge import build_mcp_config; "
    "server = build_mcp_config(Path('/tmp/bridge'))['mcpServers']['omnigent']; "
    f"print(server['env'].get('{_TIMEOUT_ENV}', '<missing>'))"
)


def _subprocess_env(value: str | None) -> dict[str, str]:
    env = os.environ.copy()
    if value is None:
        env.pop(_TIMEOUT_ENV, None)
    else:
        env[_TIMEOUT_ENV] = value
    return env


def test_tool_relay_timeout_defaults_and_override() -> None:
    assert (
        subprocess.check_output(
            [sys.executable, "-c", _PRINT_TIMEOUTS],
            env=_subprocess_env(None),
            text=True,
        ).strip()
        == "300.0 330.0"
    )
    assert (
        subprocess.check_output(
            [sys.executable, "-c", _PRINT_TIMEOUTS],
            env=_subprocess_env("7200"),
            text=True,
        ).strip()
        == "7200.0 7230.0"
    )


def test_tool_relay_timeout_malformed_value_fails_loud() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PRINT_TIMEOUTS],
        env=_subprocess_env("not-a-number"),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert _TIMEOUT_ENV in result.stderr.strip().splitlines()[-1]


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_tool_relay_timeout_rejects_non_positive_or_non_finite_values(value: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PRINT_TIMEOUTS],
        env=_subprocess_env(value),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert _TIMEOUT_ENV in result.stderr.strip().splitlines()[-1]


def test_serve_mcp_entry_carries_explicit_resolved_timeout() -> None:
    assert (
        subprocess.check_output(
            [sys.executable, "-c", _PRINT_CHILD_TIMEOUT],
            env=_subprocess_env("028800"),
            text=True,
        ).strip()
        == "28800.0"
    )


def test_serve_mcp_entry_omits_timeout_without_override() -> None:
    assert (
        subprocess.check_output(
            [sys.executable, "-c", _PRINT_CHILD_TIMEOUT],
            env=_subprocess_env(None),
            text=True,
        ).strip()
        == "<missing>"
    )
