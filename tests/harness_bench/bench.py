"""The bench orchestrator: run probes across harnesses into a matrix.

Probes *within* a harness are sequential — they share one driver/session with
a single in-flight turn per conversation. Harnesses run one at a time by
default (``jobs=1``); ``jobs>1`` runs up to N concurrently, bounded by a
semaphore, to cut wall-clock while keeping process/gateway load capped. Under a
parallel run, full-server harnesses share one server+runner (see
:func:`_maybe_shared_full_server`) rather than each booting their own.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from tests.harness_bench.driver import ProvisioningError
from tests.harness_bench.events import (
    HarnessFinished,
    HarnessSkipped,
    HarnessStarted,
    LineSink,
    ProbeFinished,
    ProbeStarted,
    ProgressSink,
)
from tests.harness_bench.full_server import SharedFullServer
from tests.harness_bench.probes import ALL_PROBES, CapabilityProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.transport import resolve_driver_class, resolve_transport_name
from tests.harness_bench.verdict import Applicability, Priority, ProbeResult, Verdict, reconcile

_logger = logging.getLogger(__name__)

# Back-compat: a plain per-line progress callback. The orchestrator now emits
# structured events to a ProgressSink; callers that still pass a line callback
# get it adapted to a LineSink. ``None`` stays silent (the pytest layer).
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
    """Every cell for one harness, plus a whole-harness skip reason.

    :param transport: The transport that actually ran this harness (the
        *resolved* driver, e.g. ``full-server`` for an SDK harness on the
        default), which can differ from ``profile.transport`` — that field is
        the harness *family* marker, not the effective driver.
    """

    profile: BenchProfile
    cells: list[CellResult]
    skipped_reason: str | None = None
    transport: str | None = None

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
    transport: str | None = None,
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
    return HarnessReport(
        profile=profile, cells=cells, skipped_reason=skipped_reason, transport=transport
    )


def _as_sink(progress: Progress | ProgressSink | None) -> ProgressSink | None:
    """Normalize the ``progress`` argument to a :class:`ProgressSink`.

    Accepts a structured sink (used as-is), a plain line callback (adapted to a
    :class:`~tests.harness_bench.events.LineSink`), or ``None`` (silent).
    """
    if progress is None:
        return None
    if isinstance(progress, ProgressSink):
        return progress
    return LineSink(progress)  # a bare callable → line output


def _emit(sink: ProgressSink | None, event) -> None:
    if sink is not None:
        sink.emit(event)


async def run_harness(
    profile: BenchProfile,
    *,
    probes: list[CapabilityProbe] | None = None,
    databricks_profile: str | None = None,
    live: bool = True,
    transport: str | None = None,
    fast: bool = False,
    progress: Progress | ProgressSink | None = None,
    shared_full_server=None,
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
        family default (see :func:`resolve_driver_class`).
    :param fast: ``--fast`` — downgrade the SDK family to sdk-inproc (skip the
        server boot, trading Tool calling + Policy DENY coverage).
    :param progress: A :class:`ProgressSink` (structured events), a plain
        per-line callback (adapted), or ``None`` (silent).
    :param shared_full_server: An optional shared
        :class:`~tests.harness_bench.full_server_driver.SharedFullServer` to
        register this harness on, instead of the driver spawning its own
        server+runner. Only used when the resolved driver is the full-server
        driver; ignored otherwise.
    :returns: The :class:`HarnessReport`.
    """
    probes = probes if probes is not None else ALL_PROBES
    sink = _as_sink(progress)

    if not live:
        # Offline: still resolve the transport a live run *would* pick, so the
        # declared matrix labels each row with its effective transport.
        resolved = resolve_transport_name(profile, override=transport, fast=fast)
        return _uniform_report(
            profile, probes, ProbeResult.skipped("offline (declared shown)"), transport=resolved
        )

    driver_cls = resolve_driver_class(profile, override=transport, fast=fast)
    resolved_transport = driver_cls.transport
    unavailable = driver_cls.unavailable(profile, databricks_profile=databricks_profile)
    if unavailable is not None:
        _emit(sink, HarnessSkipped(profile.harness, unavailable, resolved_transport))
        return _uniform_report(
            profile,
            probes,
            ProbeResult.skipped(unavailable),
            skipped_reason=unavailable,
            transport=resolved_transport,
        )

    assert databricks_profile is not None  # guaranteed by the unavailable() check
    _emit(sink, HarnessStarted(profile.harness, driver_cls.transport, profile.model))
    cells: list[CellResult] = []
    # Only the full-server driver accepts a shared server; pass it through when
    # this harness resolved to that transport, else construct plainly.
    if shared_full_server is not None and driver_cls.transport == "full-server":
        driver_cm = driver_cls(
            profile, databricks_profile=databricks_profile, shared=shared_full_server
        )
    else:
        driver_cm = driver_cls(profile, databricks_profile=databricks_profile)
    try:
        entered = await driver_cm.__aenter__()
    except Exception as exc:
        # Provisioning failed (e.g. an own-auth native whose vendor CLI is
        # installed but not logged in, so its terminal never wires up). Report
        # a capability-neutral skip for this harness rather than aborting the
        # whole run — a multi-harness run must survive one unrunnable harness.
        #
        # __aenter__ may have already spawned the server + daemon and opened a
        # client before raising, so tear those down here or they leak for the
        # rest of the run (_teardown null-checks each, so a half-provisioned
        # driver is safe to tear down).
        #
        # An expected ProvisioningError (a known-unrunnable environment) logs
        # only its reason — the matrix already shows the skip. Any *other*
        # exception is a possible driver bug (e.g. an AssertionError), so keep
        # its full traceback rather than letting it vanish behind a green skip.
        if isinstance(exc, ProvisioningError):
            _logger.info("skipping %s: %s", profile.harness, exc)
        else:
            _logger.warning("provisioning failed for %s", profile.harness, exc_info=True)
        with contextlib.suppress(Exception):
            await driver_cm.__aexit__(type(exc), exc, exc.__traceback__)
        reason = f"provisioning failed: {exc}"
        _emit(sink, HarnessSkipped(profile.harness, reason, resolved_transport))
        return _uniform_report(
            profile,
            probes,
            ProbeResult.skipped(reason),
            skipped_reason=reason,
            transport=resolved_transport,
        )
    try:
        driver = entered
        prereq_skip: str | None = None
        for probe in probes:
            if not _applicable(probe, profile):
                cells.append(_cell(probe, profile, ProbeResult.not_applicable()))
                continue
            if prereq_skip is not None:
                observed = ProbeResult.skipped(prereq_skip)
            else:
                _emit(sink, ProbeStarted(profile.harness, probe.name, probe.title))
                try:
                    observed = await probe.run(driver, profile)
                except Exception as exc:
                    observed = ProbeResult(Verdict.UNKNOWN, note=f"probe raised: {exc!r}")
            _emit(
                sink,
                ProbeFinished(
                    profile.harness, probe.name, probe.title, observed.verdict, observed.note
                ),
            )
            cell = _cell(probe, profile, observed)
            cells.append(cell)
            # If the prerequisite turn did not pass, short-circuit the rest:
            # they would only re-hit the same failure and pollute the matrix.
            if probe.name == _PREREQ_PROBE and cell.observed is not Verdict.SUPPORTED:
                prereq_skip = f"prerequisite '{probe.title}' did not pass ({observed.note})"
    finally:
        await driver_cm.__aexit__(None, None, None)
    _emit(sink, HarnessFinished(profile.harness))
    return HarnessReport(profile=profile, cells=cells, transport=resolved_transport)


