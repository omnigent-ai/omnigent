"""Harness-bench model-override false-positive e2e test (native-TUI transport).

Reproduces the bug: the ``model_override`` probe reports **SUPPORTED** for a
native-TUI harness (``pi-native`` and every native-TUI harness) purely because a
turn completed, *without the caller-specified model ever being sent to the
harness*.

Why this is a false positive:

- ``ModelOverrideProbe`` (``tests/harness_bench/probes/model_override.py``)
  assumes, per its own docstring, that "the driver launches the harness with the
  profile's model in ``{env_prefix}MODEL``". Its live half then returns
  ``SUPPORTED`` from ``result.completed and result.text`` alone.
- The native-TUI driver (``tests/harness_bench/native_tui_driver.py``) never
  threads ``profile.model`` anywhere: it has **zero** references to
  ``profile.model`` / ``env_prefix`` / ``MODEL``, and the probe-facing contract
  ``Driver.run_basic_turn(marker)`` carries only the marker — never a model. Live
  confirmation: the driver provisions ``pi`` on the config-default model
  (``databricks-claude-sonnet-4-5``) while the profile's caller-specified model
  is ``databricks-claude-sonnet-4-6``; the driver never asks for the latter.
- Because the profile *declares* ``model_override=SUPPORTED`` for native
  harnesses, the false ``SUPPORTED`` observation reconciles to ``SUPPORTED`` with
  **no DRIFT** — so the matrix silently hides that native-TUI never actually
  routed the caller's model.

This test drives the **real** bench pipeline (``run_harness`` — the same entry
``python -m tests.harness_bench --harness pi-native --dimension model_override
--live`` invokes) and the **real** ``BasicTurnProbe`` + ``ModelOverrideProbe``.
Following the repo's established probe-test convention (see
``tests/harness_bench/test_bench.py``'s ``resolve_driver_class`` monkeypatch and
``_OKDriver``), it injects a *native-TUI-faithful* driver: it completes a turn
with text exactly as the real driver does on the happy path, and records every
model it was ever asked to route — which, matching the real driver, is nothing.

Running the real subprocess command against a live gateway is what the reporter
did; it is exercised in-process here because the vendored ``pi`` Node CLI cannot
tunnel a locked-down egress proxy (so a full ``--live`` subprocess run would SKIP
on the basic-turn prerequisite rather than reach the ``SUPPORTED`` verdict). The
probe-logic false positive being reproduced is environment-independent: it
depends only on a turn completing with text, which is the documented happy path.

Regression contract (fail on buggy code, pass once fixed): the ``model_override``
probe must not report ``SUPPORTED`` when the caller-specified model was never
routed to the driver. Today it does — this test fails on ``main``. After a fix
(the probe verifies the model actually routed, or native-TUI honestly reports it
cannot route a caller-specified model), it passes.

Usage::

    python -m pytest tests/e2e/test_harness_bench_model_override_native_tui_e2e.py -v
"""

from __future__ import annotations

import pytest

from tests.harness_bench.bench import run_harness
from tests.harness_bench.driver import ForkResult, TurnResult
from tests.harness_bench.manifest import OFFICIAL_PROFILES
from tests.harness_bench.probes.basic_turn import BasicTurnProbe
from tests.harness_bench.probes.model_override import ModelOverrideProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.verdict import Verdict


