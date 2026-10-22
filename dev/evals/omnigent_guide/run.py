"""Eval: do agents inside an installed Omnigent answer questions about it correctly?

Stages, each re-runnable without repeating the earlier ones::

    run.py freeze   --cases questions.yaml            # lock the test split
    run.py sessions --cases questions.yaml --split dev --arm a0 --arm a3 ...
    run.py grade    --cases questions.yaml --out DIR  # referee + judges
    run.py report   --cases questions.yaml --out DIR --baseline a0
    run.py blind    --cases questions.yaml --out DIR  # sample for human labels
    run.py agreement --out DIR --labels labels.csv    # graders vs human labels

``--arms`` names a JSON file mapping arm name to ``{"python", "source",
"network"}``: the build's interpreter, a checkout of its source commit, and
whether the agent's shell may reach the network. ``sessions`` refuses to run
the test split unless ``questions.lock`` matches the cases, and runs each
(arm, case, repeat) once; later calls only retry runs that hit
infrastructure errors.

Run from the repository root with every ``OMNIGENT_*`` session variable unset.
See ``README.md`` for building the arms.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from dev.evals.omnigent_guide.cases import (
    QUESTIONS_LOCK,
    Case,
    load_cases,
    read_lock,
    split_digest,
)
from dev.evals.omnigent_guide.drivers import (
    ClaudeNativeEvalDriver,
    EvalDriver,
    SdkEvalDriver,
    isolated_claude_env,
)
from dev.evals.omnigent_guide.grade import Judge, OpenAIJudge, applicable, grade_runs
from dev.evals.omnigent_guide.record import (
    INFRA_ERROR,
    CriterionResult,
    GradedRun,
    RunRecord,
)
from dev.evals.omnigent_guide.referee import Referee, Surface
from tests._helpers.compat import COMPAT_RUNNER_PYTHON_ENV, COMPAT_SERVER_PYTHON_ENV
from tests.harness_bench.driver import ProvisioningError, TurnResult, infra_failure_reason
from tests.harness_bench.profile import resolve_profile

_RUNS_PER_DRIVER = 15
_INFRA_RETRIES = 4
_PROVISION_ATTEMPTS = 4
# The gateway limits input tokens per minute per workspace, and an agent turn
# resends its whole context on every tool call, so parallel lanes can exceed it.
_RATE_LIMIT_MARKERS = ("429", "REQUEST_LIMIT_EXCEEDED", "ReadTimeout")
_RATE_LIMIT_PAUSE_S = 45.0


def _rate_limited(error: str | None) -> bool:
    return any(marker in str(error) for marker in _RATE_LIMIT_MARKERS)


def is_infra_error(error: str | None) -> bool:
    """Whether a failed run says nothing about the agent: auth, network, or rate limits."""
    return _rate_limited(error) or bool(infra_failure_reason(TurnResult(failed=True, error=error)))


_SHIMS = Path(__file__).with_name("shims")


def _copy_shims() -> Path:
    """The guard shims in a fresh directory outside the checkout."""
    target = Path(tempfile.mkdtemp(prefix="omnigent-eval-shims-"))
    guard = target / "guard.sh"
    shutil.copy2(_SHIMS / "guard.sh", guard)
    for link in _SHIMS.iterdir():
        if link.is_symlink():
            (target / link.name).symlink_to(guard.name)
    return target


def _sandboxed_os_env(arm: Arm, scratch: Path) -> dict[str, Any]:
    """
    Seatbelt for the agent's shell: it can run the arm's CLI and write only to
    the workspace and a scratch data dir, and cannot signal other processes
    (``omnigent stop`` would otherwise kill whatever serves port 6767). The
    runner keeps its own connection to the model gateway either way.
    """
    venv = Path(os.path.abspath(arm.python)).parent.parent
    home = next(
        (
            line.split("=", 1)[1].strip()
            for line in (venv / "pyvenv.cfg").read_text().splitlines()
            if line.startswith("home")
        ),
        "",
    )
    return {
        "type": "caller_process",
        "cwd": ".",
        "sandbox": {
            "type": "darwin_seatbelt",
            "allow_network": arm.network,
            "read_paths": [str(venv), str(Path(home).parent), str(scratch / "config")],
            "write_paths": [".", str(scratch / "data")],
            "env_passthrough": [
                "OMNIGENT_DATA_DIR",
                "OMNIGENT_CONFIG_HOME",
                "OMNIGENT_NO_UPDATE_CHECK",
            ],
        },
    }


_DRIVERS: dict[str, Callable[..., EvalDriver]] = {
    "claude-native": ClaudeNativeEvalDriver,
    # The Databricks gateway offers the Responses API only for OpenAI GPT models;
    # chat completions works for every chat model it serves.
    "openai-agents": lambda **kw: SdkEvalDriver(
        harness="openai-agents", executor_config={"use_responses": False}, **kw
    ),
}


@dataclasses.dataclass(frozen=True)
class Arm:
    """One installed build under test."""

    name: str
    python: Path
    source: Path
    network: bool = True

    @property
    def version(self) -> str:
        out = subprocess.run(
            [
                os.path.abspath(self.python),
                "-I",
                "-c",
                "from omnigent.version import VERSION; print(VERSION)",
            ],
            capture_output=True,
            text=True,
            check=True,
            cwd="/",
        ).stdout
        return out.strip()

    @property
    def commit(self) -> str:
        return subprocess.run(
            ["git", "-C", str(self.source), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()


def load_arms(path: Path) -> dict[str, Arm]:
    raw = json.loads(path.read_text())
    base = path.parent
    return {
        name: Arm(
            name=name,
            python=(base / spec["python"])
            if not Path(spec["python"]).is_absolute()
            else Path(spec["python"]),
            source=(base / spec["source"])
            if not Path(spec["source"]).is_absolute()
            else Path(spec["source"]),
            network=bool(spec.get("network", True)),
        )
        for name, spec in raw.items()
    }


# ── freeze ──────────────────────────────────────────────────────────────


def validate_answer_keys(
    cases: list[Case], referees: Mapping[str, Referee]
) -> dict[str, list[str]]:
    """Per arm, every answer-key check that names something absent from that build."""
    problems: dict[str, list[str]] = {}
    for name, referee in referees.items():
        found = []
        for case in cases:
            for crit in case.criteria:
                if crit.check is not None and crit.applies_to(referee.surface.version):
                    for problem in referee.validate_key(
                        crit.check.kind, crit.check.values, crit.check.options
                    ):
                        found.append(f"{case.id}/{crit.id}: {problem}")
        problems[name] = found
    return problems


def cmd_freeze(args: argparse.Namespace) -> int:
    cases = load_cases(args.cases)
    if read_lock(args.lock) and not args.force:
        sys.exit(f"{args.lock} exists; the test split is already frozen")
    arms = load_arms(args.arms)
    referees = {
        name: Referee(surface_for(arms[name], args.out), arms[name].python)
        for name in args.validate_on
    }
    problems = validate_answer_keys(cases, referees)
    for name, found in problems.items():
        for problem in found:
            print(f"{name}: {problem}")
    if problems.get(args.validate_on[0]):
        sys.exit(
            f"answer keys name things missing from {args.validate_on[0]}; fix them before freezing"
        )
    lock = {
        "cases_file": args.cases.name,
        "split": "test",
        "cases": sum(c.split == "test" for c in cases),
        "sha256": split_digest(cases, "test"),
        "frozen_at": datetime.datetime.now(datetime.UTC).date().isoformat(),
    }
    args.lock.write_text(json.dumps(lock, indent=2) + "\n")
    print(json.dumps(lock, indent=2))
    return 0


# ── sessions ────────────────────────────────────────────────────────────


def _runs_path(out: Path, harness: str, arm: str) -> Path:
    return out / "runs" / f"{harness}__{arm}.jsonl"


def _read_records(path: Path) -> list[RunRecord]:
    if not path.exists():
        return []
    return [
        RunRecord(**json.loads(line)) for line in path.read_text().splitlines() if line.strip()
    ]


def cmd_sessions(args: argparse.Namespace) -> int:
    cases = [c for c in load_cases(args.cases) if _selected(c, args)]
    if args.split == "test":
        lock = read_lock(args.lock)
        digest = split_digest(load_cases(args.cases), "test")
        if not lock or lock.get("sha256") != digest:
            sys.exit(
                "the test split does not match questions.lock; "
                "freeze it first, and do not edit it after"
            )
    arms = load_arms(args.arms)
    for name in args.arm:
        if name not in arms:
            sys.exit(f"unknown arm {name!r}; known: {sorted(arms)}")
    for harness in args.harness:
        for name in args.arm:
            _run_arm(harness, arms[name], cases, args)
    return 0


def _selected(case: Case, args: argparse.Namespace) -> bool:
    if args.case and case.id not in args.case:
        return False
    return args.split == "all" or case.split == args.split


def _run_arm(harness: str, arm: Arm, cases: list[Case], args: argparse.Namespace) -> None:
    path = _runs_path(args.out, harness, arm.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(_INFRA_RETRIES + 1):
        records = _read_records(path)
        done = {(r.case_id, r.run_index) for r in records if r.status != INFRA_ERROR}
        todo = [(c, i) for c in cases for i in range(args.runs) if (c.id, i) not in done]
        if not todo:
            return
        if attempt:
            print(f"   retrying {len(todo)} run(s) after infrastructure errors", flush=True)
        _drive(harness, arm, todo, path, args)
        # Keep only the latest record per (case, run) so retried runs replace failures.
        latest: dict[tuple[str, int], RunRecord] = {}
        for record in _read_records(path):
            latest[(record.case_id, record.run_index)] = record
        path.write_text("".join(json.dumps(dataclasses.asdict(r)) + "\n" for r in latest.values()))


def _drive(
    harness: str, arm: Arm, todo: list[tuple[Case, int]], path: Path, args: argparse.Namespace
) -> None:
    os.environ[COMPAT_SERVER_PYTHON_ENV] = os.path.abspath(arm.python)
    os.environ[COMPAT_RUNNER_PYTHON_ENV] = os.path.abspath(arm.python)
    scratch = (args.out / "scratch" / f"{harness}__{arm.name}").resolve()
    (scratch / "data").mkdir(parents=True, exist_ok=True)
    bin_dir = Path(os.path.abspath(arm.python)).parent
    extra_env = {
        # Agents that run `omnigent` must reach this arm's build, not another install.
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        # Keep host identity, logs, and process registries away from a live install.
        "OMNIGENT_DATA_DIR": str(scratch / "data"),
        "OMNIGENT_NO_UPDATE_CHECK": "1",
    }
    if harness == "claude-native":
        if not arm.network:
            sys.exit(
                "claude-native cannot block the network: seatbelt would cut Claude's own API calls"
            )
        # No sandbox here (Claude Code needs the real HOME for its login), so
        # guard shims refuse commands that would change the machine. They are
        # copied out of the checkout so `which omnigent` does not lead into it.
        extra_env["PATH"] = f"{_copy_shims()}{os.pathsep}{extra_env['PATH']}"
    else:
        extra_env.update(isolated_claude_env(scratch))
        config_home = scratch / "config"
        config_home.mkdir(parents=True, exist_ok=True)
        profile_line = f"  profile: {args.profile}\n" if args.profile else ""
        (config_home / "config.yaml").write_text(
            f"auth:\n  type: databricks\n{profile_line}runner:\n  idle_timeout_s: 60\n"
        )
        extra_env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    model = args.model or (
        "sonnet" if harness == "claude-native" else resolve_profile(harness).model
    )
    network = "on" if arm.network else "off"
    print(
        f"== {harness} / {arm.name} ({arm.version}, network={network}): {len(todo)} run(s)",
        flush=True,
    )
    kwargs: dict[str, Any] = {
        "model": model,
        "databricks_profile": args.profile,
        "extra_env": extra_env,
        "timeout": args.timeout,
    }
    if harness == "openai-agents":
        kwargs["os_env"] = _sandboxed_os_env(arm, scratch)
    for start in range(0, len(todo), _RUNS_PER_DRIVER):
        batch = todo[start : start + _RUNS_PER_DRIVER]
        for attempt in range(_PROVISION_ATTEMPTS):
            try:
                _run_batch(harness, arm, batch, path, kwargs, scratch)
                break
            except ProvisioningError as exc:
                # A stack that fails to start ran no session, so the batch can rerun.
                print(f"   provisioning failed ({exc}); retrying", flush=True)
                time.sleep(30 * (attempt + 1))


def _run_batch(
    harness: str,
    arm: Arm,
    batch: list[tuple[Case, int]],
    path: Path,
    kwargs: dict[str, Any],
    scratch: Path,
) -> None:
    # Fresh provisioning per batch mints fresh gateway credentials.
    driver_cm = _DRIVERS[harness](**kwargs)
    if hasattr(driver_cm, "keep_logs_in"):
        driver_cm.keep_logs_in = scratch / "logs"
    with driver_cm as driver:
        for case, index in batch:
            record = RunRecord(case_id=case.id, harness=harness, arm=arm.name, run_index=index)
            driver.run(case.prompt, record)
            if record.status == "failed" and is_infra_error(record.error):
                record.status = INFRA_ERROR
                if _rate_limited(record.error):
                    time.sleep(_RATE_LIMIT_PAUSE_S)
            with path.open("a", encoding="utf-8") as runs_file:
                runs_file.write(json.dumps(dataclasses.asdict(record)) + "\n")
            print(f"   {case.id} #{index}: {record.status}, {record.duration_s:.0f}s", flush=True)


# ── grade ───────────────────────────────────────────────────────────────


def _graded_path(out: Path, harness: str, arm: str) -> Path:
    return out / "graded" / f"{harness}__{arm}.jsonl"


class _LazyGatewayClient:
    """An OpenAI client that resolves gateway credentials on first use.

    A regrade that reuses every stored judge verdict then needs no credentials.
    """

    def __init__(self, profile: str | None) -> None:
        self._profile = profile
        self._client: Any = None
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        with self._lock:
            if self._client is None:
                from openai import OpenAI

                from tests.harness_bench.runtime_env import resolve_bench_env

                env = resolve_bench_env(self._profile).base_env
                self._client = OpenAI(
                    base_url=env["OPENAI_BASE_URL"], api_key=env["OPENAI_API_KEY"]
                )
        return getattr(self._client, name)


def _make_judges(specs: list[str], profile: str | None) -> list[Judge]:
    if not specs:
        return []
    client = _LazyGatewayClient(profile)
    judges: list[Judge] = []
    for spec in specs:
        name, sep, model = spec.partition("=")
        if not sep:
            sys.exit(f"--judge takes name=model, got {spec!r}")
        judges.append(OpenAIJudge(client, model, name))
    return judges


def surface_for(arm: Arm, out: Path) -> Surface:
    """The arm's referee facts, probed once and cached next to the results."""
    cache = out / "surfaces" / f"{arm.name}.json"
    if cache.exists():
        return Surface.from_json(json.loads(cache.read_text()))
    surface = Surface.probe(arm.python, source=arm.source)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(surface.to_json()))
    return surface


