"""Grade runs: which skills loaded, which criteria passed, and what was invented."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from dev.evals.omnigent_guide.cases import Case, Check, Criterion
from dev.evals.omnigent_guide.record import CriterionResult, GradedRun, RunRecord
from dev.evals.omnigent_guide.referee import FABRICATION_KINDS, Referee, Review
from omnigent.runtime.harnesses._executor_adapter import (
    _extract_skill_name,
    _strip_mcp_tool_prefix,
)
from omnigent.spec.framework_skills import FRAMEWORK_SKILL_NAMES

_JUDGE_ATTEMPTS = 5

# Harnesses without a skill tool (codex, pi) read the SKILL.md file directly.
_SKILL_FILE_RE = re.compile(r"skills/([A-Za-z0-9][\w.-]*)/SKILL\.md")
_FLAGS = re.IGNORECASE | re.MULTILINE


class Judge(Protocol):
    """Grades criteria about an answer."""

    name: str

    def grade(
        self, prompt: str, answer: str, criteria: Sequence[Criterion]
    ) -> dict[str, tuple[bool | None, str]]:
        """Return ``{criterion id: (verdict or None, evidence)}``."""
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


def applicable(case: Case, version: str) -> tuple[Criterion, ...]:
    """The case's criteria that hold for a build reporting *version*."""
    return tuple(c for c in case.criteria if c.applies_to(version))


def agent_dirs(case: Case, record: RunRecord) -> list[str]:
    """Directories a ``valid_agent_dir`` check expects the agent to have written."""
    if record.workspace is None:
        return []
    found: list[str] = []
    for check in case.checks:
        if check.kind == "valid_agent_dir":
            name = check.values[0]
            # A runner may give each session its own subdirectory of the workspace.
            found.extend(
                str(p.parent) for p in sorted(Path(record.workspace).rglob(f"{name}/config.yaml"))
            )
            found.extend(str(p) for p in sorted(Path(record.workspace).rglob(f"{name}.yaml")))
    return found[:1]


def grade_runs(
    cases: Mapping[str, Case],
    records: Sequence[RunRecord],
    referee: Referee,
    judges: Sequence[Judge] = (),
    *,
    installed_version: str = "",
) -> list[GradedRun]:
    """
    Grade runs from one build.

    :param cases: Cases by id.
    :param records: Runs from the build *referee* describes.
    :param referee: Checks claims against that build.
    :param judges: Grade every criterion; their verdicts decide criteria
        without a check and are kept for agreement on the rest.
    :param installed_version: Replaces ``{installed_version}`` in regexes.
    """
    dirs = [agent_dirs(cases[r.case_id], r) for r in records]
    reviews = referee.review([r.answer for r in records], dirs)
    graded = []
    for record, review, written in zip(records, reviews, dirs, strict=True):
        case = cases[record.case_id]
        loaded = skills_loaded(record.tool_calls)
        if case.should_trigger:
            triggered = any(skill in loaded for skill in case.expect_skills)
        else:
            triggered = any(skill in loaded for skill in FRAMEWORK_SKILL_NAMES)
        results = []
        criteria = applicable(case, installed_version)
        for criterion in criteria:
            if criterion.check is None:
                results.append(CriterionResult(criterion.id, criterion.text, "judge"))
                continue
            check = _bind(criterion.check, installed_version)
            passed, evidence = _run_check(check, record, review, written)
            results.append(
                CriterionResult(
                    criterion.id, criterion.text, check.kind, passed, evidence=evidence
                )
            )
        if judges and record.status != "infra_error" and criteria:
            by_id = {r.id: r for r in results}
            for judge in judges:
                for cid, (verdict, _evidence) in judge.grade(
                    case.prompt, record.answer, criteria
                ).items():
                    if cid in by_id:
                        by_id[cid].judges[judge.name] = verdict
        named, invented = cli_counts(review)
        graded.append(
            GradedRun(
                record=record,
                skills_loaded=loaded,
                triggered=triggered,
                criteria=results,
                findings=[
                    {**f.__dict__, "counts": f.kind in FABRICATION_KINDS and not f.negated}
                    for f in review.findings
                ],
                yaml_blocks=review.yaml_blocks,
                yaml_invalid=review.yaml_invalid,
                commands_named=named,
                commands_invented=invented,
            )
        )
    return graded


_CLI_KINDS = frozenset({"command", "option"})


def cli_counts(review: Review) -> tuple[int, int]:
    """
    Distinct ``omnigent`` commands an answer names, and how many do not exist.

    A real command counts once however it is invoked; an invocation with an
    invented subcommand or flag counts as its own command. A name that only a
    newer build has, or that the answer itself negates, is named but not invented.
    """
    invented = {f.text for f in review.fabrications if f.kind in _CLI_KINDS}
    newer = {
        f.text.split(": ", 1)[1]
        for f in review.findings
        if f.kind == "version_mismatch" and f.text.split(": ", 1)[0] in _CLI_KINDS
    }
    negated = {f.text for f in review.findings if f.kind in _CLI_KINDS and f.negated}
    valid = {"omnigent " + " ".join(path) for path, _ in review.commands}
    return len(valid | newer | negated | invented), len(invented)


