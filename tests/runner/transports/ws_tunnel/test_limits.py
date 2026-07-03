"""Tests for the WS tunnel size limits constants."""

from __future__ import annotations

from omnigent.runner.transports.ws_tunnel.limits import RUNNER_TUNNEL_MAX_MESSAGE_BYTES


def test_max_message_bytes_is_100mb() -> None:
    """The tunnel message size limit matches the design spec: 100 MiB."""
    assert RUNNER_TUNNEL_MAX_MESSAGE_BYTES == 100 * 1024 * 1024


def test_max_message_bytes_is_positive_int() -> None:
    """The constant is a positive integer, not a float or zero."""
    assert isinstance(RUNNER_TUNNEL_MAX_MESSAGE_BYTES, int)
    assert RUNNER_TUNNEL_MAX_MESSAGE_BYTES > 0
