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


def paired_ratio_bootstrap(
    baseline: Mapping[str, tuple[float, float]],
    arm: Mapping[str, tuple[float, float]],
    *,
    reps: int = 10_000,
    seed: int = 7,
) -> Paired | None:
    """
    Bootstrap the difference of two pooled ratios by resampling cases.

    Each value is ``(numerator, denominator)`` summed over a case's runs, such
    as invented and named commands. A resample draws cases with replacement and
    compares ``sum(numerator) / sum(denominator)`` for each arm; resamples where
    either denominator is zero are skipped.

    :param baseline: Per-case ``(numerator, denominator)`` for the baseline.
    :param arm: The same for the compared arm.
    :param reps: Bootstrap resamples.
    :param seed: Fixed so reports are reproducible.
    :returns: ``None`` when either arm's denominator is zero over all shared cases.
    """
    shared = sorted(set(baseline) & set(arm))

    def ratio(values: Mapping[str, tuple[float, float]], chosen: Sequence[str]) -> float | None:
        denominator = sum(values[case][1] for case in chosen)
        return sum(values[case][0] for case in chosen) / denominator if denominator else None

    base_all, arm_all = ratio(baseline, shared), ratio(arm, shared)
    if base_all is None or arm_all is None:
        return None
    rng = random.Random(seed)
    deltas = []
    for _ in range(reps):
        sample = rng.choices(shared, k=len(shared))
        base, compared = ratio(baseline, sample), ratio(arm, sample)
        if base is not None and compared is not None:
            deltas.append(compared - base)
    deltas.sort()
    return Paired(
        cases=len(shared),
        baseline=base_all,
        arm=arm_all,
        delta=arm_all - base_all,
        low=deltas[int(0.025 * len(deltas))],
        high=deltas[int(0.975 * len(deltas)) - 1],
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
