from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.resources import files
from string import Template
from typing import Protocol

from issue_prioritization.areas import AreaCatalog
from issue_prioritization.domain import (
    EvidenceKind,
    Impact,
    InformationStatus,
    IssueType,
    MissingInformation,
    Priority,
)

_PRIORITY_LABELS = {priority.value for priority in Priority}
MAX_BUG_REVIEW_CHARACTERS = 100_000
_TYPE_LABELS = {
    "bug": IssueType.BUG,
    "feature": IssueType.ENHANCEMENT,
    "enhancement": IssueType.ENHANCEMENT,
    "docs": IssueType.DOCUMENTATION,
    "documentation": IssueType.DOCUMENTATION,
}
_PROMPT_TEMPLATE = Template(
    files("issue_prioritization").joinpath("classification_prompt.txt").read_text()
)


@dataclass(frozen=True)
class IssueContent:
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    author: str

    @property
    def content_hash(self) -> str:
        payload = json.dumps(
            {
                "title": self.title,
                "body": self.body,
                "labels": sorted(_classification_labels(self.labels)),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class Classification:
    issue_number: int
    issue_type: IssueType
    impact: Impact
    area_keys: tuple[str, ...]
    component_labels: tuple[str, ...]
    reasoning: str
    content_hash: str
    reported_type: IssueType | None = None
    evidence_kind: EvidenceKind = EvidenceKind.NONE
    information_status: InformationStatus = InformationStatus.NOT_APPLICABLE
    missing_information: tuple[MissingInformation, ...] = ()
    help_wanted: bool = False
    duplicate_decision: str = "none"
    duplicate_of: int | None = None
    similar_issues: tuple[int, ...] = ()
    duplicate_confidence: float = 0.0
    duplicate_reasoning: str = ""
    source_only_quote: str | None = None


class Classifier(Protocol):
    def classify(self, issue: IssueContent) -> Classification: ...


class PromptClassifier:
    def __init__(
        self,
        query: Callable[[str], str],
        areas: AreaCatalog,
        duplicate_candidates: tuple[dict[str, object], ...] = (),
        *,
        review_bugs: bool = False,
    ) -> None:
        self.query = query
        self.areas = areas
        self.duplicate_candidates = duplicate_candidates
        self.review_bugs = review_bugs

    def classify(self, issue: IssueContent) -> Classification:
        response = self.query(
            build_prompt(issue, self.areas, self.duplicate_candidates, review_bugs=self.review_bugs)
        )
        value = _parse_json_object(response)
        area_keys = tuple(
            key for key in _string_list(value.get("area_keys")) if key in self.areas.by_key
        )
        component_labels = tuple(
            dict.fromkeys(self.areas.by_key[key].issue_label for key in area_keys)
        )
        issue_type = _issue_type(value.get("type"))
        evidence_kind, information_status, missing_information = _information_assessment(
            issue_type, value
        )
        source_only_quote = None
        quote = value.get("source_only_quote")
        if (
            self.review_bugs
            and issue_type == IssueType.BUG
            and evidence_kind == EvidenceKind.CODE_ANALYSIS
            and information_status == InformationStatus.NEEDS_INFO
            and value.get("has_user_facing_repro") is False
            and isinstance(quote, str)
            and quote.strip()
            and " ".join(quote.split()) in " ".join(issue.body.split())
        ):
            source_only_quote = " ".join(quote.split())
        return Classification(
            issue_number=issue.number,
            issue_type=issue_type,
            impact=Impact.parse(value.get("impact", value.get("severity"))),
            area_keys=area_keys,
            component_labels=component_labels,
            reasoning=str(value.get("reasoning", "")),
            content_hash=issue.content_hash,
            reported_type=reported_issue_type(issue.labels),
            evidence_kind=evidence_kind,
            information_status=information_status,
            missing_information=missing_information,
            help_wanted=value.get("help_wanted") is True,
            duplicate_decision=str(value.get("duplicate_decision") or "none"),
            duplicate_of=_optional_int(value.get("duplicate_of")),
            similar_issues=tuple(_int_list(value.get("similar_issues"))),
            duplicate_confidence=_confidence(value.get("duplicate_confidence")),
            duplicate_reasoning=str(value.get("duplicate_reasoning") or ""),
            source_only_quote=source_only_quote,
        )


def build_prompt(
    issue: IssueContent,
    areas: AreaCatalog,
    duplicate_candidates: tuple[dict[str, object], ...] = (),
    *,
    review_bugs: bool = False,
) -> str:
    if review_bugs and len(issue.title) + len(issue.body) > MAX_BUG_REVIEW_CHARACTERS:
        raise ValueError(
            "Report exceeds the complete-evidence review limit; manual review required"
        )
    area_lines = [
        f"- {area.key}: label={area.issue_label}. {area.definition}"
        for area in sorted(areas.by_key.values(), key=lambda item: item.key)
    ]
    return _PROMPT_TEMPLATE.substitute(
        allowed_areas="\n".join(area_lines),
        issue_number=issue.number,
        title=issue.title,
        labels=", ".join(issue.labels) if issue.labels else "none",
        author=issue.author,
        body=issue.body if review_bugs else issue.body[:12000],
        code_analysis_guidance=(
            "Apply the event bug policy at the end of this prompt."
            if review_bugs
            else "Code analysis naming a reachable path and its concrete incorrect impact "
            "can also be sufficient. A defensive code-path report can be sufficient when "
            "it explains reachability and impact; never reject it merely because nobody "
            "ran the path end to end."
        ),
        bug_closure_guidance=(
            """Bug closure policy overrides the completeness rubric above; applies only to Bugs.
Read the complete report and author replies. Judge evidence, not writing style.
You have not inspected the code or executed any tests.

Return reasoning first: explain whether a failure occurred and evaluate the
supplied reproduction, including its preconditions. Then return these two fields
and all other classification fields, each only once:
- has_user_facing_repro=true: a complete, plausible UI/CLI/public API sequence,
  even if unexecuted or inferred from source. Internal setup requiring mocks,
  private cache/registry mutations, or resolver barriers is not a user sequence,
  even when it also includes public actions.
- has_user_facing_repro=false: affirmative evidence confines the scenario to
  internal/synthetic manipulation with NO plausible OR uncertain user workflow.
  Missing observations/steps or a source-only disclaimer cannot establish false.
  Assess supplied actions; predicted effects on users do not establish a workflow.
- has_user_facing_repro=null: steps are missing, incomplete, or uncertain,
  including uncertainty about whether supported user actions permit the scenario.
  A fully specified internal-only recipe is false, not null merely because unexecuted.
- source_only_quote: null unless ALL closure conditions below hold; otherwise
  copy a short continuous prose excerpt establishing the source-only basis.
  Match punctuation exactly; only whitespace may change. Never paraphrase.

Keep open when a failure was observed: information_status=sufficient when there
is enough context to investigate. Plain descriptions, intermittent observations,
diagnostics, and executed tests of user workflows count. Logs and first-person
wording are not required. Choose the observed evidence_kind, not code_analysis.

Keep open for clarification when observation is unclear, necessary details are
missing, or public user steps are unexecuted/uncertain: information_status=needs_info,
source_only_quote=null. Ask the author to clarify the steps or share the result.
Even an explicit source-only disclaimer cannot override plausible OR uncertain
user steps. "I don't know which user actions allow this" means null, never false.

Close ONLY when ALL are established: no observed failure, has_user_facing_repro=false,
and an explicit source-only/unexecuted internal basis supported by source_only_quote.
Use information_status=needs_info, missing_information=[observed_behavior],
evidence_kind=code_analysis, impact=low. Otherwise keep open for clarification.

Predicted Expected/Actual sections and proposed tests prove neither observation
nor source-only origin. Preserve tense: "unit tests can simulate this" and "no
live runner needs to be removed" describe future validation, not past experience.
"Export can be empty; a unit test could check it" needs clarification.
"Source audit only; open a session, run /clear, send a web message" stays open.
"Source audit only; nobody ran it; mock a subprocess and check a heartbeat" closes.
Return one complete JSON object without surrounding prose or trailing commas."""
            if review_bugs
            else ""
        ),
        bug_type_guidance=(
            "An alleged failure remains a Bug even when its trigger or impact is speculative "
            "or unsupported; assess its evidence using the event policy. Reclassify as Feature "
            "only when the author actually requests a new capability or refactoring, rather "
            "than merely alleging a possible failure. Missing evidence is not a feature request."
            if review_bugs
            else "For example, a code-quality concern that does not claim incorrect behavior "
            "is usually a Feature, not an incomplete Bug."
        ),
        duplicate_candidates=(
            json.dumps(duplicate_candidates, ensure_ascii=False, indent=2)
            if duplicate_candidates
            else "None. This is a reclassification; return duplicate_decision=none."
        ),
    )


def _parse_json_object(value: str) -> Mapping[str, object]:
    # Require the complete response, optionally fenced; prose or extra objects are ambiguous.
    cleaned = value.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines[0].strip() not in ("```", "```json") or lines[-1].strip() != "```":
            raise ValueError("classifier returned an invalid JSON code fence")
        cleaned = "\n".join(lines[1:-1])
    while True:
        try:
            parsed = json.loads(cleaned)
            break
        except json.JSONDecodeError as error:
            prefix = cleaned[: error.pos].rstrip()
            if cleaned[error.pos : error.pos + 1] not in ("}", "]") or not prefix.endswith(","):
                raise ValueError("classifier returned invalid JSON") from error
            # Repair only the trailing comma identified by the decoder, outside strings.
            cleaned = prefix[:-1] + cleaned[error.pos :]
    if not isinstance(parsed, Mapping):
        raise ValueError("classifier did not return a JSON object")
    return parsed


def _issue_type(value: object) -> IssueType:
    return IssueType.parse(value)


def reported_issue_type(labels: tuple[str, ...]) -> IssueType | None:
    types = {_TYPE_LABELS[label.casefold()] for label in labels if label.casefold() in _TYPE_LABELS}
    return next(iter(types)) if len(types) == 1 else None


def _information_assessment(
    issue_type: IssueType,
    value: Mapping[str, object],
) -> tuple[EvidenceKind, InformationStatus, tuple[MissingInformation, ...]]:
    if issue_type != IssueType.BUG:
        return EvidenceKind.NONE, InformationStatus.NOT_APPLICABLE, ()

    evidence_kind = EvidenceKind.parse(value.get("evidence_kind"))
    information_status = InformationStatus.parse(value.get("information_status"))
    missing_information = tuple(
        dict.fromkeys(
            MissingInformation.parse(item)
            for item in _string_list(value.get("missing_information"))
        )
    )
    if information_status == InformationStatus.NOT_APPLICABLE:
        raise ValueError("bug information status cannot be not_applicable")
    if information_status == InformationStatus.SUFFICIENT and evidence_kind == EvidenceKind.NONE:
        raise ValueError("a sufficient bug report must identify usable evidence")
    if information_status == InformationStatus.NEEDS_INFO and not missing_information:
        raise ValueError("a needs-info bug report must identify missing information")
    return evidence_kind, information_status, missing_information


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _int_list(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, int) and not isinstance(item, bool)]


def _confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return min(1.0, max(0.0, float(value)))


def _classification_labels(labels: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        label
        for label in labels
        if label not in _PRIORITY_LABELS
        and label.casefold() not in _TYPE_LABELS
        and label.casefold() != "needs-info"
        and not label.startswith("severity:")
        and not label.startswith("comp:")
    )
