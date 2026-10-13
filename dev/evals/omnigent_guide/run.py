"""Before/after eval: do agents answer questions about Omnigent correctly?

Runs every case in ``cases.yaml`` in fresh sessions against two installed
builds (``baseline`` and ``treatment``), grades each answer, and writes
``runs.jsonl``, ``benchmark.json``, and ``summary.md`` to ``--out``. See
``README.md`` in this directory for building the two venvs.

Run from the repository root with every ``OMNIGENT_*`` session variable unset::

    uv run --no-sync python -m dev.evals.omnigent_guide.run \\
        --baseline-python /tmp/eval/base/bin/python \\
        --treatment-python /tmp/eval/head/bin/python \\
        --profile dev --out /tmp/eval/results
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from dev.evals.omnigent_guide.cases import Case, load_cases
from dev.evals.omnigent_guide.drivers import (
    ClaudeNativeEvalDriver,
    ClaudeSdkEvalDriver,
    EvalDriver,
    isolated_claude_env,
)
from dev.evals.omnigent_guide.grade import Judge, OpenAIJudge, grade_run
from dev.evals.omnigent_guide.record import AssertionResult, GradedRun, RunRecord
from dev.evals.omnigent_guide.report import per_case, render_markdown, summarize
from tests._helpers.compat import COMPAT_RUNNER_PYTHON_ENV, COMPAT_SERVER_PYTHON_ENV
from tests.harness_bench.profile import resolve_profile

_DRIVERS: dict[str, type[ClaudeNativeEvalDriver] | type[ClaudeSdkEvalDriver]] = {
    "claude-native": ClaudeNativeEvalDriver,
    "claude-sdk": ClaudeSdkEvalDriver,
}

# Printed by the arm's interpreter: version, commit, guide present, install path.
_PROBE = """
import json, pathlib, omnigent, omnigent.onboarding
from omnigent.version import VERSION
try:
    from omnigent import _build_info
    commit = _build_info.COMMIT_SHA
except ImportError:
    commit = ""
guide = pathlib.Path(omnigent.onboarding.__file__).parent / "agent/skills/omnigent-guide/SKILL.md"
print(json.dumps({"version": VERSION, "commit": commit, "guide": guide.is_file(),
                  "package": str(pathlib.Path(omnigent.__file__).parent)}))
