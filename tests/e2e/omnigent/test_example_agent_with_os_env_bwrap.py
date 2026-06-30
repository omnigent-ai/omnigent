"""Structural test for the bubblewrap-sandboxed os_env example
(``tests/resources/examples/agent_with_os_env_bwrap.yaml``).

The opt-in hardened (Linux) variant of ``agent_with_os_env``: same ``sys_os_*``
tools, but the helper subprocess runs inside the ``linux_bwrap`` launcher
(fresh namespaces, read-only cwd, dotfile masking, egress allowlist). Pure
spec-load — the actual sandbox only engages on Linux at run time, so the
credential-gated one-shot can't assert it on a macOS CI host; this guard locks
the distinctive ``sandbox: type: linux_bwrap`` + egress wiring instead.

What breaks if this fails:
- the spec parser regresses on ``os_env.sandbox`` of type ``linux_bwrap``,
- the egress allowlist drops off the sandbox spec (the agent would gain
  unrestricted network, or lose its intended httpbin reachability),
- the variant silently degrades to ``sandbox: type: none`` (no isolation).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

# tests/e2e/omnigent/test_example_agent_with_os_env_bwrap.py -> repo root 3 up.
_BWRAP_YAML = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "resources"
    / "examples"
    / "agent_with_os_env_bwrap.yaml"
)


@pytest.fixture(scope="module")
def bwrap_spec() -> AgentSpec:
    """Load and validate the bwrap-sandboxed example once for the module."""
    return load(_BWRAP_YAML)


def test_uses_linux_bwrap_sandbox(bwrap_spec: AgentSpec) -> None:
    """
    The example wires an ``os_env`` block whose sandbox is ``linux_bwrap``.
    Degrading to ``type: none`` (or dropping the sandbox) would silently remove
    the isolation the example exists to demonstrate.
    """
    assert bwrap_spec.os_env is not None
    assert bwrap_spec.os_env.type == "caller_process"
    assert bwrap_spec.os_env.sandbox is not None
    assert bwrap_spec.os_env.sandbox.type == "linux_bwrap"


def test_egress_allowlist_present(bwrap_spec: AgentSpec) -> None:
    """
    The sandbox pins an egress allowlist (httpbin GET routes). Losing it would
    either open unrestricted network or strip the reachability the example
    relies on — both regressions worth failing on.
    """
    sandbox = bwrap_spec.os_env.sandbox
    assert sandbox.egress_rules == [
        "GET httpbin.org/get",
        "GET httpbin.org/status/*",
    ]
