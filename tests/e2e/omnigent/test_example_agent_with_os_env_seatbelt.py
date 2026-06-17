"""Structural test for the macOS-seatbelt-sandboxed os_env example
(``tests/resources/examples/agent_with_os_env_seatbelt.yaml``).

The macOS sibling of ``agent_with_os_env_bwrap``: same ``sys_os_*`` tools, but
the helper subprocess runs under the ``darwin_seatbelt`` sandbox
(``sandbox-exec`` profile, read-only cwd, dotfile masking, egress allowlist).
Pure spec-load — the live sandbox only engages on macOS at run time, so this
guard locks the distinctive ``sandbox: type: darwin_seatbelt`` + egress wiring
rather than depending on the credential-gated one-shot.

What breaks if this fails:
- the spec parser regresses on ``os_env.sandbox`` of type ``darwin_seatbelt``,
- the egress allowlist drops off the sandbox spec,
- the variant silently degrades to ``sandbox: type: none`` (no isolation).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

# tests/e2e/omnigent/test_example_agent_with_os_env_seatbelt.py -> repo root 3 up.
_SEATBELT_YAML = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "resources"
    / "examples"
    / "agent_with_os_env_seatbelt.yaml"
)


@pytest.fixture(scope="module")
def seatbelt_spec() -> AgentSpec:
    """Load and validate the seatbelt-sandboxed example once for the module."""
    return load(_SEATBELT_YAML)


def test_uses_darwin_seatbelt_sandbox(seatbelt_spec: AgentSpec) -> None:
    """
    The example wires an ``os_env`` block whose sandbox is ``darwin_seatbelt``.
    Degrading to ``type: none`` (or dropping the sandbox) would silently remove
    the isolation the example exists to demonstrate.
    """
    assert seatbelt_spec.os_env is not None
    assert seatbelt_spec.os_env.type == "caller_process"
    assert seatbelt_spec.os_env.sandbox is not None
    assert seatbelt_spec.os_env.sandbox.type == "darwin_seatbelt"


def test_egress_allowlist_present(seatbelt_spec: AgentSpec) -> None:
    """
    The sandbox pins an egress allowlist (httpbin GET routes). Losing it would
    either open unrestricted network or strip the reachability the example
    relies on — both regressions worth failing on.
    """
    sandbox = seatbelt_spec.os_env.sandbox
    assert sandbox.egress_rules == [
        "GET httpbin.org/get",
        "GET httpbin.org/status/*",
    ]