"""


@dataclasses.dataclass(frozen=True)
class Arm:
    """One installed build under test."""

    name: str
    python: Path
    version: str
    commit: str
    has_guide: bool
    package_dir: str


def main(argv: list[str] | None = None) -> int:
    """Run the eval; return a process exit code."""
    args = _parse_args(argv)
    cases = [case for case in load_cases() if _selected(case, args)]
    by_id = {case.id: case for case in load_cases()}
    args.out.mkdir(parents=True, exist_ok=True)
    runs_path = args.out / "runs.jsonl"
    arms = [
        _probe_arm("baseline", args.baseline_python),
        _probe_arm("treatment", args.treatment_python),
    ]
    if not args.skip_preflight:
        _preflight(arms)

    if not args.regrade:
        done = {
            _key(run.record) for run in _read_runs(runs_path) if run.record.status != "infra_error"
        }
        for harness in args.harness:
            for arm in arms:
                _run_arm(harness, arm, cases, done, runs_path, args)

    runs = _read_runs(runs_path)
    if args.judge_model:
        _judge(runs, by_id, _make_judge(args.judge_model, args.profile))
    _write_reports(runs, by_id, arms, args)
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--baseline-python",
        type=Path,
        required=True,
        help="Interpreter of the build before the change.",
    )
    parser.add_argument(
        "--treatment-python",
        type=Path,
        required=True,
        help="Interpreter of the build with the change.",
    )
    parser.add_argument(
        "--harness", action="append", choices=sorted(_DRIVERS), help="Repeatable. Default: all."
    )
    parser.add_argument("--runs", type=int, default=3, help="Fresh sessions per case per arm.")
    parser.add_argument("--split", choices=("all", "dev", "holdout"), default="all")
    parser.add_argument(
        "--case", action="append", default=[], help="Run only these case ids. Repeatable."
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model for both arms. Default: the harness bench's test model for each harness.",
    )
    parser.add_argument(
        "--judge-model",
        default=None,
        help="Judge for free-text assertions. Default: the bench's codex (GPT) test model.",
    )
    parser.add_argument("--no-judge", action="store_true", help="Grade deterministic checks only.")
    parser.add_argument(
        "--profile", default=None, help="Databricks profile for gateway credentials."
    )
    parser.add_argument(
        "--timeout", type=float, default=420.0, help="Seconds allowed for one turn."
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="Results directory; reruns resume into it."
    )
    parser.add_argument(
        "--regrade", action="store_true", help="Skip sessions; re-grade and re-report runs.jsonl."
    )
    parser.add_argument(
        "--skip-preflight", action="store_true", help="Allow builds that fail the install checks."
    )
    args = parser.parse_args(argv)
    args.harness = args.harness or sorted(_DRIVERS)
    # Default to the bench's pinned test models rather than naming models here.
    args.models = {
        harness: args.model or resolve_profile(harness).model for harness in args.harness
    }
    args.judge_model = (
        None if args.no_judge else args.judge_model or resolve_profile("codex").model
    )
    return args


def _selected(case: Case, args: argparse.Namespace) -> bool:
    if args.case and case.id not in args.case:
        return False
    return args.split == "all" or case.split == args.split


def _probe_arm(name: str, python: Path) -> Arm:
    out = subprocess.run(
        [str(python), "-I", "-c", _PROBE], capture_output=True, text=True, check=True, cwd="/"
    ).stdout
    info = json.loads(out)
    return Arm(
        name=name,
        python=python,
        version=info["version"],
        commit=info["commit"] or "unknown",
        has_guide=info["guide"],
        package_dir=info["package"],
    )


def _preflight(arms: list[Arm]) -> None:
    """Fail fast when the two builds would not measure the change."""
    repo_root = str(Path(__file__).resolve().parents[3])
    problems = []
    for arm in arms:
        if arm.package_dir.startswith(repo_root):
            problems.append(
                f"{arm.name} imports omnigent from the checkout ({arm.package_dir}); "
                "install a built wheel instead"
            )
    baseline, treatment = arms
    if baseline.has_guide:
        problems.append(
            "baseline already ships omnigent-guide; build it from the commit before the change"
        )
    if not treatment.has_guide:
        problems.append(
            "treatment does not ship omnigent-guide; build it from the branch with the change"
        )
    if problems:
        sys.exit("preflight failed:\n- " + "\n- ".join(problems))


def _run_arm(
    harness: str,
    arm: Arm,
    cases: list[Case],
    done: set[tuple[str, str, str, int]],
    runs_path: Path,
    args: argparse.Namespace,
) -> None:
    todo = [
        (case, index)
        for case in cases
        for index in range(args.runs)
        if (harness, arm.name, case.id, index) not in done
    ]
    if not todo:
        return
    os.environ[COMPAT_SERVER_PYTHON_ENV] = str(arm.python)
    os.environ[COMPAT_RUNNER_PYTHON_ENV] = str(arm.python)
    scratch = args.out / "scratch" / f"{harness}-{arm.name}"
    extra_env = {
        # Agents that run `omnigent` must reach this arm's build, not another install.
        "PATH": f"{arm.python.parent}{os.pathsep}{os.environ.get('PATH', '')}",
        # Keep host identity, logs, and process registries away from a live install.
        "OMNIGENT_DATA_DIR": str(scratch / "data"),
        **isolated_claude_env(scratch),
    }
    print(f"== {harness} / {arm.name} ({arm.version}): {len(todo)} run(s)", flush=True)
    driver: EvalDriver = _DRIVERS[harness](
        model=args.models[harness],
        databricks_profile=args.profile,
        extra_env=extra_env,
        timeout=args.timeout,
    )
    with driver:
        for case, index in todo:
            record = RunRecord(case_id=case.id, harness=harness, arm=arm.name, run_index=index)
            driver.run(case.prompt, record)
            graded = grade_run(case, record, installed_version=arm.version)
            with runs_path.open("a", encoding="utf-8") as runs_file:
                runs_file.write(json.dumps(dataclasses.asdict(graded)) + "\n")
            passed = sum(result.passed for result in graded.assertions)
            print(
                f"   {case.id} #{index}: {record.status}, loaded={graded.skills_loaded or '-'}, "
                f"checks {passed}/{len(graded.assertions)}, {record.duration_s:.0f}s",
                flush=True,
            )


def _judge(runs: list[GradedRun], cases: dict[str, Case], judge: Judge) -> None:
    """Add judged assertions to every run that lacks them."""
    for run in runs:
        case = cases.get(run.record.case_id)
        if case is None or not case.judge or run.record.status == "infra_error":
            continue
        if any(result.text.startswith("judge: ") for result in run.assertions):
            continue
        run.assertions.extend(judge.grade(case.prompt, run.record.answer, case.judge))


def _make_judge(model: str, profile: str | None) -> Judge:
    from openai import OpenAI

    from tests.harness_bench.runtime_env import resolve_bench_env

    env = resolve_bench_env(profile).base_env
    client = OpenAI(base_url=env["OPENAI_BASE_URL"], api_key=env["OPENAI_API_KEY"])
    return OpenAIJudge(client, model)


def _write_reports(
    runs: list[GradedRun], cases: dict[str, Case], arms: list[Arm], args: argparse.Namespace
) -> None:
    runs = [run for run in runs if run.record.case_id in cases]
    holdout = [run for run in runs if cases[run.record.case_id].split == "holdout"]
    summaries = {"All cases": summarize(runs, cases), "Holdout only": summarize(holdout, cases)}
    rows = per_case(runs, cases)
    meta = {
        "model": ", ".join(f"{harness} `{model}`" for harness, model in args.models.items()),
        "runs": args.runs,
        "judge": args.judge_model or "off",
        "baseline_commit": arms[0].commit,
        "treatment_commit": arms[1].commit,
        "baseline_version": arms[0].version,
        "treatment_version": arms[1].version,
    }
    benchmark = {"meta": meta, "run_summary": summaries, "per_case": rows}
    (args.out / "benchmark.json").write_text(
        json.dumps(benchmark, indent=2) + "\n", encoding="utf-8"
    )
    summary = render_markdown(summaries, rows, meta)
    (args.out / "summary.md").write_text(summary, encoding="utf-8")
    # Rewrite runs.jsonl so judged assertions persist for --regrade.
    with (args.out / "runs.jsonl").open("w", encoding="utf-8") as runs_file:
        for run in runs:
            runs_file.write(json.dumps(dataclasses.asdict(run)) + "\n")
    print(summary)


def _read_runs(path: Path) -> list[GradedRun]:
    """Read graded runs; a retried run replaces its earlier record."""
    if not path.exists():
        return []
    latest: dict[tuple[str, str, str, int], GradedRun] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            run = _graded_from_dict(json.loads(line))
            latest[_key(run.record)] = run
    return list(latest.values())


def _graded_from_dict(raw: dict[str, Any]) -> GradedRun:
    return GradedRun(
        record=RunRecord(**raw["record"]),
        skills_loaded=list(raw["skills_loaded"]),
        triggered=bool(raw["triggered"]),
        assertions=[AssertionResult(**result) for result in raw["assertions"]],
    )


def _key(record: RunRecord) -> tuple[str, str, str, int]:
    return (record.harness, record.arm, record.case_id, record.run_index)


if __name__ == "__main__":
    raise SystemExit(main())
