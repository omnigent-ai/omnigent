"""Verdict vocabulary, priorities, and the observed-vs-declared reconciler.

The verdicts map onto the glyphs of the hand-maintained harness support
matrix so a rendered bench report reads like the spreadsheet it replaces,
plus two operational states (``SKIPPED``) and the drift alarm (``DRIFT``)
that the spreadsheet cannot express.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class Verdict(enum.Enum):
    """One cell of the capability matrix.

    :cvar SUPPORTED: Probe ran; behavior confirmed present. Glyph ``✓``.
    :cvar UNSUPPORTED: Probe ran; capability absent (and expected absent
        for this harness). Glyph ``✗``.
    :cvar PARTIAL: Works with caveats, e.g. "complete-only" streaming or
        "TUI-only" gating. Glyph ``~``.
    :cvar NOT_APPLICABLE: Dimension does not apply to this harness, e.g.
        model override on a harness that self-selects its model. Glyph
        ``—``.
    :cvar UNKNOWN: Never probed / no probe written yet. Glyph ``?``.
    :cvar SKIPPED: Probe could not run in this environment (CLI, creds, or
        transport unavailable). Distinct from ``UNKNOWN``: the probe
        exists, the environment could not exercise it.
    :cvar DRIFT: Observed verdict disagrees with the declared verdict. Not
        produced by a probe — computed by :func:`reconcile` at report
        time. Glyph ``!!``.
    """

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    PARTIAL = "partial"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"
    DRIFT = "drift"

    @property
    def glyph(self) -> str:
        """Return the spreadsheet glyph for this verdict."""
        return _GLYPHS[self]


_GLYPHS: dict[Verdict, str] = {
    Verdict.SUPPORTED: "✓",
    Verdict.UNSUPPORTED: "✗",
    Verdict.PARTIAL: "~",
    Verdict.NOT_APPLICABLE: "—",
    Verdict.UNKNOWN: "?",
    Verdict.SKIPPED: "·",
    Verdict.DRIFT: "!!",
}

# Verdicts that assert a concrete, comparable fact. Reconciliation only
# fires drift when BOTH sides are concrete — an UNKNOWN/SKIPPED on either
# side means we never claimed or never observed, so there is nothing to
# disagree about.
_CONCRETE: frozenset[Verdict] = frozenset(
    {Verdict.SUPPORTED, Verdict.UNSUPPORTED, Verdict.PARTIAL, Verdict.NOT_APPLICABLE}
)


class Priority(enum.Enum):
    """Dimension priority, carried from the support matrix.

    ``P0`` dimensions gate merge in the live layer; ``P1`` dimensions are
    reported but non-blocking (they cover newer / less-load-bearing
    capabilities like reasoning forwarding and cost tracking).
    """

    P0 = "P0"
    P1 = "P1"


class Applicability(enum.Enum):
    """Which harness kinds a probe applies to.

    A probe marked ``SDK`` is skipped (``NOT_APPLICABLE``) against native
    harnesses and vice versa; ``BOTH`` runs everywhere.
    """

    SDK = "sdk"
    NATIVE = "native"
    BOTH = "both"


@dataclass(frozen=True)
class ProbeResult:
    """The outcome of running one probe against one harness.

    :param verdict: The observed :class:`Verdict`.
    :param note: Short human-readable evidence, e.g. ``"14 text deltas"``
        or ``"tool call dispatched, result accepted"``. Rendered in the
        detail view and in failure messages.
    :param detail: Optional structured evidence (event-type counts, the
        blocked reason, the effective model) for debugging or JSON
        export. Never rendered in the compact matrix.
    """

    verdict: Verdict
    note: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def skipped(cls, reason: str) -> ProbeResult:
        """Build a ``SKIPPED`` result carrying *reason* as the note."""
        return cls(Verdict.SKIPPED, note=reason)

    @classmethod
    def not_applicable(cls, reason: str = "") -> ProbeResult:
        """Build a ``NOT_APPLICABLE`` result."""
        return cls(Verdict.NOT_APPLICABLE, note=reason)


def reconcile(observed: Verdict, declared: Verdict) -> Verdict:
    """Compare an observed verdict against the harness's declared verdict.

    Returns :attr:`Verdict.DRIFT` when both sides assert a concrete fact
    and those facts differ — the alarm the whole bench exists to raise
    (a harness that *claims* a capability but no longer exhibits it, or
    the reverse). Otherwise returns *observed* unchanged.

    Drift is symmetric on purpose: a capability that regressed
    (declared ``SUPPORTED``, observed ``UNSUPPORTED``) and one that
    quietly gained coverage (declared ``UNSUPPORTED``, observed
    ``SUPPORTED``) both mean the matrix is now lying, and both deserve a
    human's attention.

    :param observed: The verdict a probe produced this run.
    :param declared: The verdict the :class:`BenchProfile` claims.
    :returns: ``DRIFT`` on a concrete mismatch, else *observed*.
    """
    if observed in _CONCRETE and declared in _CONCRETE and observed != declared:
        return Verdict.DRIFT
    return observed