def cmd_grade(args: argparse.Namespace) -> int:
    cases = {c.id: c for c in load_cases(args.cases)}
    arms = load_arms(args.arms)
    judges = _make_judges(args.judge, args.profile)
    routes = args.site_routes.read_text().split() if args.site_routes else []
    latest = surface_for(arms[args.latest], args.out) if args.latest else None
    for runs_file in sorted((args.out / "runs").glob("*.jsonl")):
        harness, arm_name = runs_file.stem.split("__")
        if args.arm and arm_name not in args.arm:
            continue
        path = _graded_path(args.out, harness, arm_name)
        if (
            not args.regrade
            and path.exists()
            and path.stat().st_mtime >= runs_file.stat().st_mtime
        ):
            print(f"{harness} / {arm_name} is already graded", flush=True)
            continue
        arm = arms[arm_name]
        records = [r for r in _read_records(runs_file) if r.case_id in cases]
        referee = Referee(
            surface_for(arm, args.out), arm.python, site_routes=routes, latest=latest
        )
        previous = _previous_verdicts(path)
        graded = _grade_parallel(cases, records, referee, judges, arm.version, previous)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(dataclasses.asdict(g)) + "\n" for g in graded))
        print(f"graded {len(graded)} run(s) for {harness} / {arm_name}", flush=True)
    return 0


