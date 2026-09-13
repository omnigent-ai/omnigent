"""Tests for the ``harness: devin`` wrap (:mod:`omnigent.inner.devin.harness`).

The wrap is the only thing that distinguishes a Devin harness process from a
generic ACP one, so these pin the injection itself. Everything below the wrap is
shared ACP code and is covered by ``tests/inner/test_acp_executor.py``.
"""

from __future__ import annotations

import pytest

from omnigent.harness_plugins import harness_capabilities, harness_modules
from omnigent.inner import acp_harness
from omnigent.inner.acp_executor import AcpExecutor
from omnigent.inner.acp_extension import NO_ACP_EXTENSION
from omnigent.inner.devin import DEVIN_ACP_EXTENSION
from omnigent.inner.devin import harness as devin_harness


def test_create_app_injects_the_devin_extension(monkeypatch: pytest.MonkeyPatch) -> None:
    """Devin's wrap delegates to the shared ACP wrap with Devin's extension.

    **What breaks if this fails**: the Devin harness runs as a plain ACP agent —
    everything still works except the vendor behavior, silently. That is the
    failure mode composition-over-discovery trades for, so it is asserted here.
    """
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        acp_harness,
        "create_app",
        lambda extension=NO_ACP_EXTENSION: captured.setdefault("extension", extension),
    )

    devin_harness.create_app()

    assert captured["extension"] is DEVIN_ACP_EXTENSION


def test_shared_builder_carries_the_extension_onto_the_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The injected extension reaches the executor the adapter drives."""
    monkeypatch.setenv("HARNESS_ACP_COMMAND", "devin acp")

    ex = acp_harness._build_acp_executor(DEVIN_ACP_EXTENSION)
    assert isinstance(ex, AcpExecutor)
    assert ex._extension is DEVIN_ACP_EXTENSION


def test_shared_builder_defaults_to_no_vendor_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    """The generic ``acp`` harness builds a protocol-only executor.

    The other half of the injection: a builtin ACP row that declares no vendor
    behavior (Grok, a user's ``acp:<slug>``) must not inherit Devin's dialect.
    """
    monkeypatch.setenv("HARNESS_ACP_COMMAND", "grok agent stdio")

    ex = acp_harness._build_acp_executor()
    assert isinstance(ex, AcpExecutor)
    assert ex._extension is NO_ACP_EXTENSION
    assert ex._extension.subagent_sources == ()


def test_registry_points_devin_at_its_own_wrap() -> None:
    """``harness: devin`` resolves to this wrap, not the shared one.

    The registry entry is what makes the injection happen at all — without it
    Devin gets the generic wrap and the extension is never constructed.
    """
    assert harness_modules()["devin"] == "omnigent.inner.devin.harness"
    # Sibling ACP rows keep the shared wrap.
    assert harness_modules()["grok"] == "omnigent.inner.acp_harness"
    assert harness_modules()["acp"] == "omnigent.inner.acp_harness"


def test_declared_capability_is_derived_from_the_extension() -> None:
    """Devin's ``subagents`` capability tracks the extension, not a hand-edit.

    ``/v1/harnesses`` publishes this matrix, so a dialect added or removed
    without updating the declaration would publish a false capability.
    """
    caps = harness_capabilities()
    assert caps["devin"].subagents is DEVIN_ACP_EXTENSION.surfaces_subagents is True
    # Only Devin diverges from the shared generic ACP profile, and only there.
    assert caps["grok"] == caps["acp"]
    assert caps["devin"] != caps["acp"]
