"""P0 capability probes and their ordered registry.

:data:`ALL_PROBES` is the single list the bench iterates; append a probe
here to add a dimension. Order is the report's column order:
``basic_turn`` first (the prerequisite), then the capabilities.
"""

from __future__ import annotations

from tests.harness_bench.probes.base import CapabilityProbe
from tests.harness_bench.probes.basic_turn import BasicTurnProbe
from tests.harness_bench.probes.interrupt import InterruptProbe
from tests.harness_bench.probes.model_override import ModelOverrideProbe
from tests.harness_bench.probes.policy_deny import PolicyDenyProbe
from tests.harness_bench.probes.streaming import StreamingProbe
from tests.harness_bench.probes.tool_calling import ToolCallingProbe

# Order is the report's column order AND the run order. basic_turn is first
# (the prerequisite short-circuit). interrupt is LAST because cancelling a
# turn can leave a harness's session mid-processing, which would contaminate
# any probe that runs after it on the same shared session (e.g. pi rejecting
# the next turn with "Agent is already processing").
ALL_PROBES: list[CapabilityProbe] = [
    BasicTurnProbe(),
    StreamingProbe(),
    ToolCallingProbe(),
    PolicyDenyProbe(),
    ModelOverrideProbe(),
    InterruptProbe(),
]

__all__ = ["ALL_PROBES", "CapabilityProbe"]
