"""Load and validate eval cases.

Two files use the same loader:

- ``questions.yaml``: real questions mined from GitHub and session history,
  each with criteria derived from the CLI, the spec parser, and the tool
  registry. Its ``test`` split is frozen by ``questions.lock``.
- ``cases.yaml``: the earlier hand-written cases, kept for regression.

A criterion is one statement a correct answer satisfies. When it has a
``check``, code decides it; otherwise the judges do. The judges also grade
criteria that have checks, so the two can be compared.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import yaml

HERE = Path(__file__).parent
CASES_PATH = HERE / "cases.yaml"
QUESTIONS_PATH = HERE / "questions.yaml"
QUESTIONS_LOCK = HERE / "questions.lock"

Split = Literal["dev", "holdout", "test"]
CheckKind = Literal[
    "any_of", "none_of", "command_ran", "tool_called", "valid_agent_dir", "cli", "config", "tool"
]

_SPLITS = frozenset({"dev", "holdout", "test"})
_CHECK_KINDS = frozenset(
    {"any_of", "none_of", "command_ran", "tool_called", "valid_agent_dir", "cli", "config", "tool"}
)
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
        ``tool_called`` names tools the agent must call; ``valid_agent_dir``
        names a directory the agent must leave as a loadable agent; ``cli``
        names command paths (``"config set"``), one of which the answer must
        invoke validly; ``config`` names dotted config keys, one of which the
        answer must use; ``tool`` names tools the answer must mention.
    :param values: The regexes, names, paths, or the single directory.
    :param options: For ``cli``: flags that must accompany the command.
    """

    kind: CheckKind
    values: tuple[str, ...]
    options: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """Short text for reports, e.g. ``"cli: upgrade | update"``."""
        extra = f" with {' '.join(self.options)}" if self.options else ""
        return f"{self.kind}: {' | '.join(self.values)}{extra}"


@dataclass(frozen=True)
class Criterion:
    """
    One statement a correct answer satisfies.

    :param id: Unique within the case, e.g. ``"c1"``.
    :param text: The statement, as judges and human labelers read it.
    :param check: How code decides it; ``None`` leaves it to the judges.
    :param source: Where the expected answer comes from, e.g.
        ``"omnigent/cli.py: upgrade"``.
    """

    id: str
    text: str
    check: Check | None = None
    source: str = ""
    versions: str = ""

    def applies_to(self, version: str) -> bool:
        """
        Whether the criterion holds for a build reporting *version*.

        ``versions`` is empty (every build) or a comparison such as
        ``">=0.18.0"`` or ``"<0.18.0"``; dev suffixes compare as their release.
        """
        if not self.versions or not version:
            return True
        match = re.fullmatch(r"\s*(>=|<=|>|<|==)\s*([\d.]+)\s*", self.versions)
        if match is None:
            raise ValueError(f"bad versions predicate {self.versions!r}")
        op, wanted = match.groups()
        have, want = _version_tuple(version), _version_tuple(wanted)
        return {
            ">=": have >= want,
            "<=": have <= want,
            ">": have > want,
            "<": have < want,
            "==": have == want,
        }[op]


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version.split(".dev")[0])[:3])


@dataclass(frozen=True)
class Case:
    """
    One prompt with its expected skill use and its criteria.

    :param id: Stable identifier, e.g. ``"deny-shell"``.
    :param split: ``dev`` cases may guide changes; ``test`` (and the older
        ``holdout``) cases only measure them.
    :param prompt: The user message sent to a fresh session.
    :param should_trigger: Whether a framework skill should be loaded.
    :param expect_skills: Skills that count as a correct load (any one).
    :param criteria: What a correct answer satisfies.
    :param origin: Where the question came from, e.g. ``"github issue#1234"``.
    :param cluster: Topic cluster the question was sampled from.
    """

    id: str
    split: Split
    prompt: str
    should_trigger: bool
    expect_skills: tuple[str, ...]
    criteria: tuple[Criterion, ...]
    origin: str = ""
    cluster: str = ""

    @property
    def checks(self) -> tuple[Check, ...]:
        return tuple(c.check for c in self.criteria if c.check is not None)

    @property
    def judge(self) -> tuple[str, ...]:
        return tuple(c.text for c in self.criteria if c.check is None)

    @property
    def assertion_count(self) -> int:
        return len(self.criteria)


