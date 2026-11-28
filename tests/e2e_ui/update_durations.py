"""Refresh shard estimates from complete, successful CI timing artifacts.

Run ``python -m tests.e2e_ui.update_durations ARTIFACT_DIR ...`` after
downloading every ``e2e-ui-timings-*`` artifact for each chosen run.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from statistics import median

_TIMING_FILE = re.compile(r"ui-timings(?:-gw\d+)?\.jsonl")
_PLAN_FIELDS = ("commit", "splits", "markexpr", "keyword")


def _load(path: Path) -> list[dict]:
    try:
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid timing artifact: {path}: {exc}") from exc
    finish = rows[-1] if rows else {}
    if rows[0:1] and rows[0].get("type") == "plan" and finish.get("type") == "finish":
        if finish.get("exitstatus") == 0:
            return rows
    raise ValueError(f"Incomplete or unsuccessful timing artifact: {path}")


def _phase_duration(report: dict, path: Path) -> float:
    duration = report.get("duration")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration < 0
    ):
        raise ValueError(f"Invalid phase duration: {path}: {report.get('nodeid')}")
    return float(duration)


def _clean_total(reports: list[dict]) -> float | None:
    """Sum a single passed attempt. Skips and retries are not ordinary cost."""
    if len(reports) != 3:
        return None
    if {report.get("when") for report in reports} != {"setup", "call", "teardown"}:
        return None
    if any(report.get("attempt") != 0 or report.get("outcome") != "passed" for report in reports):
        return None
    return sum(float(report["duration"]) for report in reports)


def _consume(path: Path, runs: dict[tuple[object, object], dict]) -> None:
    rows = _load(path)
    plan = rows[0]
    for field in ("run_id", "run_attempt", "group", "nodeids", *_PLAN_FIELDS):
        if field not in plan:
            raise ValueError(f"Timing plan missing {field}: {path}")
    nodeids = plan["nodeids"]
    if not isinstance(nodeids, list) or len(nodeids) != len(set(nodeids)):
        raise ValueError(f"Shard plan has duplicate tests: {path}")
    key = (plan["run_id"], plan["run_attempt"])
    meta = {"run_id": key[0], "run_attempt": key[1]}
    meta.update({field: plan[field] for field in _PLAN_FIELDS})
    run = runs.setdefault(key, {"meta": meta, "shards": {}})
    if any(run["meta"][field] != plan[field] for field in _PLAN_FIELDS):
        raise ValueError(f"Inconsistent shard metadata: {path}")
    shard = run["shards"].setdefault(
        plan["group"], {"nodeids": nodeids, "workers": {}, "seen": set()}
    )
    if shard["nodeids"] != nodeids:
        raise ValueError(f"Inconsistent shard plan: {path}")
    worker = plan.get("worker")
    if worker in shard["workers"]:
        raise ValueError(f"Duplicate shard or worker in run {key} group {plan['group']}: {path}")
    phases: dict[str, list[dict]] = defaultdict(list)
    for row in rows[1:-1]:
        if row.get("type") != "phase":
            continue
        nodeid = row.get("nodeid")
        if not isinstance(nodeid, str):
            raise ValueError(f"Phase record missing nodeid: {path}")
        _phase_duration(row, path)
        phases[nodeid].append(row)
    if shard["seen"].intersection(phases):
        raise ValueError(f"Duplicate test in run {key} group {plan['group']}: {path}")
    shard["seen"].update(phases)
    shard["workers"][worker] = phases


def _finish_run(key: tuple[object, object], run: dict, samples: dict[str, list[float]]) -> None:
    splits = run["meta"]["splits"]
    groups = set(run["shards"])
    if not isinstance(splits, int) or splits < 1 or groups != set(range(1, splits + 1)):
        raise ValueError(f"Missing shards in run {key}")
    owned: set[str] = set()
    for group, shard in run["shards"].items():
        if shard["seen"] != set(shard["nodeids"]):
            raise ValueError(f"Phase records do not match shard plan in run {key} group {group}")
        if owned.intersection(shard["nodeids"]):
            raise ValueError(f"Overlapping test plans in run {key}")
        owned.update(shard["nodeids"])
        for nodeid in shard["nodeids"]:
            reports: list[dict] = []
            for worker_phases in shard["workers"].values():
                reports.extend(worker_phases.get(nodeid, []))
            total = _clean_total(reports)
            if total is not None:
                samples[nodeid].append(total)


def summarize(paths: list[Path]) -> dict:
    runs: dict[tuple[object, object], dict] = {}
    for path in paths:
        _consume(path, runs)
    samples: dict[str, list[float]] = defaultdict(list)
    for key, run in runs.items():
        _finish_run(key, run, samples)
    if not samples:
        raise ValueError("No complete passing tests found")
    return {
        "version": 1,
        "sources": [
            {field: run["meta"][field] for field in ("run_id", "run_attempt", *_PLAN_FIELDS)}
            for _, run in sorted(runs.items(), key=lambda item: str(item[0]))
        ],
        "durations": {
            nodeid: max(0.001, round(median(values), 3))
            for nodeid, values in sorted(samples.items())
        },
    }


def artifact_paths(directories: list[Path]) -> list[Path]:
    found = {
        path
        for directory in directories
        for path in directory.rglob("*.jsonl")
        if _TIMING_FILE.fullmatch(path.name)
    }
    return sorted(found)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("durations.json"))
    args = parser.parse_args()
    snapshot = summarize(artifact_paths(args.directories))
    args.output.write_text(json.dumps(snapshot, indent=2) + "\n")
    print(f"Wrote {len(snapshot['durations'])} estimates from {len(snapshot['sources'])} runs")


if __name__ == "__main__":
    main()
