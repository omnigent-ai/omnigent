"""The bench orchestrator: run probes across harnesses into a matrix.

Sequential by design. Each harness spawns one wrap subprocess with a
single in-flight turn per conversation, so its probes run one after
another over a shared driver; harnesses run one at a time to keep the
subprocess and gateway load bounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tests.harness_bench.driver import SdkInprocDriver
from tests.harness_bench.probes import ALL_PROBES, CapabilityProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.verdict import Applicability, Priority, ProbeResult, Verdict, reconcile


@dataclass(frozen=True)
class CellResult:
    """One dimension's outcome for one harness (a matrix cell).

    :param observed: The raw verdict the probe produced.
    :param declared: The verdict the profile claims.
    :param verdict: The reconciled verdict — equals *observed* unless the
        two concrete facts disagree, in which case ``DRIFT``.
    """

    probe_name: str
    title: str
    priority: Priority
    observed: Verdict
    declared: Verdict
    verdict: Verdict
    note: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def is_drift(self) -> bool:
        return self.verdict is Verdict.DRIFT


@dataclass(frozen=True)
class HarnessReport:
    """Every cell for one harness, plus a whole-harness skip reason."""

    profile: BenchProfile
    cells: list[CellResult]
    skipped_reason: str | None = None

    @property
    def has_drift(self) -> bool:
        return any(c.is_drift for c in self.cells)


@dataclass(frozen=True)
class BenchMatrix:
    """The full run: one :class:`HarnessReport` per harness."""

    reports: list[HarnessReport]

    @property
    def has_drift(self) -> bool:
        return any(r.has_drift for r in self.reports)


def _is_native(profile: BenchProfile) -> bool:
    """Whether *profile* names a native harness (drives the applicability gate)."""
    return profile.transport not in {"sdk-inproc"}


def _applicable(probe: CapabilityProbe, profile: BenchProfile) -> bool:
    if probe.applies_to is Applicability.BOTH:
        return True
    if probe.applies_to is Applicability.NATIVE:
        return _is_native(profile)
    return not _is_native(profile)


def _cell(probe: CapabilityProbe, profile: BenchProfile, observed: ProbeResult) -> CellResult:
    declared = profile.declared_for(probe.name)
    return CellResult(
        probe_name=probe.name,
        title=probe.title,
        priority=probe.priority,
        observed=observed.verdict,
        declared=declared,
        verdict=reconcile(observed.verdict, declared),
        note=observed.note,
        detail=observed.detail,
    )


def _uniform_report(
    profile: BenchProfile,
    probes: list[CapabilityProbe],
    observed: ProbeResult,
    *,
    skipped_reason: str | None = None,
) -> HarnessReport:
    """A report where every applicable probe shares one *observed* result.

    Used for the offline layer (all ``SKIPPED``) and for a harness the
    driver cannot run (whole-harness skip), so the matrix still shows the
    declared column and the skip reason per cell.
    """
    cells = [
        _cell(
            probe,
            profile,
            observed if _applicable(probe, profile) else ProbeResult.not_applicable(),
        )
        for probe in probes
    ]
    return HarnessReport(profile=profile, cells=cells, skipped_reason=skipped_reason)


async def run_harness(
    profile: BenchProfile,
    *,
    probes: list[CapabilityProbe] | None = None,
    databricks_profile: str | None = None,
    live: bool = True,
) -> HarnessReport:
    """Run every applicable probe against one harness.

    :param profile: The harness under test.
    :param probes: Probes to run; defaults to :data:`ALL_PROBES`.
    :param databricks_profile: Gateway profile for live turns. Required
        for ``live=True``; its absence skips the whole harness.
    :param live: When ``False``, produce a declared-only report (every
        cell ``SKIPPED`` with an "offline" note) without spawning
        anything — used for a fast ``--list``/dry render.
    :returns: The :class:`HarnessReport`.
    """
    probes = probes if probes is not None else ALL_PROBES

    if not live:
        return _uniform_report(profile, probes, ProbeResult.skipped("offline (declared shown)"))

    unavailable = SdkInprocDriver.unavailable(profile, databricks_profile=databricks_profile)
    if unavailable is not None:
        return _uniform_report(
            profile, probes, ProbeResult.skipped(unavailable), skipped_reason=unavailable
        )

    assert databricks_profile is not None  # guaranteed by the unavailable() check
    cells: list[CellResult] = []
    async with SdkInprocDriver(profile, databricks_profile=databricks_profile) as driver:
        for probe in probes:
            if not _applicable(probe, profile):
                cells.append(_cell(probe, profile, ProbeResult.not_applicable()))
                continue
            try:
                observed = await probe.run(driver, profile)
            except Exception as exc:
                observed = ProbeResult(Verdict.UNKNOWN, note=f"probe raised: {exc!r}")
            cells.append(_cell(probe, profile, observed))
    return HarnessReport(profile=profile, cells=cells)


async def run_bench(
    profiles: list[BenchProfile],
    *,
    probes: list[CapabilityProbe] | None = None,
    databricks_profile: str | None = None,
    live: bool = True,
) -> BenchMatrix:
    """Run the bench across *profiles*, sequentially, into a :class:`BenchMatrix`."""
    reports = [
        await run_harness(p, probes=probes, databricks_profile=databricks_profile, live=live)
        for p in profiles
    ]
    return BenchMatrix(reports=reports)