def _answer_key(record: RunRecord) -> tuple[str, int, str]:
    digest = hashlib.sha256(record.answer.encode()).hexdigest()
    return (record.case_id, record.run_index, digest)


def _previous_verdicts(path: Path) -> dict[tuple[str, int, str], dict[str, dict[str, Any]]]:
    """Judge verdicts already given for each unchanged answer, by criterion id."""
    if not path.exists():
        return {}
    found: dict[tuple[str, int, str], dict[str, dict[str, Any]]] = {}
    for line in path.read_text().splitlines():
        if line.strip():
            raw = json.loads(line)
            key = _answer_key(RunRecord(**raw["record"]))
            found[key] = {c["id"]: c["judges"] for c in raw["criteria"]}
    return found


def _grade_parallel(
    cases: Mapping[str, Case],
    records: list[RunRecord],
    referee: Referee,
    judges: list[Judge],
    version: str,
    previous: Mapping[tuple[str, int, str], Mapping[str, Mapping[str, Any]]] = {},
) -> list[GradedRun]:
    # The referee batches its probe calls; judges are slow network calls, so
    # grade without judges first and fan the judge calls out. A judge verdict
    # on an unchanged answer is reused, so a referee fix does not re-ask judges.
    graded = grade_runs(cases, records, referee, (), installed_version=version)
    if not judges:
        return graded

    def judge_one(item: tuple[GradedRun, Judge]) -> None:
        run, judge = item
        criteria = applicable(cases[run.record.case_id], version)
        if run.record.status == INFRA_ERROR or not criteria:
            return
        before = previous.get(_answer_key(run.record), {})
        if all(judge.name in before.get(r.id, {}) for r in run.criteria):
            for result in run.criteria:
                result.judges[judge.name] = before[result.id][judge.name]
            return
        verdicts = judge.grade(cases[run.record.case_id].prompt, run.record.answer, criteria)
        for result in run.criteria:
            result.judges[judge.name] = verdicts.get(result.id, (None, ""))[0]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(judge_one, [(run, judge) for run in graded for judge in judges]))
    return graded


