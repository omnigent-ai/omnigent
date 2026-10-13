"""Grade one run: which skills it loaded and which assertions it passed."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

from dev.evals.omnigent_guide.cases import Case, Check
from dev.evals.omnigent_guide.record import AssertionResult, GradedRun, RunRecord
from omnigent.runtime.harnesses._executor_adapter import (
    _extract_skill_name,
    _strip_mcp_tool_prefix,
)
from omnigent.spec.framework_skills import FRAMEWORK_SKILL_NAMES

# Harnesses without a skill tool (codex, pi) read the SKILL.md file directly.
_SKILL_FILE_RE = re.compile(r"skills/([A-Za-z0-9][\w.-]*)/SKILL\.md")
_FLAGS = re.IGNORECASE | re.MULTILINE


class Judge(Protocol):
    """Grades free-text assertions about an answer."""

    def grade(
        self, prompt: str, answer: str, assertions: tuple[str, ...]
    ) -> list[AssertionResult]:
        """Return one result per assertion, in order."""
        ...


def skills_loaded(tool_calls: Iterable[Mapping[str, Any]]) -> list[str]:
    """
    Return the skill names that *tool_calls* loaded or read.

    Recognizes Claude Code's ``Skill`` tool (plugin-qualified names lose the
    ``plugin:`` prefix), Omnigent's ``load_skill`` / ``read_skill_file``, and
    any call whose arguments open a ``skills/<name>/SKILL.md`` file.

    :param tool_calls: ``{"name", "arguments"}`` dicts.
    :returns: Sorted, de-duplicated skill names.
    """
    names: set[str] = set()
    for call in tool_calls:
        tool = _strip_mcp_tool_prefix(str(call.get("name") or ""))
        raw = call.get("arguments")
        text = raw if isinstance(raw, str) else json.dumps(raw or {})
        args = _json_object(text)
        skill = _extract_skill_name(tool, args)
        if skill is None and tool == "read_skill_file" and isinstance(args.get("skill_name"), str):
            skill = args["skill_name"]
        if skill:
            names.add(skill.rsplit(":", 1)[-1])
        names.update(_SKILL_FILE_RE.findall(text))
    return sorted(names)


def grade_run(
    case: Case,
    record: RunRecord,
    judge: Judge | None = None,
    *,
    installed_version: str = "",
) -> GradedRun:
    """
    Grade *record* against *case*.

    :param case: The case the run answered.
    :param record: The run.
    :param judge: Grades ``case.judge`` assertions; ``None`` leaves them out.
    :param installed_version: Replaces ``{installed_version}`` in check
        regexes, e.g. ``"0.19.0.dev0"``.
    :returns: The graded run.
    """
    loaded = skills_loaded(record.tool_calls)
    if case.should_trigger:
        triggered = any(skill in loaded for skill in case.expect_skills)
    else:
        triggered = any(skill in loaded for skill in FRAMEWORK_SKILL_NAMES)
    assertions = [_run_check(_bind(check, installed_version), record) for check in case.checks]
    if judge is not None and case.judge:
        assertions.extend(judge.grade(case.prompt, record.answer, case.judge))
    return GradedRun(
        record=record, skills_loaded=loaded, triggered=triggered, assertions=assertions
    )


def _run_check(check: Check, record: RunRecord) -> AssertionResult:
    if check.kind == "any_of":
        hit = _first_match(check.values, record.answer)
        evidence = f"matched {hit!r}" if hit else "no pattern matched the answer"
        return AssertionResult(check.label, hit is not None, evidence)
    if check.kind == "none_of":
        hit = _first_match(check.values, record.answer)
        evidence = f"answer matched trap {hit!r}" if hit else "no trap matched"
        return AssertionResult(check.label, hit is None, evidence)
    if check.kind == "command_ran":
        for call in record.tool_calls:
            raw = call.get("arguments")
            text = raw if isinstance(raw, str) else json.dumps(raw or {})
            hit = _first_match(check.values, text)
            if hit:
                return AssertionResult(check.label, True, f"{call.get('name')} matched {hit!r}")
        return AssertionResult(check.label, False, "no tool call matched")
    if check.kind == "tool_called":
        called = {
            _strip_mcp_tool_prefix(str(call.get("name") or "")) for call in record.tool_calls
        }
        hits = sorted(called & set(check.values))
        evidence = f"called {hits}" if hits else f"called none of them; saw {sorted(called)}"
        return AssertionResult(check.label, bool(hits), evidence)
    return _check_agent_dir(check, record)


def _check_agent_dir(check: Check, record: RunRecord) -> AssertionResult:
    from omnigent.spec.parser import parse

    if record.workspace is None:
        return AssertionResult(check.label, False, "run has no workspace")
    # A runner may give each session its own subdirectory of the workspace.
    name = check.values[0]
    found = sorted(Path(record.workspace).rglob(f"{name}/config.yaml"))
    if not found:
        return AssertionResult(check.label, False, f"no {name}/config.yaml in the workspace")
    agent_dir = found[0].parent
    try:
        spec = parse(agent_dir)
    except Exception as exc:  # noqa: BLE001 — any parse failure is the evidence
        return AssertionResult(check.label, False, f"parse failed: {str(exc)[:200]}")
    return AssertionResult(check.label, True, f"parsed agent {spec.name!r}")


def _bind(check: Check, installed_version: str) -> Check:
    if check.kind not in ("any_of", "none_of", "command_ran"):
        return check
    # Without a known version, accept any version-shaped string.
    version = re.escape(installed_version) if installed_version else r"\d+\.\d+\.\d+\S*"
    values = tuple(value.replace("{installed_version}", version) for value in check.values)
    return Check(kind=check.kind, values=values)


def _first_match(patterns: tuple[str, ...], text: str) -> str | None:
    for pattern in patterns:
        if re.search(pattern, text, _FLAGS):
            return pattern
    return None


def _json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class OpenAIJudge:
    """
    LLM judge over an OpenAI-compatible endpoint (the Databricks gateway).

    It sees the question, the answer, and the assertions, but not which
    build produced the answer.

    :param client: An ``openai.OpenAI`` client.
    :param model: Judge model, e.g. ``"databricks-gpt-5-4-mini"``. Use a
        different model family from the one under test.
    """

    _INSTRUCTIONS = (
        "You grade an AI assistant's answer to a question about Omnigent, an "
        "open-source meta-harness for AI agents. For each assertion, PASS only "
        "when the answer states it concretely and correctly; a vague or hedged "
        "mention is a FAIL. Quote the answer as evidence. Reply with JSON only: "
        '{"results": [{"assertion": "...", "passed": true, "evidence": "..."}]}'
    )

    def __init__(self, client: Any, model: str) -> None:
        self._client = client
        self._model = model

    def grade(
        self, prompt: str, answer: str, assertions: tuple[str, ...]
    ) -> list[AssertionResult]:
        """Grade each assertion; an unparseable reply fails all of them."""
        numbered = "\n".join(f"{i + 1}. {text}" for i, text in enumerate(assertions))
        message = (
            f"Question:\n{prompt}\n\nAnswer:\n{answer or '(no answer)'}\n\nAssertions:\n{numbered}"
        )
        try:
            reply = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": self._INSTRUCTIONS},
                    {"role": "user", "content": message},
                ],
                temperature=0,
            )
            results = _judge_results(reply.choices[0].message.content or "")
        except Exception as exc:  # noqa: BLE001 — a judge outage must not abort the eval
            results = []
            failure = f"judge error: {str(exc)[:200]}"
        else:
            failure = "judge returned no verdict"
        graded = []
        for index, text in enumerate(assertions):
            result = results[index] if index < len(results) else None
            if not isinstance(result, dict):
                graded.append(AssertionResult(f"judge: {text}", False, failure))
                continue
            graded.append(
                AssertionResult(
                    f"judge: {text}",
                    result.get("passed") is True,
                    str(result.get("evidence", ""))[:300],
                )
            )
        return graded


def _judge_results(content: str) -> list[Any]:
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < start:
        return []
    parsed = _json_object(content[start : end + 1])
    results = parsed.get("results")
    return results if isinstance(results, list) else []