async def run_bench(
    profiles: list[BenchProfile],
    *,
    probes: list[CapabilityProbe] | None = None,
    databricks_profile: str | None = None,
    live: bool = True,
    transport: str | None = None,
    fast: bool = False,
    progress: Progress | ProgressSink | None = None,
    jobs: int = 1,
) -> BenchMatrix:
    """Run the bench across *profiles* into a :class:`BenchMatrix`.

    :param jobs: Max harnesses to run concurrently. ``1`` (default) is the
        original sequential behavior. ``>1`` runs up to *jobs* harnesses at
        once, bounded by a semaphore. Probes *within* a harness always run
        sequentially — they share one driver/session with a single in-flight
        turn — so concurrency is only across harnesses. Report order always
        matches *profiles* order regardless of finish order.

        For full-server harnesses under ``jobs`` > 1, one shared server+runner
        is spawned and every full-server harness registers its own agent +
        session on it (the runner resolves the harness per session), instead of
        each harness booting its own server. native-tui harnesses still
        self-provision (each needs its own host daemon).
    """
    async with _maybe_shared_full_server(
        profiles,
        databricks_profile=databricks_profile,
        live=live,
        transport=transport,
        fast=fast,
        jobs=jobs,
    ) as shared:
        if jobs <= 1:
            reports = [
                await run_harness(
                    p,
                    probes=probes,
                    databricks_profile=databricks_profile,
                    live=live,
                    transport=transport,
                    fast=fast,
                    progress=progress,
                    shared_full_server=shared,
                )
                for p in profiles
            ]
            return BenchMatrix(reports=reports)

        semaphore = asyncio.Semaphore(jobs)

        async def _one(p: BenchProfile) -> HarnessReport:
            async with semaphore:
                return await run_harness(
                    p,
                    probes=probes,
                    databricks_profile=databricks_profile,
                    live=live,
                    transport=transport,
                    fast=fast,
                    progress=progress,
                    shared_full_server=shared,
                )

        # gather preserves input order, so the matrix stays in *profiles* order
        # even though harnesses finish out of order.
        reports = await asyncio.gather(*(_one(p) for p in profiles))
        return BenchMatrix(reports=list(reports))


@contextlib.asynccontextmanager
async def _maybe_shared_full_server(
    profiles: list[BenchProfile],
    *,
    databricks_profile: str | None,
    live: bool,
    transport: str | None,
    fast: bool,
    jobs: int,
):
    """Yield a shared full-server for parallel full-server runs, else ``None``.

    Only stands one up when it actually helps: a live, parallel run with more
    than one harness that resolves to the full-server transport. Otherwise
    yields ``None`` and each harness provisions as before (a solo full-server
    run still owns its own server, unchanged).
    """
    shared = None
    if live and jobs > 1 and databricks_profile is not None:
        full = [
            p
            for p in profiles
            if resolve_driver_class(p, override=transport, fast=fast).transport == "full-server"
        ]
        if len(full) > 1:
            shared = SharedFullServer(databricks_profile)
            await asyncio.to_thread(shared.__enter__)
    try:
        yield shared
    finally:
        if shared is not None:
            await asyncio.to_thread(shared.__exit__, None, None, None)
