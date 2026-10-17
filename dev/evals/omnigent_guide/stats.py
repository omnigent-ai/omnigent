"""Paired comparisons with bootstrap confidence intervals over cases, and grader agreement."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import fmean


@dataclass(frozen=True)
class Paired:
    """Mean of (arm - baseline) over cases both arms answered, with a 95% CI."""

    cases: int
    baseline: float
    arm: float
    delta: float
    low: float
    high: float

    def to_json(self) -> dict[str, float | int]:
        return self.__dict__.copy()


def paired_bootstrap(
    baseline: Mapping[str, float],
    arm: Mapping[str, float],
    *,
    reps: int = 10_000,
    seed: int = 7,
) -> Paired | None:
    """
    Bootstrap the mean per-case difference by resampling cases.

    :param baseline: Per-case value (already averaged over runs) for the baseline.
    :param arm: The same for the compared arm.
    :param reps: Bootstrap resamples.
    :param seed: Fixed so reports are reproducible.
    :returns: ``None`` when no case has both values.
    """
    shared = sorted(set(baseline) & set(arm))
    if not shared:
        return None
    diffs = [arm[case] - baseline[case] for case in shared]
    rng = random.Random(seed)
    means = sorted(fmean(rng.choices(diffs, k=len(diffs))) for _ in range(reps))
    return Paired(
        cases=len(shared),
        baseline=fmean(baseline[case] for case in shared),
        arm=fmean(arm[case] for case in shared),
        delta=fmean(diffs),
        low=means[int(0.025 * reps)],
        high=means[int(0.975 * reps) - 1],
    )


def cohen_kappa(a: Sequence[bool], b: Sequence[bool]) -> float | None:
    """Cohen's kappa for two binary raters over the same items; ``None`` if undefined."""
    if len(a) != len(b) or not a:
        return None
    n = len(a)
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    pa, pb = sum(a) / n, sum(b) / n
    expected = pa * pb + (1 - pa) * (1 - pb)
    if expected == 1:
        return None
    return (observed - expected) / (1 - expected)


def agreement(a: Sequence[bool], b: Sequence[bool]) -> dict[str, float | int | None]:
    """Raw agreement and kappa for two raters."""
    n = len(a)
    return {
        "items": n,
        "agree": (sum(x == y for x, y in zip(a, b, strict=True)) / n) if n else None,
        "kappa": cohen_kappa(a, b),
    }
