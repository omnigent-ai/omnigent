"""Offline checks for the omnigent-guide eval: cases, the referee, grading, and reports.

The eval itself drives live sessions and is run by hand (see
``dev/evals/omnigent_guide/README.md``). These tests pin the parts that need
no model, so a broken grader cannot quietly skew the numbers. The referee
tests probe this checkout's own build.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dev.evals.omnigent_guide.cases import (
    QUESTIONS_LOCK,
    QUESTIONS_PATH,
    Check,
    Criterion,
    load_cases,
    read_lock,
    split_digest,
)
from dev.evals.omnigent_guide.grade import OpenAIJudge, grade_runs, skills_loaded
from dev.evals.omnigent_guide.labeling import human_agreement, write_blind_sample
from dev.evals.omnigent_guide.record import INFRA_ERROR, CriterionResult, GradedRun, RunRecord
from dev.evals.omnigent_guide.referee import Referee, Surface
from dev.evals.omnigent_guide.report import per_case, render_markdown, write_report
from dev.evals.omnigent_guide.stats import cohen_kappa, paired_bootstrap

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def referee() -> Referee:
    surface = Surface.probe(Path(sys.executable), source=_REPO)
    return Referee(surface, Path(sys.executable))


def _record(case_id: str, **fields: object) -> RunRecord:
    return RunRecord(case_id=case_id, harness="openai-agents", arm="a3", run_index=0, **fields)


def _call(name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {"name": name, "arguments": json.dumps(arguments)}


def test_case_files_load_and_the_lock_matches() -> None:
    synthetic = load_cases()
    assert {case.split for case in synthetic} == {"dev", "holdout"}
    if QUESTIONS_PATH.exists():
        real = load_cases(QUESTIONS_PATH)
        assert {"dev", "test"} <= {case.split for case in real}
        lock = read_lock(QUESTIONS_LOCK)
        assert lock, "questions.yaml exists without questions.lock"
        assert lock["sha256"] == split_digest(real, "test"), "the frozen test split was edited"


def test_skill_loads_are_recognized_across_harness_shapes() -> None:
    calls = [
        _call("Skill", {"skill": "claude-native-ui:omnigent-guide"}),
        _call("mcp__omnigent__load_skill", {"name": "build-omnigent"}),
        _call("read_skill_file", {"skill_name": "detect-framework", "path": "x.md"}),
        _call("shell", {"command": "cat ~/.codex/skills/other-skill/SKILL.md"}),
        _call("Bash", {"command": "ls"}),
    ]

    assert skills_loaded(calls) == [
        "build-omnigent",
        "detect-framework",
        "omnigent-guide",
        "other-skill",
    ]


def test_referee_checks_commands_and_flags_against_the_cli(referee: Referee) -> None:
    good, bad, negated, passthrough, positional = referee.review(
        [
            "Run `omnigent upgrade --check`, then `omni upgrade`.",
            "```bash\nomnigent schedule create --cron '0 9 * * 1-5'\n"
            "omnigent upgrade --latest\n```",
            "There is no `omnigent schedule` command.",
            "`omnigent claude --dangerously-skip-permissions` forwards the flag.",
            'Run `omnigent host https://omni.example.com`, or wrap it: `omnigent "$@"`.',
        ]
    )

    assert good.uses_command(["upgrade"], ["--check"]) and not good.fabrications
    assert {(f.kind, f.text) for f in bad.fabrications} == {
        ("command", "omnigent schedule"),
        ("option", "omnigent upgrade --latest"),
    }
    assert negated.findings and not negated.fabrications
    assert not passthrough.fabrications
    assert not positional.fabrications and positional.uses_command(["host"])


def test_referee_loads_yaml_through_the_spec_loaders(referee: Referee) -> None:
    invented, bad_value, bundle_policies, fragment = referee.review(
        [
            "```yaml\nname: x\nprompt: y\nos_env:\n  sandbox:\n"
            "    type: darwin_seatbelt\n    network: off\n```",
            "```yaml\nos_env:\n  sandbox: true\n```",
            "```yaml\nspec_version: 1\nname: x\nprompt: y\n"
            "policies:\n  p:\n    type: function\n```",
            "```yaml\nsandbox:\n  type: darwin_seatbelt\n  allow_network: false\n```",
        ]
    )

    assert [f.text for f in invented.fabrications] == ["os_env.sandbox.network"]
    assert [f.kind for f in bad_value.findings] == ["config_value"]
    # A bundle config.yaml takes policies under guardrails; the parser ignores this one.
    assert [f.text for f in bundle_policies.fabrications] == ["policies"]
    assert not fragment.findings and "os_env.sandbox.allow_network" in fragment.config_paths


def test_referee_checks_names_paths_and_imports(referee: Referee) -> None:
    (review,) = referee.review(
        [
            "Call `sys_session_get_info` and `sys_list_harnesses`. Set `OMNIGENT_DATA_DIR` and "
            "`OMNIGENT_STATE_PATH`. See docs/POLICIES.md and docs/NOPE.md. Handler "
            "`omnigent.policies.builtins.safety.ask_on_os_tools` or "
            "`omnigent.policies.builtins.safety.deny_everything`. Key `os_env.sandbox.mode`. "
            "There is no `sys_make_coffee` tool."
        ]
    )

    assert review.tools_named == {"sys_session_get_info"}
    assert {(f.kind, f.text) for f in review.fabrications} == {
        ("tool", "sys_list_harnesses"),
        ("env_var", "OMNIGENT_STATE_PATH"),
        ("doc_path", "docs/NOPE.md"),
        ("python_ref", "omnigent.policies.builtins.safety.deny_everything"),
        ("config_key", "os_env.sandbox.mode"),
    }


def test_referee_checks_user_config_keys_by_reader_names(referee: Referee) -> None:
    (review,) = referee.review(
        [
            "Add the agent under `acp.agents`, set `runner.zz_invented_leaf`, and read "
            "`os.environ` as usual. The Docker image reads `OMNIGENT_ARTIFACT_URI`.\n"
            "```yaml\nproviders:\n  my-gateway:\n    kind: gateway\n    zz_invented_field: 1\n```"
        ]
    )

    assert "acp.agents" in review.config_paths
    assert "providers.my-gateway.kind" in review.config_paths
    assert {(f.kind, f.text) for f in review.fabrications} == {
        ("config_key", "runner.zz_invented_leaf"),
        ("config_key", "providers.my-gateway.zz_invented_field"),
    }


class _FakeJudge:
    name = "fake"

    def grade(self, prompt, answer, criteria):  # type: ignore[no-untyped-def]
        return {c.id: ("upgrade" in answer, "") for c in criteria}


def test_grade_runs_combines_code_and_judges(referee: Referee, tmp_path: Path) -> None:
    case = SimpleNamespace(
        id="up",
        split="test",
        prompt="How do I update?",
        should_trigger=True,
        expect_skills=("omnigent-guide",),
        criteria=(
            Criterion("c1", "Recommends omnigent upgrade.", Check("cli", ("upgrade", "update"))),
            Criterion("c2", "Says pip installs need a manual command."),
        ),
        checks=(Check("cli", ("upgrade", "update")),),
    )
    good = _record(
        "up",
        answer="Run `omnigent upgrade`.",
        tool_calls=[_call("load_skill", {"name": "omnigent-guide"})],
    )
    bad = _record("up", answer="Run `omnigent self-update`.")

    graded = grade_runs({"up": case}, [good, bad], referee, [_FakeJudge()])  # type: ignore[dict-item]

    assert graded[0].triggered and graded[0].score == 1.0 and not graded[0].fabricated
    assert graded[1].score == 0.0 and graded[1].fabricated
    assert graded[0].criteria[0].judges == {"fake": True}


def test_judge_reply_parsing() -> None:
    def client(content: object) -> SimpleNamespace:
        reply = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )
        completions = SimpleNamespace(create=lambda **_kwargs: reply)
        return SimpleNamespace(chat=SimpleNamespace(completions=completions))

    criteria = (Criterion("c1", "a"), Criterion("c2", "b"))
    fenced = '```json\n{"results": [{"id": "c1", "passed": true, "evidence": "q"}]}\n```'
    parts = [{"type": "reasoning", "summary": []}, {"type": "text", "text": fenced}]

    assert OpenAIJudge(client(fenced), "m", "j").grade("q", "a", criteria) == {
        "c1": (True, "q"),
        "c2": (None, "no verdict"),
    }
    assert OpenAIJudge(client(parts), "m", "j").grade("q", "a", criteria)["c1"] == (True, "q")
    assert OpenAIJudge(client("no json"), "m", "j").grade("q", "a", criteria)["c1"][0] is None


def _graded(
    arm: str,
    case_id: str,
    score: float,
    *,
    fabricated: bool = False,
    status: str = "completed",
    run: int = 0,
) -> GradedRun:
    record = RunRecord(
        case_id=case_id, harness="openai-agents", arm=arm, run_index=run, status=status, answer="x"
    )
    crit = CriterionResult(
        "c1", "t", "cli", code=bool(score), judges={"j1": bool(score), "j2": True}
    )
    return GradedRun(
        record=record,
        skills_loaded=[],
        triggered=bool(score),
        criteria=[crit],
        findings=[{"kind": "command", "counts": True}] if fabricated else [],
    )


def _cases(*ids: str, negative: str | None = None) -> dict[str, SimpleNamespace]:
    out = {
        i: SimpleNamespace(
            id=i,
            split="test",
            should_trigger=True,
            cluster="k",
            prompt="p?",
            criteria=(Criterion("c1", "t"),),
        )
        for i in ids
    }
    if negative:
        out[negative] = SimpleNamespace(
            id=negative, split="test", should_trigger=False, cluster="", prompt="p?", criteria=()
        )
    return out


def test_report_pairs_arms_by_case_and_drops_infra_errors(tmp_path: Path) -> None:
    cases = _cases("q1", "q2", negative="n1")
    runs = [
        _graded("a0", "q1", 0, fabricated=True),
        _graded("a0", "q2", 0),
        _graded("a0", "n1", 0),
        _graded("a3", "q1", 1),
        _graded("a3", "q2", 1),
        _graded("a3", "n1", 1),
        _graded("a3", "q2", 0, status=INFRA_ERROR, run=1),
    ]

    report = write_report(runs, cases, tmp_path, baseline="a0")  # type: ignore[arg-type]

    data = report["splits"]["test"]["openai-agents"]
    assert per_case(runs[3:], cases)["score"] == {"q1": 1.0, "q2": 1.0}  # type: ignore[arg-type]
    assert data["arms"]["a3"]["infra_errors"] == 1
    assert data["arms"]["a3"]["false_loads"] == 1.0
    (row,) = data["comparisons"]
    assert row["score"]["delta"] == 1.0 and row["fabricated"]["delta"] == -0.5
    assert data["agreement"]["j1 vs j2 (checked)"]["items"] == 6
    assert "| a3 vs a0 | +100 pp" in render_markdown(report)


def test_stats_helpers() -> None:
    result = paired_bootstrap(
        {"a": 0.0, "b": 0.0, "c": 1.0}, {"a": 1.0, "b": 1.0, "c": 1.0}, reps=500
    )
    assert (
        result is not None
        and result.cases == 3
        and 0 <= result.low <= result.delta <= result.high <= 1
    )
    assert cohen_kappa([True, False, True, False], [True, False, True, False]) == 1.0
    assert cohen_kappa([True, True], [True, True]) is None


def test_blind_sample_hides_arms_and_round_trips(tmp_path: Path) -> None:
    cases = _cases("q1", "q2")
    runs = [_graded("a0", "q1", 0, fabricated=True), _graded("a3", "q2", 1)]

    write_blind_sample(runs, cases, tmp_path, size=2, seed=1)  # type: ignore[arg-type]

    sample = (tmp_path / "sample.md").read_text()
    assert "a0" not in sample and "a3" not in sample
    rows = list(csv.reader((tmp_path / "labels.csv").open()))
    key = json.loads((tmp_path / "key.json").read_text())
    for row in rows[1:]:
        arm = key[row[0]]["arm"]
        row[3] = (
            ("none" if arm == "a3" else "omnigent schedule")
            if row[1] == "fabricated"
            else ("PASS" if arm == "a3" else "FAIL")
        )
    with (tmp_path / "filled.csv").open("w", newline="") as handle:
        csv.writer(handle).writerows(rows)

    result = human_agreement(runs, tmp_path, tmp_path / "filled.csv")

    assert result["code (cli)"]["agree"] == 1.0
    assert result["referee fabrication flag"]["agree"] == 1.0
