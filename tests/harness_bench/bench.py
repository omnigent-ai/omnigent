"""The bench orchestrator: run probes across harnesses into a matrix.

Sequential by design. Each harness spawns one wrap subprocess with a
single in-flight turn per conversation, so its probes run one after
another over a shared driver; harnesses run one at a time to keep the
subprocess and gateway load bounded.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from tests.harness_bench.probes import ALL_PROBES, CapabilityProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.transport import resolve_driver_class
from tests.harness_bench.verdict import Applicability, Priority, ProbeResult, Verdict, reconcile

# A progress sink: the bench calls it with human-readable status lines as it
# spawns harnesses and runs probes. ``None`` (the default) stays silent, which
# is what the pytest layer wants; the CLI passes a stderr writer so a live run
# is not silent for minutes.
Progress = Callable[[str], None]

# The prerequisite probe: if it does not pass, the harness cannot be exercised
# at all, so the remaining probes are skipped rather than run against a dead
# turn (which would otherwise emit misleading UNSUPPORTED/DRIFT noise).
_PREREQ_PROBE = "basic_turn"


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


def _emit(progress: Progress | None, message: str) -> None:
    if progress is not None:
        progress(message)


async def run_harness(
    profile: BenchProfile,
    *,
    probes: list[CapabilityProbe] | None = None,
    databricks_profile: str | None = None,
    live: bool = True,
    transport: str | None = None,
    progress: Progress | None = None,
) -> HarnessReport:
    """Run every applicable probe against one harness.

    :param profile: The harness under test.
    :param probes: Probes to run; defaults to :data:`ALL_PROBES`.
    :param databricks_profile: Gateway profile for live turns. Required
        for ``live=True``; its absence skips the whole harness.
    :param live: When ``False``, produce a declared-only report (every
        cell ``SKIPPED`` with an "offline" note) without spawning
        anything — used for a fast ``--list``/dry render.
    :param transport: ``--transport`` override; wins over the profile's
        declared transport when set (see :func:`resolve_driver_class`).
    :param progress: Optional status sink called with human-readable lines
        as the harness spawns and each probe runs. ``None`` stays silent.
    :returns: The :class:`HarnessReport`.
    """
    probes = probes if probes is not None else ALL_PROBES

    if not live:
        return _uniform_report(profile, probes, ProbeResult.skipped("offline (declared shown)"))

    driver_cls = resolve_driver_class(profile, override=transport)
    unavailable = driver_cls.unavailable(profile, databricks_profile=databricks_profile)
    if unavailable is not None:
        _emit(progress, f"[{profile.harness}] skipped: {unavailable}")
        return _uniform_report(
            profile, probes, ProbeResult.skipped(unavailable), skipped_reason=unavailable
        )

    assert databricks_profile is not None  # guaranteed by the unavailable() check
    _emit(
        progress,
        f"[{profile.harness}] provisioning {driver_cls.transport} transport "
        f"(model={profile.model}); first turn may take ~10-30s...",
    )
    cells: list[CellResult] = []
    async with driver_cls(profile, databricks_profile=databricks_profile) as driver:
        prereq_skip: str | None = None
        for probe in probes:
            if not _applicable(probe, profile):
                cells.append(_cell(probe, profile, ProbeResult.not_applicable()))
                continue
            if prereq_skip is not None:
                observed = ProbeResult.skipped(prereq_skip)
                _emit(progress, f"[{profile.harness}]   {probe.title}: skipped (prerequisite)")
            else:
                _emit(progress, f"[{profile.harness}]   {probe.title}: running...")
                try:
                    observed = await probe.run(driver, profile)
                except Exception as exc:
                    observed = ProbeResult(Verdict.UNKNOWN, note=f"probe raised: {exc!r}")
                _emit(
                    progress,
                    f"[{profile.harness}]   {probe.title}: {observed.verdict.name}"
                    + (f" ({observed.note})" if observed.note else ""),
                )
            cell = _cell(probe, profile, observed)
            cells.append(cell)
            # If the prerequisite turn did not pass, short-circuit the rest:
            # they would only re-hit the same failure and pollute the matrix.
            if probe.name == _PREREQ_PROBE and cell.observed is not Verdict.SUPPORTED:
                prereq_skip = f"prerequisite '{probe.title}' did not pass ({observed.note})"
    return HarnessReport(profile=profile, cells=cells)


async def run_bench(
    profiles: list[BenchProfile],
    *,
    probes: list[CapabilityProbe] | None = None,
    databricks_profile: str | None = None,
    live: bool = True,
    transport: str | None = None,
    progress: Progress | None = None,
) -> BenchMatrix:
    """Run the bench across *profiles*, sequentially, into a :class:`BenchMatrix`."""
    reports = [
        await run_harness(
            p,
            probes=probes,
            databricks_profile=databricks_profile,
            live=live,
            transport=transport,
            progress=progress,
        )
        for p in profiles
    ]
    return BenchMatrix(reports=reports)
