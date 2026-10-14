"""Assemble ``questions.yaml`` from the drawn sample, its answer keys, and near misses.

Splits follow each cluster's kept order (two test, then one dev), so a
replaced question does not shift the test/dev ratio. Near misses are split
with the same seed. The result is the file ``run.py freeze`` locks::

    uv run --no-sync python -m dev.evals.omnigent_guide.assemble \\
        --keys .eval/mining/keys_*.yaml --negatives .eval/mining/history_negatives.jsonl \\
        --draw .eval/mining/draw.jsonl --out dev/evals/omnigent_guide/questions.yaml
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

_HEADER = """\
# Real questions about Omnigent, with answer keys derived from the code.
#
# Positives were mined from GitHub issues and discussions and from one user's
# Omnigent session history (paraphrased and scrubbed), embedded, clustered
# with k-means, and drawn at random within clusters. A drawn question was
# replaced by the next draw from its cluster only when it had no answer the
# code or docs establish (bug-only, private, outside, duplicate); see
# README.md. Near misses are real questions about other tools.
#
# The test split is frozen by questions.lock and scored once per arm.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--keys", type=Path, nargs="+", required=True)
    parser.add_argument("--negatives", type=Path, required=True)
    parser.add_argument("--draw", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--skips", type=Path, default=None, help="Write the skip log here.")
    parser.add_argument("--seed", type=int, default=20261014)
    args = parser.parse_args(argv)

    draw = {
        json.loads(line)["pool_id"]: json.loads(line)
        for line in args.draw.read_text().splitlines()
        if line.strip()
    }
    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for path in args.keys:
        data = yaml.safe_load(path.read_text())
        kept.extend(data.get("kept", []))
        skipped.extend(data.get("skipped", []))

    by_cluster: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for entry in kept:
        by_cluster[draw[entry["pool_id"]]["cluster_id"]].append(entry)
    cases: list[dict[str, Any]] = []
    for cluster_id in sorted(by_cluster):
        entries = sorted(by_cluster[cluster_id], key=lambda e: draw[e["pool_id"]]["draw_rank"])
        for rank, entry in enumerate(entries):
            cases.append(
                {
                    "id": entry["id"],
                    "split": "dev" if rank % 3 == 2 else "test",
                    "origin": entry["origin"],
                    "cluster": entry["cluster"],
                    "should_trigger": True,
                    "prompt": entry["prompt"].strip(),
                    "criteria": entry["criteria"],
                }
            )

    negatives = [
        json.loads(line) for line in args.negatives.read_text().splitlines() if line.strip()
    ]
    rng = random.Random(args.seed)
    rng.shuffle(negatives)
    for index, neg in enumerate(negatives):
        cases.append(
            {
                "id": f"near-miss-{index + 1:02d}",
                "split": "dev" if index % 3 == 2 else "test",
                "origin": "session history",
                "cluster": f"near-miss: {neg.get('topic', '')}".strip(),
                "should_trigger": False,
                "prompt": neg["question"].strip(),
            }
        )

    ids = [c["id"] for c in cases]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise SystemExit(f"duplicate case ids: {duplicates}")
    text = (
        _HEADER
        + "\n"
        + yaml.safe_dump({"cases": cases}, sort_keys=False, width=100, allow_unicode=True)
    )
    args.out.write_text(text)
    if args.skips:
        args.skips.write_text(json.dumps(skipped, indent=1) + "\n")
    tally = defaultdict(int)
    for case in cases:
        tally[(case["split"], case["should_trigger"])] += 1
    print(
        json.dumps(
            {
                "cases": len(cases),
                "skipped": len(skipped),
                "by_split": {
                    f"{s} {'pos' if t else 'neg'}": n for (s, t), n in sorted(tally.items())
                },
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
