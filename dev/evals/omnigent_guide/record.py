"""Records passed between the drivers, the grader, and the report."""

from __future__ import annotations

from dataclasses import dataclass, field
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
    :param arm: ``"baseline"`` or ``"treatment"``.
    :param run_index: 0-based repeat number for the case in this arm.
    :param answer: The final assistant text.
    :param tool_calls: ``{"name", "arguments"}`` dicts in call order;
        ``arguments`` is the raw JSON string the harness recorded.
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
    """One graded assertion, with the evidence for the verdict."""

    text: str
    passed: bool
    evidence: str


@dataclass
class GradedRun:
    """
    A run plus its grades.

    :param record: The raw run.
    :param skills_loaded: Skill names the agent loaded, sorted.
    :param triggered: For a should-trigger case, whether an expected skill
        loaded; otherwise, whether any framework skill loaded.
    :param assertions: Deterministic results, then judged results.
    """

    record: RunRecord
    skills_loaded: list[str]
    triggered: bool
    assertions: list[AssertionResult]

    @property
    def pass_rate(self) -> float | None:
        """Fraction of assertions passed, or ``None`` when there are none."""
        if not self.assertions:
            return None
        return sum(result.passed for result in self.assertions) / len(self.assertions)
