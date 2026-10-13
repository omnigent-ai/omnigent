"""Aggregate graded runs into ``benchmark.json`` and a Markdown summary."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from statistics import fmean, pstdev
from typing import Any

from dev.evals.omnigent_guide.cases import Case
from dev.evals.omnigent_guide.record import INFRA_ERROR, GradedRun

ARMS = ("baseline", "treatment")


def summarize(runs: Iterable[GradedRun], cases: Mapping[str, Case]) -> dict[str, Any]:
    """
    Compute per-harness, per-arm statistics over *runs*.

    Runs with an infrastructure error are counted but left out of every rate.

    :param runs: Graded runs from any mix of harnesses and arms.
    :param cases: Cases by id, for ``should_trigger``.
    :returns: ``{harness: {arm: stats, ..., "delta": {...}}}``.
    """
    buckets: dict[str, dict[str, list[GradedRun]]] = {}
    for run in runs:
        record = run.record
        buckets.setdefault(record.harness, {}).setdefault(record.arm, []).append(run)
    summary: dict[str, Any] = {}
    for harness, by_arm in sorted(buckets.items()):
        arms = {arm: _arm_stats(by_arm.get(arm, []), cases) for arm in ARMS if arm in by_arm}
        if len(arms) == len(ARMS):
            arms["delta"] = {
                key: _delta(arms["treatment"][key], arms["baseline"][key])
                for key in ("trigger_rate", "false_trigger_rate", "pass_rate", "tokens", "seconds")
            }
        summary[harness] = arms
    return summary


def per_case(runs: Iterable[GradedRun], cases: Mapping[str, Case]) -> list[dict[str, Any]]:
    """
    Per-case load and pass rates for each harness and arm.

    :returns: One row per (harness, case), in case-file order.
    """
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for run in runs:
        record = run.record
        if record.status == INFRA_ERROR:
            continue
        row = rows.setdefault(
            (record.harness, record.case_id),
            {"harness": record.harness, "case": record.case_id, "arms": {}},
        )
        arm = row["arms"].setdefault(record.arm, {"runs": 0, "loaded": 0, "pass_rates": []})
        arm["runs"] += 1
        arm["loaded"] += run.triggered
        if run.pass_rate is not None:
            arm["pass_rates"].append(run.pass_rate)
    order = {case_id: index for index, case_id in enumerate(cases)}
    result = []
    for (_harness, case_id), row in sorted(
        rows.items(), key=lambda kv: (kv[0][0], order[kv[0][1]])
    ):
        row["split"] = cases[case_id].split
        row["should_trigger"] = cases[case_id].should_trigger
        for arm in row["arms"].values():
            rates = arm.pop("pass_rates")
            arm["pass_rate"] = fmean(rates) if rates else None
        result.append(row)
    return result


def _arm_stats(runs: list[GradedRun], cases: Mapping[str, Case]) -> dict[str, Any]:
    ok = [run for run in runs if run.record.status != INFRA_ERROR]
    positives = [run for run in ok if cases[run.record.case_id].should_trigger]
    negatives = [run for run in ok if not cases[run.record.case_id].should_trigger]
    pass_rates = [run.pass_rate for run in positives if run.pass_rate is not None]
    tokens = [run.record.total_tokens for run in ok if run.record.total_tokens is not None]
    costs = [run.record.cost_usd for run in ok if run.record.cost_usd is not None]
    return {
        "runs": len(runs),
        "infra_errors": len(runs) - len(ok),
        "trigger_hits": sum(run.triggered for run in positives),
        "trigger_total": len(positives),
        "trigger_rate": _rate(sum(run.triggered for run in positives), len(positives)),
        "false_trigger_hits": sum(run.triggered for run in negatives),
        "false_trigger_total": len(negatives),
        "false_trigger_rate": _rate(sum(run.triggered for run in negatives), len(negatives)),
        "pass_rate": fmean(pass_rates) if pass_rates else None,
        "pass_rate_stddev": pstdev(pass_rates) if len(pass_rates) > 1 else None,
        "tokens": fmean(tokens) if tokens else None,
        "seconds": fmean(run.record.duration_s for run in ok) if ok else None,
        "cost_usd": fmean(costs) if costs else None,
    }


def _rate(hits: int, total: int) -> float | None:
    return hits / total if total else None


def _delta(after: float | None, before: float | None) -> float | None:
    return None if after is None or before is None else after - before


def render_markdown(
    summaries: Mapping[str, Mapping[str, Any]],
    rows: list[dict[str, Any]],
    meta: Mapping[str, Any],
) -> str:
    """
    Render the PR-ready summary.

    :param summaries: ``{"All cases": summarize(...), "Holdout only": summarize(...)}``.
    :param rows: :func:`per_case` output over all cases.
    :param meta: Run settings: ``model``, ``runs``, ``judge``, and both arms' commits.
    :returns: Markdown text.
    """
    lines = [
        "## Omnigent guide eval",
        "",
        f"Models: {meta['model']}. {meta['runs']} run(s) per case. Judge: {meta['judge']}. "
        f"Baseline `{meta['baseline_commit']}`, treatment `{meta['treatment_commit']}`.",
        "",
    ]
    for title, summary in summaries.items():
        lines += [
            f"### {title}",
            "",
            "| Harness | Build | Guide loaded | Loaded when it should not "
            "| Answer pass rate | Tokens | Time |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for harness, arms in summary.items():
            for arm in ARMS:
                stats = arms.get(arm)
                if stats is None:
                    continue
                lines.append(
                    f"| {harness} | {arm} "
                    f"| {_count(stats['trigger_hits'], stats['trigger_total'])} "
                    f"| {_count(stats['false_trigger_hits'], stats['false_trigger_total'])} "
                    f"| {_pct(stats['pass_rate'], stats['pass_rate_stddev'])} "
                    f"| {_num(stats['tokens'])} | {_secs(stats['seconds'])} |"
                )
        errors = sum(
            arms[a]["infra_errors"] for arms in summary.values() for a in ARMS if a in arms
        )
        if errors:
            lines.append(f"\n{errors} run(s) hit infrastructure errors and are excluded.")
        lines.append("")
    lines += [
        "<details><summary>Per case (answer pass rate, baseline → treatment)</summary>",
        "",
        "| Harness | Case | Split | Baseline | Treatment |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        cells = []
        for arm in ARMS:
            stats = row["arms"].get(arm)
            if stats is None:
                cells.append("—")
            elif row["should_trigger"]:
                cells.append(
                    f"{_pct(stats['pass_rate'])} (loaded {stats['loaded']}/{stats['runs']})"
                )
            else:
                cells.append(f"loaded {stats['loaded']}/{stats['runs']}")
        name = row["case"] if row["should_trigger"] else f"{row['case']} (should not load)"
        lines.append(f"| {row['harness']} | {name} | {row['split']} | {cells[0]} | {cells[1]} |")
    lines += ["", "</details>", ""]
    return "\n".join(lines)


def _count(hits: int, total: int) -> str:
    return "—" if not total else f"{hits}/{total} ({hits / total:.0%})"


def _pct(value: float | None, stddev: float | None = None) -> str:
    if value is None:
        return "—"
    return f"{value:.0%}" if stddev is None else f"{value:.0%} ± {stddev:.0%}"


def _num(value: float | None) -> str:
    return "—" if value is None else f"{value / 1000:.1f}k"


def _secs(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f} s"