class _NativeTuiFaithfulDriver:
    """A native-TUI driver stand-in faithful to the real one's contract.

    Mirrors ``tests/harness_bench/native_tui_driver.py``: a basic turn completes
    with text, and the only thing the probe-facing contract carries is the
    marker (``run_basic_turn(marker)``) — never a model. It records every model
    it was asked to route so the test can assert the caller-specified model was
    never sent (the real driver routes the config default, not ``profile.model``).
    """

    transport = "native-tui"

    # ``run_harness`` instantiates the resolved driver class itself, so the test
    # recovers the live instance through this registry.
    instances: list[_NativeTuiFaithfulDriver] = []

    def __init__(self, profile: BenchProfile, *, databricks_profile: str | None = None) -> None:
        self.profile = profile
        # Every marker the probe pipeline drove a basic turn with.
        self.basic_turn_markers: list[str] = []
        # Every model the driver was ever explicitly asked to route on. The real
        # native-TUI driver has no such channel, so this stays empty — the whole
        # point of the bug.
        self.routed_models: list[str] = []
        _NativeTuiFaithfulDriver.instances.append(self)

    @staticmethod
    def unavailable(profile: BenchProfile, *, databricks_profile: str | None) -> str | None:
        return None

    async def __aenter__(self) -> _NativeTuiFaithfulDriver:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def run_basic_turn(self, marker: str) -> TurnResult:
        # Happy-path native-TUI behaviour: a turn completes and echoes the
        # marker. The caller-specified model is *not* an argument here — exactly
        # the gap the bug exploits.
        self.basic_turn_markers.append(marker)
        return TurnResult(completed=True, text=marker)

    # Remaining Driver-protocol methods so the pipeline stays well-formed even if
    # the selected probe set grows; unused by this test's probes.
    async def run_streaming_turn(self) -> TurnResult:
        return TurnResult(completed=True, text_delta_count=5)

    async def run_reasoning_turn(self) -> TurnResult:
        return TurnResult(completed=True, reasoning_delta_count=2)

    async def run_tool_turn(self, *, deny: bool) -> TurnResult:
        return TurnResult(completed=True)

    async def run_mcp_tool_turn(self) -> TurnResult:
        return TurnResult(completed=True)

    async def run_fork_turn(self, marker: str) -> ForkResult:
        return ForkResult(created=True, history_copied=True, recalled=True)

    async def run_policy_turn(self, *, action: str) -> TurnResult:
        return TurnResult(completed=True)

    async def run_interrupt_turn(self) -> TurnResult:
        return TurnResult(cancelled=True)


async def test_model_override_not_supported_without_routing_model_native_tui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """model_override must not report SUPPORTED when the caller's model is never routed.

    Drives the real bench pipeline for ``pi-native`` with the real basic-turn +
    model-override probes and a native-TUI-faithful driver. On ``main`` the probe
    reports ``SUPPORTED`` from mere turn completion though the driver was never
    asked to route ``profile.model`` — this assertion fails there. After a fix
    that ties the verdict to the model actually routing, it passes.
    """
    profile = OFFICIAL_PROFILES["pi-native"]
    assert profile.transport == "native-tui", "test targets the native-TUI transport"
    # The profile declares model_override SUPPORTED, so a false SUPPORTED
    # observation hides as a no-drift cell — part of why the bug is silent.
    assert profile.declared_for("model_override") is Verdict.SUPPORTED

    _NativeTuiFaithfulDriver.instances.clear()
    monkeypatch.setattr(
        "tests.harness_bench.bench.resolve_driver_class",
        lambda p, *, override=None, fast=False: _NativeTuiFaithfulDriver,
    )

    report = await run_harness(
        profile,
        probes=[BasicTurnProbe(), ModelOverrideProbe()],
        databricks_profile="oss",
        live=True,
    )

    assert _NativeTuiFaithfulDriver.instances, "the bench pipeline never built the driver"
    driver = _NativeTuiFaithfulDriver.instances[-1]

    cells = {c.probe_name: c for c in report.cells}
    assert "model_override" in cells, "model_override cell missing from report"
    mo = cells["model_override"]

    # Sanity: the prerequisite turn completed with text (the condition the probe
    # misreads), and the caller-specified model was never communicated anywhere.
    assert cells["basic_turn"].observed is Verdict.SUPPORTED
    model_was_routed = profile.model in driver.routed_models or any(
        profile.model in marker for marker in driver.basic_turn_markers
    )
    assert not model_was_routed, (
        "test premise broken: the caller-specified model was routed to the driver "
        f"(routed_models={driver.routed_models}, markers={driver.basic_turn_markers})"
    )

    # Regression invariant: with the caller-specified model
    # never routed, model_override must not be reported SUPPORTED. On buggy code
    # it is SUPPORTED with the note "turn routed on caller-specified model
    # 'databricks-claude-sonnet-4-6'" — a false positive from a bare completion.
    assert mo.observed is not Verdict.SUPPORTED, (
        "model_override regression: reported SUPPORTED for pi-native "
        f"(note={mo.note!r}) although the caller-specified model {profile.model!r} "
        f"was never sent to the driver (routed_models={driver.routed_models}, "
        f"basic_turn_markers={driver.basic_turn_markers}). A completing turn was "
        "misread as proof the override routed; the declared SUPPORTED then hides "
        "it as a no-drift cell."
    )
