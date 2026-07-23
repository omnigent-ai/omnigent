"""Tests for the background host daemon process entry point."""

from __future__ import annotations

import sys

import pytest

from omnigent import _platform
from omnigent.host import _daemon_entry


def test_daemon_configures_stdio_before_argument_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The standalone daemon receives the same Windows stdio hardening as the CLI."""
    calls: list[None] = []
    monkeypatch.setattr(_platform, "configure_unicode_safe_stdio", lambda: calls.append(None))
    monkeypatch.setattr(sys, "argv", ["omnigent.host._daemon_entry"])

    with pytest.raises(SystemExit):
        _daemon_entry.main()

    assert calls == [None]