def load_cases(path: Path = CASES_PATH) -> list[Case]:
    """
    Parse and validate every case in *path*.

    In ``cases.yaml``, ``global_traps`` become one extra ``none_of`` check on
    every should-trigger case.

    :param path: The cases file.
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


def split_digest(cases: list[Case], split: str = "test") -> str:
    """SHA-256 over the canonical form of one split's cases, for the freeze lock."""
    chosen = [case for case in cases if case.split == split]
    canonical = [
        {
            "id": c.id,
            "prompt": c.prompt,
            "should_trigger": c.should_trigger,
            "expect_skills": list(c.expect_skills),
            "criteria": [
                {
                    "id": k.id,
                    "text": k.text,
                    "versions": k.versions,
                    "check": None
                    if k.check is None
                    else [k.check.kind, list(k.check.values), list(k.check.options)],
                }
                for k in c.criteria
            ],
        }
        for c in chosen
    ]
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


def read_lock(path: Path = QUESTIONS_LOCK) -> dict[str, Any]:
    """The frozen split's digest and metadata, or ``{}`` before the freeze."""
    return json.loads(path.read_text()) if path.exists() else {}


_CASE_KEYS = frozenset(
    {
        "id",
        "split",
        "prompt",
        "should_trigger",
        "expect_skills",
        "checks",
        "judge",
        "criteria",
        "origin",
        "cluster",
    }
)


def _parse_case(entry: dict[str, Any], global_traps: tuple[str, ...]) -> Case:
    case_id = str(entry.get("id", "?"))
    unknown = set(entry) - _CASE_KEYS
    if unknown:
        raise ValueError(f"case {case_id!r}: unknown keys {sorted(unknown)}")
    split = entry["split"]
    if split not in _SPLITS:
        raise ValueError(f"case {case_id!r}: split must be one of {sorted(_SPLITS)}")
    should_trigger = bool(entry["should_trigger"])
    default_skills = _DEFAULT_EXPECTED_SKILLS if should_trigger else ()
    expect_skills = tuple(entry.get("expect_skills", default_skills))
    if "criteria" in entry:
        criteria = tuple(
            _parse_criterion(case_id, raw, i) for i, raw in enumerate(entry["criteria"])
        )
    else:
        checks = [_parse_check(case_id, raw) for raw in entry.get("checks", [])]
        criteria = tuple(
            [Criterion(f"c{i + 1}", check.label, check) for i, check in enumerate(checks)]
            + [Criterion(f"j{i + 1}", str(text)) for i, text in enumerate(entry.get("judge", []))]
        )
    ids = [c.id for c in criteria]
    if len(ids) != len(set(ids)):
        raise ValueError(f"case {case_id!r}: duplicate criterion ids")
    if should_trigger and not criteria:
        raise ValueError(f"case {case_id!r}: a should-trigger case needs criteria")
    if should_trigger and global_traps:
        trap = Check(kind="none_of", values=global_traps)
        criteria = (*criteria, Criterion("traps", "Recommends no removed command.", trap))
    return Case(
        id=case_id,
        split=cast(Split, split),
        prompt=str(entry["prompt"]).strip(),
        should_trigger=should_trigger,
        expect_skills=expect_skills,
        criteria=criteria,
        origin=str(entry.get("origin", "")),
        cluster=str(entry.get("cluster", "")),
    )


def _parse_criterion(case_id: str, raw: dict[str, Any], index: int) -> Criterion:
    unknown = set(raw) - {"id", "text", "check", "source", "versions"}
    if unknown:
        raise ValueError(f"case {case_id!r}: criterion has unknown keys {sorted(unknown)}")
    check = _parse_check(case_id, raw["check"]) if raw.get("check") else None
    criterion = Criterion(
        id=str(raw.get("id", f"c{index + 1}")),
        text=str(raw["text"]).strip(),
        check=check,
        source=str(raw.get("source", "")),
        versions=str(raw.get("versions", "")),
    )
    criterion.applies_to("0.0.0")  # reject a malformed predicate at load time
    return criterion


def _parse_check(case_id: str, raw: dict[str, Any]) -> Check:
    options = tuple(str(o) for o in raw.get("options", ()))
    kinds = [key for key in raw if key != "options"]
    if len(kinds) != 1:
        raise ValueError(f"case {case_id!r}: each check has exactly one kind, got {sorted(raw)}")
    kind = kinds[0]
    if kind not in _CHECK_KINDS:
        raise ValueError(f"case {case_id!r}: unknown check kind {kind!r}")
    value = raw[kind]
    values = (value,) if isinstance(value, str) else tuple(str(v) for v in value)
    if not values:
        raise ValueError(f"case {case_id!r}: {kind} needs at least one value")
    if kind in _REGEX_KINDS:
        for pattern in values:
            re.compile(pattern)
    if kind == "valid_agent_dir" and len(values) != 1:
        raise ValueError(f"case {case_id!r}: valid_agent_dir takes one directory")
    if options and kind != "cli":
        raise ValueError(f"case {case_id!r}: options apply only to cli checks")
    return Check(kind=cast(CheckKind, kind), values=values, options=options)
