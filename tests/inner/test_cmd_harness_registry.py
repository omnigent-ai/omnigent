"""Registry tests for the Command Code (``cmd``) harness.

Drift here means a spec with ``harness: cmd`` fails at runner spawn
(unknown harness), at spec-load (not in the allowlist), or at runtime
dispatch. Mirrors the peer harness registry tests.
"""

from __future__ import annotations

from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.spec._omnigent_compat import OMNIGENT_HARNESSES


def test_cmd_harness_registered() -> None:
    """The runtime resolves ``"cmd"`` to the cmd harness wrap module."""
    assert _HARNESS_MODULES.get("cmd") == "omnigent.inner.cmd_harness"


def test_cmd_harness_accepted_by_spec_compat() -> None:
    """The spec-load allowlist accepts ``harness: cmd``.

    A spec with ``harness: cmd`` must not fail at parse time with
    "must be one of [...], got 'cmd'".
    """
    assert "cmd" in OMNIGENT_HARNESSES
