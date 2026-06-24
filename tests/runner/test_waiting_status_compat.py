"""
Unit tests for the runner's session.status "waiting" backwards-compat gate.

The runner emits ``session.status: "waiting"`` (PR #930) only to servers new
enough to serialize it; older servers (< 0.3.0) 500 on ``GET /v1/sessions``, so
the runner downgrades "waiting"→"running" for them. Here we test the pure
version-comparison; the probe + downgrade are exercised end-to-end by
tests/e2e/test_waiting_status_compat_e2e.py (old server + new runner -> no 500).
"""

from __future__ import annotations

import pytest

from omnigent.runner.app import _version_supports_waiting_status


@pytest.mark.parametrize(
    ("server_version", "expected"),
    [
        ("0.2.0", False),  # the released version that 500s on "waiting"
        ("0.2.5", False),
        ("0.1.1", False),
        ("0.3.0", True),  # first release that models "waiting"
        ("0.3.0.dev0", True),  # main: dev of the supporting release still supports it
        ("0.3.1", True),
        ("0.4.0", True),  # later minor: still supports "waiting"
        ("1.0.0", True),
    ],
)
def test_version_supports_waiting_status(server_version: str, expected: bool) -> None:
    assert _version_supports_waiting_status(server_version) is expected
