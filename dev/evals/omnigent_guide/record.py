"""Records passed between the drivers, the grader, and the report."""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import fmean
from typing import Any

#: A run whose plumbing failed (provisioning, server error) says nothing about
#: the agent, so reports exclude it from rates and count it separately.
INFRA_ERROR = "infra_error"


@dataclass
class RunRecord:
    """
    What one fresh session did with one prompt.

    :param case_id: The case that produced the prompt, e.g. ``"deny-shell"``.
    :param harness: e.g. ``"claude-native"``.
    :param arm: The build under test, e.g. ``"a3"``.
    :param run_index: 0-based repeat number for the case in this arm.
    :param answer: The final assistant text.
    :param tool_calls: ``{"name", "arguments", "output"}`` dicts in call order;
        ``arguments`` is the raw JSON string the harness recorded and
        ``output`` the start of the tool's result.
    :param status: ``completed``, ``failed``, ``timed_out``, or
        :data:`INFRA_ERROR`.
    :param error: Failure detail, when there was one.
    :param duration_s: Wall time from posting the prompt to the end of the turn.
    :param total_tokens: Session token total, when the harness reports it.
    :param cost_usd: Session cost, when the harness reports it.
    :param elicitations: Approval prompts the driver accepted.
    :param workspace: The session's working directory, for file checks.
    """

    case_id: str
    harness: str
    arm: str
    run_index: int
    answer: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    status: str = "completed"
    error: str | None = None
    duration_s: float = 0.0
    total_tokens: int | None = None
    cost_usd: float | None = None
    elicitations: int = 0
    workspace: str | None = None


@dataclass
class AssertionResult:
    """One graded statement, with the evidence for the verdict."""

    text: str
    passed: bool
    evidence: str


@dataclass
class CriterionResult:
    """
    The verdicts on one criterion for one run.

    :param id: The criterion's id within its case.
    :param text: The criterion statement.
    :param kind: The check kind, or ``"judge"`` when only judges decide it.
    :param code: The deterministic verdict, or ``None`` without a check.
    :param judges: Verdict per judge name; ``None`` when that judge gave none.
    :param evidence: What the deterministic check saw.
    """

    id: str
    text: str
    kind: str
    code: bool | None = None
    judges: dict[str, bool | None] = field(default_factory=dict)
    evidence: str = ""

    @property
    def score(self) -> float | None:
        """Code decides when it can; otherwise the mean of the judges that answered."""
        if self.code is not None:
            return float(self.code)
        votes = [float(v) for v in self.judges.values() if v is not None]
        return fmean(votes) if votes else None


@dataclass
class GradedRun:
    """
    A run plus its grades.

    :param record: The raw run.
    :param skills_loaded: Skill names the agent loaded, sorted.
    :param triggered: For a should-trigger case, whether an expected skill
        loaded; otherwise, whether any framework skill loaded.
    :param criteria: One result per case criterion.
    :param findings: Referee findings (claims that do not exist in the build).
    :param yaml_blocks: Agent-config YAML blocks or files the answer contained.
    :param yaml_invalid: How many of those failed to load cleanly.
    """

    record: RunRecord
    skills_loaded: list[str]
    triggered: bool
    criteria: list[CriterionResult] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    yaml_blocks: int = 0
    yaml_invalid: int = 0

    @property
    def score(self) -> float | None:
        """Mean criterion score, or ``None`` when no criterion has a verdict."""
        scores = [s for s in (c.score for c in self.criteria) if s is not None]
        return fmean(scores) if scores else None

    @property
    def fabricated(self) -> bool:
        """Whether the answer names a command, flag, key, or tool that does not exist."""
        return any(f.get("counts") for f in self.findings)
