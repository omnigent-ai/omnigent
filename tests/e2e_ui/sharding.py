"""Deterministic UI shard planning and portable pytest timing records."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from collections import defaultdict
from pathlib import Path

import pytest


def read_durations(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text())
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("durations"), dict)
    ):
        raise ValueError(f"{path}: expected version 1 with a durations object")
    durations = data["durations"]
    for nodeid, seconds in durations.items():
        if not isinstance(nodeid, str) or type(seconds) not in (int, float):
            raise ValueError(f"{path}: durations must map test IDs to seconds")
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError(f"{path}: durations must be finite and nonnegative")
    return durations


def plan_shards(
    nodeids: list[str], splits: int, durations: dict[str, float]
) -> tuple[list[list[str]], list[float]]:
    """Balance longest tests first, retaining collection order in each shard."""
    if splits < 1 or len(nodeids) != len(set(nodeids)):
        raise ValueError("positive shard count and unique test IDs required")
    known = sorted(durations[n] for n in nodeids if n in durations)
    if not known:
        return [nodeids[i::splits] for i in range(splits)], [0.0] * splits
    # New tests receive a conservative estimate from the selected suite.
    default = max(known[math.ceil(len(known) * 0.75) - 1], 1.0)
    weights = {n: durations.get(n, default) for n in nodeids}
    loads = [0.0] * splits
    counts = [0] * splits
    assignments: dict[str, int] = {}
    for nodeid in sorted(nodeids, key=lambda n: (-weights[n], n)):
        group = min(range(splits), key=lambda i: (loads[i], counts[i], i))
        assignments[nodeid] = group
        loads[group] += weights[nodeid]
        counts[group] += 1
    return [[n for n in nodeids if assignments[n] == i] for i in range(splits)], loads


def collection_digest(nodeids: list[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(nodeids)).encode()).hexdigest()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--splits", type=int, help="Total UI shards.")
    parser.addoption("--group", type=int, help="1-indexed UI shard to execute.")
    parser.addoption("--ui-duration-file", type=Path, help="Shared versioned duration snapshot.")
    parser.addoption("--ui-timing-output", type=Path, help="Write per-phase JSONL timing records.")


class TimingRecorder:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")

    def write(self, record: dict) -> None:
        with self.path.open("a") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self.write(
            {
                "type": "phase",
                "nodeid": report.nodeid,
                "when": report.when,
                "outcome": report.outcome,
                "duration": report.duration,
                "attempt": getattr(report, "rerun", 0),
            }
        )

    def pytest_sessionfinish(self, exitstatus: int) -> None:
        self.write({"type": "finish", "exitstatus": int(exitstatus)})


def pytest_configure(config: pytest.Config) -> None:
    path = config.getoption("--ui-timing-output")
    if path is not None:
        if config.getoption("numprocesses", default=0):
            raise pytest.UsageError("--ui-timing-output requires serial pytest within each shard")
        config.pluginmanager.register(TimingRecorder(path), "ui-timing-recorder")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    splits, group = config.getoption("--splits"), config.getoption("--group")
    if (splits is None) != (group is None):
        raise pytest.UsageError("--splits and --group must be passed together")
    if splits is not None and (splits < 1 or not 1 <= group <= splits):
        raise pytest.UsageError("require --splits >= 1 and 1 <= --group <= --splits")
    nodeids = [item.nodeid for item in items]
    path = config.getoption("--ui-duration-file")
    try:
        durations = read_durations(path) if path is not None else {}
        shards, loads = plan_shards(nodeids, splits or 1, durations)
    except (OSError, ValueError) as exc:
        raise pytest.UsageError(str(exc)) from exc
    selected = set(shards[(group or 1) - 1])
    recorder = config.pluginmanager.get_plugin("ui-timing-recorder")
    if recorder is not None:
        recorder.write(
            {
                "type": "plan",
                "version": 1,
                "splits": splits or 1,
                "group": group or 1,
                "collection_digest": collection_digest(nodeids),
                "collection_count": len(nodeids),
                "nodeids": [n for n in nodeids if n in selected],
                "estimated_seconds": loads,
                "unknown_count": sum(n not in durations for n in nodeids),
                "snapshot_digest": hashlib.sha256(path.read_bytes()).hexdigest() if path else None,
                "run_id": os.environ.get("GITHUB_RUN_ID"),
                "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
                "commit": os.environ.get("GITHUB_SHA"),
                "markexpr": config.option.markexpr,
                "keyword": config.option.keyword,
            }
        )
    if splits is not None:
        deselected = [item for item in items if item.nodeid not in selected]
        items[:] = [item for item in items if item.nodeid in selected]
        config.hook.pytest_deselected(items=deselected)
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(
                f"UI shard {group}/{splits}: {len(items)}/{len(nodeids)} tests; "
                f"estimated loads (s): {[round(s) for s in loads]}"
            )


def build_snapshot(paths: list[Path]) -> dict:
    """Accept only a complete successful run whose shards partition collection."""
    plans = []
    durations: dict[str, float] = {}
    for path in paths:
        records = [json.loads(line) for line in path.read_text().splitlines()]
        if not records or records[0].get("type") != "plan" or records[0].get("version") != 1:
            raise ValueError(f"{path}: missing versioned plan")
        plan = records[0]
        plans.append(plan)
        expected_exit = 0 if plan["nodeids"] else int(pytest.ExitCode.NO_TESTS_COLLECTED)
        if records[-1] != {"type": "finish", "exitstatus": expected_exit}:
            raise ValueError(f"{path}: incomplete or unsuccessful pytest run")
        phases: dict[str, list[dict]] = defaultdict(list)
        for record in records[1:-1]:
            if record.get("type") != "phase" or record["nodeid"] not in plan["nodeids"]:
                raise ValueError(f"{path}: report outside shard assignment")
            if not math.isfinite(record["duration"]) or record["duration"] < 0:
                raise ValueError(f"{path}: invalid phase duration")
            phases[record["nodeid"]].append(record)
        if set(phases) != set(plan["nodeids"]):
            raise ValueError(f"{path}: missing test results")
        for nodeid, reports in phases.items():
            # Skips do not teach us how long an executable test takes.
            if any(r["when"] == "call" and r["outcome"] == "passed" for r in reports):
                durations[nodeid] = round(sum(r["duration"] for r in reports), 6)
    if not plans:
        raise ValueError("no timing files supplied")
    first = plans[0]
    shared = (
        "splits",
        "collection_digest",
        "collection_count",
        "snapshot_digest",
        "run_id",
        "run_attempt",
        "commit",
        "markexpr",
        "keyword",
    )
    if any(any(p[k] != first[k] for k in shared) for p in plans):
        raise ValueError("timing files use different collections, snapshots or runs")
    if sorted(p["group"] for p in plans) != list(range(1, first["splits"] + 1)):
        raise ValueError("need exactly one timing file per shard")
    nodeids = [n for p in plans for n in p["nodeids"]]
    if (
        len(nodeids) != len(set(nodeids))
        or len(nodeids) != first["collection_count"]
        or collection_digest(nodeids) != first["collection_digest"]
    ):
        raise ValueError("shards do not partition the full collection exactly once")
    return {
        "version": 1,
        "source_run_id": first["run_id"],
        "source_commit": first["commit"],
        "durations": dict(sorted(durations.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("timings", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    snapshot = build_snapshot(args.timings)
    args.output.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    values = list(snapshot["durations"].values())
    print(f"Validated all shards; recorded {len(values)} tests, {sum(values):.1f} test-seconds")


if __name__ == "__main__":
    main()
