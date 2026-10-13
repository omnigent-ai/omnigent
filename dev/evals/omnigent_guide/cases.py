"""Load and validate the eval cases in ``cases.yaml``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import yaml

CASES_PATH = Path(__file__).with_name("cases.yaml")

Split = Literal["dev", "holdout"]
CheckKind = Literal["any_of", "none_of", "command_ran", "tool_called", "valid_agent_dir"]

_SPLITS = frozenset({"dev", "holdout"})
_CHECK_KINDS = frozenset({"any_of", "none_of", "command_ran", "tool_called", "valid_agent_dir"})
_REGEX_KINDS = frozenset({"any_of", "none_of", "command_ran"})
_DEFAULT_EXPECTED_SKILLS = ("omnigent-guide",)


@dataclass(frozen=True)
class Check:
    """
    One deterministic assertion about a run.

    Regex values may contain ``{installed_version}``, which grading replaces
    with the version the arm's ``omnigent --version`` reports.

    :param kind: ``any_of`` / ``none_of`` match regexes against the answer;
        ``command_ran`` matches regexes against tool-call arguments;
        ``tool_called`` names tools; ``valid_agent_dir`` names a directory
        the agent must leave in its workspace as a loadable agent.
    :param values: The regexes, tool names, or the single directory.
    """

    kind: CheckKind
    values: tuple[str, ...]

    @property
    def label(self) -> str:
        """Short text for reports, e.g. ``"any_of: policies?:"``."""
        return f"{self.kind}: {' | '.join(self.values)}"


@dataclass(frozen=True)
class Case:
    """
    One prompt with its expected skill use and its assertions.

    :param id: Stable identifier, e.g. ``"deny-shell"``.
    :param split: ``dev`` cases may guide skill edits; ``holdout`` cases
        measure whether those edits generalize.
    :param prompt: The user message sent to a fresh session.
    :param should_trigger: Whether a framework skill should be loaded.
    :param expect_skills: Skills that count as a correct load (any one).
    :param checks: Deterministic assertions.
    :param judge: Free-text assertions graded by the LLM judge.
    """

    id: str
    split: Split
    prompt: str
    should_trigger: bool
    expect_skills: tuple[str, ...]
    checks: tuple[Check, ...]
    judge: tuple[str, ...]

    @property
    def assertion_count(self) -> int:
        """Number of graded assertions (deterministic plus judged)."""
        return len(self.checks) + len(self.judge)


def load_cases(path: Path = CASES_PATH) -> list[Case]:
    """
    Parse and validate every case in *path*.

    ``global_traps`` become one extra ``none_of`` check on every
    should-trigger case.

    :param path: The cases file, e.g. :data:`CASES_PATH`.
    :returns: The cases in file order.
    :raises ValueError: On an unknown key, bad regex, or duplicate id.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    traps = tuple(str(pattern) for pattern in raw.get("global_traps", []))
    for pattern in traps:
        re.compile(pattern)
    cases = [_parse_case(entry, traps) for entry in raw["cases"]]
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"duplicate case id {case.id!r}")
        seen.add(case.id)
    return cases


def _parse_case(entry: dict[str, Any], global_traps: tuple[str, ...]) -> Case:
    unknown = set(entry) - {
        "id",
        "split",
        "prompt",
        "should_trigger",
        "expect_skills",
        "checks",
        "judge",
    }
    case_id = str(entry.get("id", "?"))
    if unknown:
        raise ValueError(f"case {case_id!r}: unknown keys {sorted(unknown)}")
    split = entry["split"]
    if split not in _SPLITS:
        raise ValueError(f"case {case_id!r}: split must be one of {sorted(_SPLITS)}")
    should_trigger = bool(entry["should_trigger"])
    default_skills = _DEFAULT_EXPECTED_SKILLS if should_trigger else ()
    expect_skills = tuple(entry.get("expect_skills", default_skills))
    checks = tuple(_parse_check(case_id, raw) for raw in entry.get("checks", []))
    judge = tuple(str(text) for text in entry.get("judge", []))
    if should_trigger and not (checks or judge):
        raise ValueError(f"case {case_id!r}: a should-trigger case needs assertions")
    if should_trigger and global_traps:
        checks = (*checks, Check(kind="none_of", values=global_traps))
    return Case(
        id=case_id,
        split=cast(Split, split),
        prompt=str(entry["prompt"]).strip(),
        should_trigger=should_trigger,
        expect_skills=expect_skills,
        checks=checks,
        judge=judge,
    )


def _parse_check(case_id: str, raw: dict[str, Any]) -> Check:
    if len(raw) != 1:
        raise ValueError(f"case {case_id!r}: each check has exactly one kind, got {sorted(raw)}")
    ((kind, value),) = raw.items()
    if kind not in _CHECK_KINDS:
        raise ValueError(f"case {case_id!r}: unknown check kind {kind!r}")
    values = (value,) if isinstance(value, str) else tuple(str(v) for v in value)
    if not values:
        raise ValueError(f"case {case_id!r}: {kind} needs at least one value")
    if kind in _REGEX_KINDS:
        for pattern in values:
            re.compile(pattern)
    if kind == "valid_agent_dir" and len(values) != 1:
        raise ValueError(f"case {case_id!r}: valid_agent_dir takes one directory")
    return Check(kind=cast(CheckKind, kind), values=values)
