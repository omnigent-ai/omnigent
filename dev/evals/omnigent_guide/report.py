"""Turn graded runs into per-arm rates, paired deltas with bootstrap CIs, and grader agreement."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from statistics import fmean
from typing import Any

from dev.evals.omnigent_guide.cases import Case
from dev.evals.omnigent_guide.record import INFRA_ERROR, GradedRun
from dev.evals.omnigent_guide.stats import agreement, paired_bootstrap

#: Per-case metrics, each a mean over that case's runs.
METRICS = ("score", "fabricated", "loaded", "yaml_invalid")
_RAW_RE = re.compile(r"raw\.githubusercontent\.com/omnigent-ai/omnigent/([^/\s\"']+)/")
_FETCH_FAILED_RE = re.compile(
    r"404: Not Found|Could not resolve host|Couldn't connect|Operation not permitted|"
    r"\"exit_code\": (?!0[,}])\d+"
)
_HELP_RE = re.compile(r"\bomni(gent)?\b[^\n]*(--help|\s-h\b)")
_VERSION_RE = re.compile(r"\bomni(gent)?\s+--version")
_SOURCE_KEYS = (
    "pinned_own",
    "pinned_other",
    "main",
    "llms_txt",
    "fetch_failed",
    "cli_help",
    "cli_version",
)


def doc_sources(runs: Sequence[GradedRun], version: str) -> dict[str, float]:
    """
    How the agent looked things up, as a share of *runs*.

    ``pinned_own`` counts runs that fetched repo docs at the build's own
    release tag (``v0.18.0`` for 0.18.0); ``main`` counts fetches from main.
    """
    if not runs:
        return {}
    tag = f"v{version}" if version and "dev" not in version else None
    counts: Counter[str] = Counter()
    for run in runs:
        seen: set[str] = set()
        for call in run.record.tool_calls:
            args = str(call.get("arguments") or "")
            out = str(call.get("output") or "")
            for ref in _RAW_RE.findall(args):
                if ref == tag:
                    seen.add("pinned_own")
                else:
                    seen.add("main" if ref == "main" else "pinned_other")
                if _FETCH_FAILED_RE.search(out):
                    seen.add("fetch_failed")
            if "omnigent.ai/llms" in args:
                seen.add("llms_txt")
            if _HELP_RE.search(args):
                seen.add("cli_help")
            if _VERSION_RE.search(args):
                seen.add("cli_version")
        counts.update(seen)
    return {key: counts[key] / len(runs) for key in _SOURCE_KEYS}


def failure_breakdown(runs: Sequence[GradedRun]) -> dict[str, float | None]:
    """Where the lost score sits: answers with a fabrication versus the rest."""
    scored = [(r, r.score) for r in runs if r.score is not None]
    lost = sum(1 - score for _, score in scored)
    fabricated = [score for r, score in scored if r.fabricated]
    clean = [score for r, score in scored if not r.fabricated]
    return {
        "score_fabricated": fmean(fabricated) if fabricated else None,
        "score_clean": fmean(clean) if clean else None,
        "lost_in_fabricated": sum(1 - x for x in fabricated) / lost if lost else None,
    }


def per_case(runs: Sequence[GradedRun], cases: Mapping[str, Case]) -> dict[str, dict[str, float]]:
    """``{metric: {case_id: mean over runs}}`` for one arm, infrastructure errors excluded."""
    buckets: dict[str, dict[str, list[float]]] = {m: defaultdict(list) for m in METRICS}
    for run in runs:
        if run.record.status == INFRA_ERROR:
            continue
        case = cases[run.record.case_id]
        if case.should_trigger and run.score is not None:
            buckets["score"][case.id].append(run.score)
        if case.should_trigger:
            buckets["fabricated"][case.id].append(float(run.fabricated))
            if run.yaml_blocks:
                buckets["yaml_invalid"][case.id].append(float(run.yaml_invalid > 0))
        buckets["loaded"][case.id].append(float(run.triggered))
    return {m: {cid: fmean(v) for cid, v in b.items()} for m, b in buckets.items()}


def _arm_summary(
    runs: Sequence[GradedRun], cases: Mapping[str, Case], version: str = ""
) -> dict[str, Any]:
    ok = [r for r in runs if r.record.status != INFRA_ERROR]
    pos = [r for r in ok if cases[r.record.case_id].should_trigger]
    neg = [r for r in ok if not cases[r.record.case_id].should_trigger]
    metrics = per_case(runs, cases)
    kinds = Counter(f["kind"] for r in pos for f in r.findings if f.get("counts"))
    tokens = [r.record.total_tokens for r in ok if r.record.total_tokens is not None]

    def mean_of(values: dict[str, float], wanted: Sequence[str]) -> float | None:
        chosen = [values[c] for c in wanted if c in values]
        return fmean(chosen) if chosen else None

    positives = sorted({r.record.case_id for r in pos})
    negatives = sorted({r.record.case_id for r in neg})
    return {
        "runs": len(runs),
        "infra_errors": len(runs) - len(ok),
        "cases": len(positives),
        "score": mean_of(metrics["score"], positives),
        "fabricated": mean_of(metrics["fabricated"], positives),
        "loaded": mean_of(metrics["loaded"], positives),
        "false_loads": mean_of(metrics["loaded"], negatives),
        "yaml_invalid": mean_of(metrics["yaml_invalid"], positives),
        "doc_sources": doc_sources(pos, version),
        "failure_breakdown": failure_breakdown(pos),
        "fabrication_kinds": dict(kinds.most_common()),
        "tokens": fmean(tokens) if tokens else None,
        "seconds": fmean(r.record.duration_s for r in ok) if ok else None,
    }


def grader_agreement(runs: Sequence[GradedRun]) -> dict[str, Any]:
    """Agreement between the judges, and between each judge and code where both decided."""
    judge_names = sorted({name for r in runs for c in r.criteria for name in c.judges})
    pairs: dict[str, tuple[list[bool], list[bool]]] = defaultdict(lambda: ([], []))
    for run in runs:
        if run.record.status == INFRA_ERROR:
            continue
        for crit in run.criteria:
            votes = {n: v for n, v in crit.judges.items() if v is not None}
            for i, a in enumerate(judge_names):
                for b in judge_names[i + 1 :]:
                    if a in votes and b in votes:
                        key = (
                            f"{a} vs {b} ({'checked' if crit.code is not None else 'judge-only'})"
                        )
                        pairs[key][0].append(votes[a])
                        pairs[key][1].append(votes[b])
                if a in votes and crit.code is not None:
                    key = f"{a} vs code ({crit.kind})"
                    pairs[key][0].append(votes[a])
                    pairs[key][1].append(crit.code)
    return {key: agreement(a, b) for key, (a, b) in sorted(pairs.items())}


def write_report(
    runs: Sequence[GradedRun],
    cases: Mapping[str, Case],
    out: Path,
    *,
    baseline: str,
    pairs: Sequence[str] = (),
    arms: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write ``report.json`` and ``summary.md`` to *out*; return the report."""
    by_key: dict[tuple[str, str, str], list[GradedRun]] = defaultdict(list)
    for run in runs:
        split = cases[run.record.case_id].split
        by_key[(split, run.record.harness, run.record.arm)].append(run)
    report: dict[str, Any] = {"arms": arms or {}, "splits": {}}
    for split in sorted({k[0] for k in by_key}):
        split_report: dict[str, Any] = {}
        for harness in sorted({k[1] for k in by_key if k[0] == split}):
            arm_runs = {
                arm: rs for (s, h, arm), rs in by_key.items() if s == split and h == harness
            }
            versions = {name: str(meta.get("version", "")) for name, meta in (arms or {}).items()}
            summaries = {
                arm: _arm_summary(rs, cases, versions.get(arm, ""))
                for arm, rs in sorted(arm_runs.items())
            }
            metrics = {arm: per_case(rs, cases) for arm, rs in arm_runs.items()}
            comparisons = []
            wanted = [
                f"{arm}:{baseline}"
                for arm in sorted(arm_runs)
                if arm != baseline and not any(p.startswith(f"{arm}:") for p in pairs)
            ]
            for pair in [*wanted, *pairs]:
                arm, _, base = pair.partition(":")
                if arm not in metrics or base not in metrics:
                    continue
                row: dict[str, Any] = {"arm": arm, "baseline": base}
                for metric in ("score", "fabricated", "loaded"):
                    result = paired_bootstrap(metrics[base][metric], metrics[arm][metric])
                    row[metric] = result.to_json() if result else None
                comparisons.append(row)
            per_cluster: dict[str, dict[str, float]] = defaultdict(dict)
            for arm, m in metrics.items():
                clusters: dict[str, list[float]] = defaultdict(list)
                for cid, value in m["score"].items():
                    clusters[cases[cid].cluster or "-"].append(value)
                for cluster, values in clusters.items():
                    per_cluster[cluster][arm] = fmean(values)
            split_report[harness] = {
                "arms": summaries,
                "comparisons": comparisons,
                "per_case": {arm: m["score"] for arm, m in metrics.items()},
                "per_case_fabricated": {arm: m["fabricated"] for arm, m in metrics.items()},
                "per_cluster": per_cluster,
                "agreement": grader_agreement([r for rs in arm_runs.values() for r in rs]),
            }
        report["splits"][split] = split_report
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    (out / "summary.md").write_text(render_markdown(report))
    print(render_markdown(report))
    return report


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _delta(row: Mapping[str, Any] | None) -> str:
    if not row:
        return "—"
    return f"{row['delta'] * 100:+.0f} pp [{row['low'] * 100:+.0f}, {row['high'] * 100:+.0f}]"


