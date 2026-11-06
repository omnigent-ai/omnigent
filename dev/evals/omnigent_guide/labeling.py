"""A blind sample of graded answers for a maintainer to label, and agreement with those labels.

The sample hides which build produced each answer and every automated verdict.
``key.json`` maps item ids back to runs; keep it away from the labeler.
"""

from __future__ import annotations

import csv
import json
import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from dev.evals.omnigent_guide.cases import Case
from dev.evals.omnigent_guide.record import INFRA_ERROR, GradedRun
from dev.evals.omnigent_guide.stats import agreement

_INSTRUCTIONS = """# Labeling the Omnigent guide eval

Each item in `sample.md` is a question a user asked an AI agent running inside
Omnigent, the agent's answer, and the criteria a correct answer meets. You do
not see which build produced the answer or how the automated graders scored it.

For each item, fill in `labels.csv`:

- For every criterion row, put `PASS` if the answer meets the statement
  concretely, or `FAIL` if it does not, is vague, or contradicts it.
- In the `fabricated` row, list every command, flag, config key, tool name,
  environment variable, doc path, or URL in the answer that does not exist in
  Omnigent at the answer's version, separated by `;`. Write `none` if there are
  none. Things the answer itself says do not exist are not fabrications.

Use your own knowledge and the code (`omnigent <command> --help`, the spec
parser, the docs). Leave a row blank if you cannot decide.
"""


def write_blind_sample(
    runs: Sequence[GradedRun],
    cases: Mapping[str, Case],
    out: Path,
    *,
    size: int,
    seed: int,
    split: str = "test",
) -> None:
    """Pick *size* answers spread across arms and cases; write the sample, template, and key."""
    rng = random.Random(seed)
    pool = [
        r
        for r in runs
        if r.record.status != INFRA_ERROR
        and cases[r.record.case_id].split == split
        and cases[r.record.case_id].should_trigger
    ]
    by_arm: dict[tuple[str, str], list[GradedRun]] = defaultdict(list)
    for run in pool:
        by_arm[(run.record.harness, run.record.arm)].append(run)
    for group in by_arm.values():
        rng.shuffle(group)
    chosen: list[GradedRun] = []
    seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    groups = sorted(by_arm)
    while len(chosen) < size and any(by_arm[g] for g in groups):
        for group in groups:
            if len(chosen) >= size or not by_arm[group]:
                continue
            # Prefer a case this arm has not contributed yet.
            index = next(
                (i for i, r in enumerate(by_arm[group]) if r.record.case_id not in seen[group]), 0
            )
            run = by_arm[group].pop(index)
            seen[group].add(run.record.case_id)
            chosen.append(run)
    rng.shuffle(chosen)

    out.mkdir(parents=True, exist_ok=True)
    key: dict[str, Any] = {}
    md = ["# Blind sample", "", "Labels go in `labels.csv`. See `README.md`.", ""]
    rows: list[list[str]] = []
    for run in chosen:
        item = f"{rng.getrandbits(32):08x}"  # seeded, so the sample is reproducible
        case = cases[run.record.case_id]
        key[item] = {
            "harness": run.record.harness,
            "arm": run.record.arm,
            "case_id": case.id,
            "run_index": run.record.run_index,
        }
        md += [f"## Item {item}", "", f"**Question:** {case.prompt}", "", "**Answer:**", ""]
        # The sample goes to someone else: hide this machine's home directory.
        answer = (run.record.answer or "(no answer)").replace(str(Path.home()), "~")
        md += [f"> {line}" if line else ">" for line in answer.splitlines()]
        md += ["", "**Criteria:**", ""]
        # The run's own criteria: only those that hold for its build's version.
        for crit in run.criteria:
            md.append(f"- `{crit.id}`: {crit.text}")
            rows.append([item, crit.id, crit.text, ""])
        rows.append(
            [
                item,
                "fabricated",
                "Invented commands, flags, keys, tools, env vars, paths, or URLs",
                "",
            ]
        )
        md.append("")
    (out / "README.md").write_text(_INSTRUCTIONS)
    (out / "sample.md").write_text("\n".join(md) + "\n")
    with (out / "labels.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["item", "criterion", "statement", "label"])
        writer.writerows(rows)
    (out / "key.json").write_text(json.dumps(key, indent=2) + "\n")


def human_agreement(runs: Sequence[GradedRun], blind: Path, labels: Path) -> dict[str, Any]:
    """Compare filled-in labels with code checks, each judge, and the fabrication flags."""
    key = json.loads((blind / "key.json").read_text())
    index = {
        (r.record.harness, r.record.arm, r.record.case_id, r.record.run_index): r for r in runs
    }
    pairs: dict[str, tuple[list[bool], list[bool]]] = defaultdict(lambda: ([], []))
    with labels.open(newline="") as handle:
        for row in csv.DictReader(handle):
            label = (row.get("label") or "").strip()
            if not label:
                continue
            meta = key.get(row["item"])
            run = (
                index.get((meta["harness"], meta["arm"], meta["case_id"], meta["run_index"]))
                if meta
                else None
            )
            if run is None:
                continue
            if row["criterion"] == "fabricated":
                human = label.lower() != "none"
                pairs["referee fabrication flag"][0].append(human)
                pairs["referee fabrication flag"][1].append(run.fabricated)
                continue
            human = label.upper() == "PASS"
            crit = next((c for c in run.criteria if c.id == row["criterion"]), None)
            if crit is None:
                continue
            if crit.code is not None:
                pairs[f"code ({crit.kind})"][0].append(human)
                pairs[f"code ({crit.kind})"][1].append(crit.code)
            for name, verdict in crit.judges.items():
                if verdict is not None:
                    kind = "checked" if crit.code is not None else "judge-only"
                    pairs[f"{name} ({kind})"][0].append(human)
                    pairs[f"{name} ({kind})"][1].append(verdict)
    return {grader: agreement(human, auto) for grader, (human, auto) in sorted(pairs.items())}
