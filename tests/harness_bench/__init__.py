"""Harness capability test bench.

A standardized, pluggable conformance suite that probes a harness and
reports a verdict per capability dimension (basic turn, streaming,
tool calling, interrupt, policy DENY, model override, ...), reconciling
observed behavior against a self-declared :class:`BenchProfile` to
surface drift.

Design: ``docs/harness-bench-design.md``.

Two entry points:

- ``python -m tests.harness_bench --harness <name>`` renders the matrix
  for one harness (or all official harnesses with no ``--harness``).
- ``tests/harness_bench/test_bench.py`` runs the offline conformance
  layer on every PR and the live-probe layer when a ``--profile`` and
  the harness CLI are available.
"""

from __future__ import annotations

from tests.harness_bench.profile import BenchProfile, resolve_profile
from tests.harness_bench.verdict import (
    Applicability,
    Priority,
    ProbeResult,
    Verdict,
    reconcile,
)

__all__ = [
    "Applicability",
    "BenchProfile",
    "Priority",
    "ProbeResult",
    "Verdict",
    "reconcile",
    "resolve_profile",
]