def render_markdown(report: Mapping[str, Any]) -> str:
    """Short tables: per-arm rates, then paired deltas with 95% bootstrap CIs."""
    lines: list[str] = []
    for split, harnesses in report["splits"].items():
        for harness, data in harnesses.items():
            lines += [
                f"### {split} split, {harness}",
                "",
                "| Arm | Cases | Pass rate | Fabricated | Guide loaded "
                "| Loaded on near misses | Invalid YAML | Tokens | Infra errors |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
            for arm, s in data["arms"].items():
                tokens = "—" if s["tokens"] is None else f"{s['tokens'] / 1000:.1f}k"
                cells = [
                    arm,
                    str(s["cases"]),
                    _pct(s["score"]),
                    _pct(s["fabricated"]),
                    _pct(s["loaded"]),
                    _pct(s["false_loads"]),
                    _pct(s["yaml_invalid"]),
                    tokens,
                    str(s["infra_errors"]),
                ]
                lines.append("| " + " | ".join(cells) + " |")
            if data["comparisons"]:
                lines += [
                    "",
                    "| Arm vs baseline | Pass rate Δ [95% CI] | Fabricated Δ [95% CI] "
                    "| Guide loaded Δ [95% CI] |",
                    "| --- | --- | --- | --- |",
                ]
                for row in data["comparisons"]:
                    cells = [
                        f"{row['arm']} vs {row['baseline']}",
                        _delta(row["score"]),
                        _delta(row["fabricated"]),
                        _delta(row["loaded"]),
                    ]
                    lines.append("| " + " | ".join(cells) + " |")
            lines.append("")
    return "\n".join(lines)
