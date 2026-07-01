"""The capability-probe contract.

A probe measures one dimension of the support matrix against one harness.
Probes are harness-agnostic: they call the transport driver's small
surface and return a :class:`ProbeResult`. Adding a dimension means adding
a probe module and listing it in :data:`tests.harness_bench.probes.ALL_PROBES`
— no per-harness code anywhere.
"""

from __future__ import annotations

import abc

from tests.harness_bench.driver import SdkInprocDriver
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.verdict import Applicability, Priority, ProbeResult


class CapabilityProbe(abc.ABC):
    """One capability dimension, measurable against any harness.

    Subclasses set :attr:`name`, :attr:`priority`, and
    :attr:`applies_to`, and implement :meth:`run`.

    :cvar name: Stable dimension key, also the key into
        :attr:`BenchProfile.declared` for reconciliation, e.g.
        ``"streaming"``.
    :cvar title: Human-readable column header for the report.
    :cvar priority: ``P0`` (merge-gating in the live layer) or ``P1``.
    :cvar applies_to: Which harness kinds the probe is meaningful for.
    """

    name: str
    title: str
    priority: Priority
    applies_to: Applicability = Applicability.BOTH

    @abc.abstractmethod
    async def run(self, driver: SdkInprocDriver, profile: BenchProfile) -> ProbeResult:
        """Exercise the dimension and return the observed verdict.

        :param driver: A live driver already bound to *profile*'s harness.
        :param profile: The harness under test.
        :returns: The observed :class:`ProbeResult`. Raising is allowed;
            the bench catches it and records an ``UNKNOWN`` with the
            exception text so one broken probe never masks the rest.
        """
        raise NotImplementedError
