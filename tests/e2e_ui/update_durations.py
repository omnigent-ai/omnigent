"""Refresh shard estimates from complete, successful CI timing artifacts.

Run ``python -m tests.e2e_ui.update_durations ARTIFACT_DIR ...`` after downloading
all ``e2e-ui-timings-*`` artifacts for each chosen successful workflow run.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import median


def summarize(paths: list[Path]) -> dict:
    samples = defaultdict(list)
    runs = {}
    for path in paths:
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        plan = rows[0]
        if plan["type"] != "plan" or rows[-1] != {"type": "finish", "exitstatus": 0}:
            raise ValueError(f"Incomplete or unsuccessful timing artifact: {path}")
        key = (plan["run_id"], plan["run_attempt"])
        run = runs.setdefault(
            key,
            {
                "run_id": key[0],
                "run_attempt": key[1],
                "commit": plan["commit"],
                "splits": plan["splits"],
                "markexpr": plan["markexpr"],
                "keyword": plan["keyword"],
                "groups": set(),
                "nodeids": set(),
            },
        )
        if any(run[field] != plan[field] for field in ("commit", "splits", "markexpr", "keyword")):
            raise ValueError(f"Inconsistent shard metadata: {path}")
        if plan["group"] in run["groups"] or run["nodeids"].intersection(plan["nodeids"]):
            raise ValueError(f"Duplicate shard or test in run {key}: {path}")
        run["groups"].add(plan["group"])
        run["nodeids"].update(plan["nodeids"])
        phases = defaultdict(list)
        for row in rows[1:-1]:
            if row["type"] == "phase":
                phases[row["nodeid"]].append(row)
        if set(phases) != set(plan["nodeids"]):
            raise ValueError(f"Phase records do not match shard plan: {path}")
        for nodeid, reports in phases.items():
            # Skips and retries underestimate or inflate ordinary execution cost.
            if (
                len(reports) == 3
                and {r["when"] for r in reports} == {"setup", "call", "teardown"}
                and all(r["attempt"] == 0 and r["outcome"] == "passed" for r in reports)
            ):
                durations = [r["duration"] for r in reports]
                if any(not math.isfinite(d) or d < 0 for d in durations):
                    raise ValueError(f"Invalid phase duration: {path}: {nodeid}")
                samples[nodeid].append(sum(durations))
    for key, run in runs.items():
        if run["groups"] != set(range(1, run["splits"] + 1)):
            raise ValueError(f"Missing shards in run {key}")
    if not samples:
        raise ValueError("No complete passing tests found")
    return {
        "version": 1,
        "sources": [
            {k: v for k, v in run.items() if k not in ("groups", "nodeids")}
            for _, run in sorted(runs.items())
        ],
        "durations": {n: max(0.001, round(median(v), 3)) for n, v in sorted(samples.items())},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("durations.json"))
    args = parser.parse_args()
    paths = sorted(
        {p for directory in args.directories for p in directory.rglob("ui-timings.jsonl")}
    )
    snapshot = summarize(paths)
    args.output.write_text(json.dumps(snapshot, indent=2) + "\n")
    print(f"Wrote {len(snapshot['durations'])} estimates from {len(snapshot['sources'])} runs")


if __name__ == "__main__":
    main()
