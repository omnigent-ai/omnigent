"""Offline checks for the omnigent-guide eval: cases, grading, and the report.

The eval itself drives live sessions and is run by hand (see
``dev/evals/omnigent_guide/README.md``); these tests pin the parts that need
no model, so a broken grader cannot quietly skew the numbers.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from dev.evals.omnigent_guide.cases import Check, load_cases
from dev.evals.omnigent_guide.grade import OpenAIJudge, grade_run, skills_loaded
from dev.evals.omnigent_guide.record import INFRA_ERROR, AssertionResult, GradedRun, RunRecord
from dev.evals.omnigent_guide.report import per_case, render_markdown, summarize


def _record(case_id: str, **fields: object) -> RunRecord:
    return RunRecord(
        case_id=case_id, harness="claude-native", arm="treatment", run_index=0, **fields
    )


def _call(name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {"name": name, "arguments": json.dumps(arguments)}


def test_cases_file_is_valid_and_split() -> None:
    cases = load_cases()

    positives = [case for case in cases if case.should_trigger]
    assert positives and len(positives) < len(cases), "need should-trigger and near-miss cases"
    assert {case.split for case in cases} == {"dev", "holdout"}
    # The shared traps ride along on every should-trigger case.
    assert all(any(check.kind == "none_of" for check in case.checks) for case in positives)


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


def test_deterministic_checks_and_trigger(tmp_path: Path) -> None:
    case = next(case for case in load_cases() if case.id == "upgrade")
    good = _record(
        "upgrade",
        answer="Run `omnigent upgrade`.",
        tool_calls=[_call("Skill", {"skill": "omnigent-guide"})],
    )
    bad = _record("upgrade", answer="Run `ap deploy` then `omnigent self-update`.")

    good_run, bad_run = grade_run(case, good), grade_run(case, bad)

    assert good_run.triggered and good_run.pass_rate == 1.0
    assert not bad_run.triggered
    assert [result.passed for result in bad_run.assertions] == [False, False, False]


def test_version_and_agent_dir_checks(tmp_path: Path) -> None:
    version_case = next(case for case in load_cases() if case.id == "version")
    agent_case = next(case for case in load_cases() if case.id == "author-agent")
    agent_dir = tmp_path / "session-1" / "pr-summarizer"
    agent_dir.mkdir(parents=True)
    (agent_dir / "config.yaml").write_text(
        "spec_version: 1\nname: pr-summarizer\nprompt: Summarize pull requests.\n"
    )

    right = grade_run(
        version_case,
        _record("version", answer="You run 0.19.0.dev0."),
        installed_version="0.19.0.dev0",
    )
    wrong = grade_run(
        version_case, _record("version", answer="You run 0.18.0."), installed_version="0.19.0.dev0"
    )
    authored = grade_run(agent_case, _record("author-agent", workspace=str(tmp_path)))
    missing = grade_run(agent_case, _record("author-agent", workspace=str(tmp_path / "empty")))

    assert right.pass_rate == 1.0 and wrong.pass_rate == 0.5
    assert authored.assertions[0].passed and "pr-summarizer" in authored.assertions[0].evidence
    assert not missing.assertions[0].passed


def test_judge_reply_parsing() -> None:
    def client(content: str) -> SimpleNamespace:
        reply = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )
        completions = SimpleNamespace(create=lambda **_kwargs: reply)
        return SimpleNamespace(chat=SimpleNamespace(completions=completions))

    verdict = (
        '```json\n{"results": [{"assertion": "a", "passed": true, "evidence": "quote"}]}\n```'
    )
    graded = OpenAIJudge(client(verdict), "judge").grade("q", "answer", ("a", "b"))
    garbled = OpenAIJudge(client("no json here"), "judge").grade("q", "answer", ("a",))

    assert [(result.passed, result.evidence) for result in graded] == [
        (True, "quote"),
        (False, "judge returned no verdict"),
    ]
    assert not garbled[0].passed


def test_report_deltas_exclude_infra_errors() -> None:
    cases = {case.id: case for case in load_cases()}

    def run(
        arm: str, case_id: str, triggered: bool, passed: bool, status: str = "completed"
    ) -> GradedRun:
        record = RunRecord(
            case_id=case_id,
            harness="claude-sdk",
            arm=arm,
            run_index=0,
            status=status,
            total_tokens=1000,
        )
        result = AssertionResult(text="t", passed=passed, evidence="e")
        return GradedRun(record=record, skills_loaded=[], triggered=triggered, assertions=[result])

    runs = [
        run("baseline", "upgrade", False, False),
        run("baseline", "neg-yaml-json", False, False),
        run("treatment", "upgrade", True, True),
        run("treatment", "neg-yaml-json", True, False),
        run("treatment", "upgrade", False, False, status=INFRA_ERROR),
    ]

    stats = summarize(runs, cases)["claude-sdk"]
    markdown = render_markdown(
        {"All cases": summarize(runs, cases)},
        per_case(runs, cases),
        {
            "model": "m",
            "runs": 1,
            "judge": "off",
            "baseline_commit": "a",
            "treatment_commit": "b",
        },
    )

    assert stats["treatment"]["infra_errors"] == 1
    assert stats["delta"]["trigger_rate"] == 1.0
    assert stats["delta"]["pass_rate"] == 1.0
    assert stats["treatment"]["false_trigger_rate"] == 1.0
    assert "| claude-sdk | treatment | 1/1 (100%) | 1/1 (100%) | 100% |" in markdown


def test_check_label_names_kind_and_values() -> None:
    assert Check(kind="any_of", values=("a", "b")).label == "any_of: a | b"