def read_graded(out: Path) -> list[GradedRun]:
    runs = []
    for path in sorted((out / "graded").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            runs.append(
                GradedRun(
                    record=RunRecord(**raw["record"]),
                    skills_loaded=raw["skills_loaded"],
                    triggered=raw["triggered"],
                    criteria=[CriterionResult(**c) for c in raw["criteria"]],
                    findings=raw["findings"],
                    yaml_blocks=raw["yaml_blocks"],
                    yaml_invalid=raw["yaml_invalid"],
                    commands_named=raw.get("commands_named", 0),
                    commands_invented=raw.get("commands_invented", 0),
                )
            )
    return runs


# ── report, blind, agreement ────────────────────────────────────────────


def cmd_report(args: argparse.Namespace) -> int:
    from dev.evals.omnigent_guide.report import write_report

    cases = {c.id: c for c in load_cases(args.cases)}
    arms = load_arms(args.arms)
    meta = {
        name: {"version": arm.version, "commit": arm.commit, "network": arm.network}
        for name, arm in arms.items()
    }
    write_report(
        read_graded(args.out), cases, args.out, baseline=args.baseline, pairs=args.pair, arms=meta
    )
    return 0


def cmd_blind(args: argparse.Namespace) -> int:
    from dev.evals.omnigent_guide.labeling import write_blind_sample

    cases = {c.id: c for c in load_cases(args.cases)}
    write_blind_sample(
        read_graded(args.out), cases, args.out / "blind", size=args.size, seed=args.seed
    )
    return 0


def cmd_agreement(args: argparse.Namespace) -> int:
    from dev.evals.omnigent_guide.labeling import human_agreement

    print(
        json.dumps(
            human_agreement(read_graded(args.out), args.out / "blind", args.labels), indent=2
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--cases", type=Path, default=Path(__file__).with_name("questions.yaml"))
        p.add_argument("--lock", type=Path, default=QUESTIONS_LOCK)
        p.add_argument("--out", type=Path, default=Path(".eval/results"))
        p.add_argument("--arms", type=Path, default=Path(".eval/arms.json"))
        p.add_argument(
            "--profile", default=None, help="Databricks profile for gateway credentials."
        )

    p = sub.add_parser("freeze")
    common(p)
    p.add_argument("--force", action="store_true")
    p.add_argument(
        "--validate-on",
        action="append",
        default=None,
        help="Arms whose code must contain every answer-key entity; the first must pass.",
    )
    p.set_defaults(func=cmd_freeze)

    p = sub.add_parser("sessions")
    common(p)
    p.add_argument("--arm", action="append", required=True)
    p.add_argument("--harness", action="append", choices=sorted(_DRIVERS), required=True)
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--split", choices=("all", "dev", "holdout", "test"), default="dev")
    p.add_argument("--case", action="append", default=[])
    p.add_argument("--model", default=None, help="Default: the harness bench's test model.")
    p.add_argument("--timeout", type=float, default=420.0)
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("grade")
    common(p)
    p.add_argument("--judge", action="append", default=[], help="name=model, repeatable.")
    p.add_argument(
        "--site-routes", type=Path, default=None, help="omnigent.ai page paths, one per line."
    )
    p.add_argument("--arm", action="append", default=[], help="Grade only these arms.")
    p.add_argument("--regrade", action="store_true", help="Grade runs that are already graded.")
    p.add_argument(
        "--latest",
        default=None,
        help="Arm with the newest build; names only it has count as version mismatches.",
    )
    p.set_defaults(func=cmd_grade)

    p = sub.add_parser("report")
    common(p)
    p.add_argument("--baseline", default="a0")
    p.add_argument("--pair", action="append", default=[], help="arm:baseline, repeatable.")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("blind")
    common(p)
    p.add_argument("--size", type=int, default=60)
    p.add_argument("--seed", type=int, default=11)
    p.set_defaults(func=cmd_blind)

    p = sub.add_parser("agreement")
    common(p)
    p.add_argument("--labels", type=Path, required=True)
    p.set_defaults(func=cmd_agreement)

    args = parser.parse_args(argv)
    if args.command == "freeze" and not args.validate_on:
        args.validate_on = ["a3", "t3"]
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