def _run_check(
    check: Check, record: RunRecord, review: Review, written: Sequence[str]
) -> tuple[bool, str]:
    answer = record.answer
    if check.kind == "any_of":
        hit = _first_match(check.values, answer)
        return hit is not None, f"matched {hit!r}" if hit else "no pattern matched"
    if check.kind == "none_of":
        hit = _first_match(check.values, answer)
        return hit is None, f"matched trap {hit!r}" if hit else "no trap matched"
    if check.kind == "command_ran":
        for call in record.tool_calls:
            text = (
                call.get("arguments")
                if isinstance(call.get("arguments"), str)
                else json.dumps(call.get("arguments") or {})
            )
            hit = _first_match(check.values, text)
            if hit:
                return True, f"{call.get('name')} matched {hit!r}"
        return False, "no tool call matched"
    if check.kind == "tool_called":
        called = {_strip_mcp_tool_prefix(str(c.get("name") or "")) for c in record.tool_calls}
        hits = sorted(called & set(check.values))
        return bool(hits), f"called {hits}" if hits else "called none of them"
    if check.kind == "cli":
        for value in check.values:
            if review.uses_command(value.split(), check.options):
                return True, f"valid `omnigent {value}`"
        seen = sorted({" ".join(p) for p, _ in review.commands})
        return False, f"valid commands seen: {seen}"
    if check.kind == "config":
        hits = sorted(set(check.values) & review.config_paths)
        return bool(
            hits
        ), f"uses {hits}" if hits else f"keys seen: {sorted(review.config_paths)[:12]}"
    if check.kind == "tool":
        hits = sorted(set(check.values) & review.tools_named)
        return bool(hits), f"names {hits}" if hits else "names none of them"
    # valid_agent_dir: the referee loaded what the agent wrote.
    if not written:
        return False, f"no {check.values[0]} agent in the workspace"
    bad = [
        f
        for f in review.findings
        if f.kind in {"config_key", "config_value"} and f.text.startswith(("/", written[0]))
    ]
    problems = [f for f in review.findings if f.kind in {"config_key", "config_value"}]
    return not bad and not any(written[0] in f.text for f in problems), f"loaded {written[0]}"


def _bind(check: Check, installed_version: str) -> Check:
    if check.kind not in ("any_of", "none_of", "command_ran"):
        return check
    # Without a known version, accept any version-shaped string.
    version = re.escape(installed_version) if installed_version else r"\d+\.\d+\.\d+\S*"
    values = tuple(value.replace("{installed_version}", version) for value in check.values)
    return Check(kind=check.kind, values=values, options=check.options)


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

    It sees the question, the answer, and the criteria, but not which build
    produced the answer or what the deterministic checks decided.

    :param client: An ``openai.OpenAI`` client.
    :param model: Judge model. Use a family different from the model under test.
    :param name: Short label for reports, e.g. ``"llama"``.
    """

    _INSTRUCTIONS = (
        "You grade an AI assistant's answer to a question about Omnigent, an "
        "open-source meta-harness for AI agents. Each criterion states something a "
        "correct answer does. Mark a criterion passed only when the answer does it "
        "concretely; a vague, hedged, or contradicted mention fails. Quote the answer "
        "as evidence. Reply with JSON only, one entry per criterion id: "
        '{"results": [{"id": "c1", "passed": true, "evidence": "..."}]}'
    )

    def __init__(self, client: Any, model: str, name: str) -> None:
        self._client = client
        self._model = model
        self.name = name

    def grade(
        self, prompt: str, answer: str, criteria: Sequence[Criterion]
    ) -> dict[str, tuple[bool | None, str]]:
        """Grade each criterion; a criterion the reply leaves out gets ``None``."""
        listed = "\n".join(f"{c.id}: {c.text}" for c in criteria)
        message = (
            f"Question:\n{prompt}\n\nAnswer:\n{answer or '(no answer)'}\n\nCriteria:\n{listed}"
        )
        for attempt in range(_JUDGE_ATTEMPTS):
            if attempt:
                time.sleep(min(2**attempt, 30))  # gateway rate limits clear within seconds
            try:
                reply = self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": self._INSTRUCTIONS},
                        {"role": "user", "content": message},
                    ],
                    temperature=0,
                    max_tokens=4000,
                )
            except Exception:  # noqa: BLE001 — a judge outage must not abort grading
                continue
            results = _judge_results(_message_text(reply.choices[0].message.content))
            verdicts: dict[str, tuple[bool | None, str]] = {}
            for entry in results:
                if isinstance(entry, dict) and isinstance(entry.get("passed"), bool):
                    verdicts[str(entry.get("id"))] = (
                        entry["passed"],
                        str(entry.get("evidence", ""))[:300],
                    )
            if verdicts:
                return {c.id: verdicts.get(c.id, (None, "no verdict")) for c in criteria}
        return {c.id: (None, "judge gave no usable reply") for c in criteria}


def _message_text(content: object) -> str:
    """Text of a chat message; reasoning models can return a list of typed parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in (None, "text", "output_text")
        )
    return ""


def _judge_results(content: str) -> list[Any]:
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < start:
        return []
    parsed = _json_object(content[start : end + 1])
    results = parsed.get("results")
    return results if isinstance(results, list) else []
