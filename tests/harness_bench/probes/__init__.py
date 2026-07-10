"""Capability probes and their ordered registry."""

from __future__ import annotations

from tests.harness_bench.probes.base import CapabilityProbe
from tests.harness_bench.probes.basic_turn import BasicTurnProbe
from tests.harness_bench.probes.cost_tracking import CostTrackingProbe
from tests.harness_bench.probes.interrupt import InterruptProbe
from tests.harness_bench.probes.model_override import ModelOverrideProbe
from tests.harness_bench.probes.policy_allow import PolicyAllowProbe
from tests.harness_bench.probes.policy_ask import PolicyAskProbe
from tests.harness_bench.probes.policy_deny import PolicyDenyProbe
from tests.harness_bench.probes.streaming import StreamingProbe
from tests.harness_bench.probes.tool_calling import ToolCallingProbe

# Basic turn gates the run; interrupt stays last because cancellation can linger.
ALL_PROBES: list[CapabilityProbe] = [
    BasicTurnProbe(),
    StreamingProbe(),
    ToolCallingProbe(),
    PolicyDenyProbe(),
    PolicyAllowProbe(),
    PolicyAskProbe(),
    ModelOverrideProbe(),
    CostTrackingProbe(),
    InterruptProbe(),
]

__all__ = ["ALL_PROBES", "CapabilityProbe"]
