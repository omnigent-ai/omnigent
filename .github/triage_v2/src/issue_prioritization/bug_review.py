from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from enum import StrEnum


class BugActionability(StrEnum):
    ACTIONABLE = "actionable"
    NEEDS_INFO = "needs_info"
    NON_ACTIONABLE = "non_actionable"


@dataclass(frozen=True)
class ReproductionStep:
    text: str
    source_quote: str


@dataclass(frozen=True)
class BugClarification:
    summary: str
    reproduction_steps: tuple[ReproductionStep, ...] = ()


@dataclass(frozen=True)
class BugReview:
    actionability: BugActionability
    reason: str
    clarification: BugClarification | None = None
    source_only_quote: str | None = None
    has_user_facing_repro: bool | None = None

    @classmethod
    def from_mapping(cls, value: object) -> BugReview:
        if not isinstance(value, Mapping):
            raise ValueError("bug_review must be an object")
        actionability = BugActionability(value.get("actionability"))
        reason = _text(value.get("reason"), "reason", 500)
        clarification = None
        raw = value.get("clarification")
        if raw is not None:
            if actionability != BugActionability.ACTIONABLE or not isinstance(raw, Mapping):
                raise ValueError("only an actionable bug can have a clarification object")
            steps = raw.get("reproduction_steps", [])
            if not isinstance(steps, (list, tuple)):
                raise ValueError("reproduction_steps must be an array")
            parsed_steps = []
            for step in steps:
                if not isinstance(step, Mapping):
                    raise ValueError("each reproduction step must be an object")
                parsed_steps.append(
                    ReproductionStep(
                        _text(step.get("text"), "step text", 300),
                        _text(step.get("source_quote"), "source_quote", 2000),
                    )
                )
            clarification = BugClarification(
                _text(raw.get("summary"), "summary", 600), tuple(parsed_steps)
            )
        quote = value.get("source_only_quote")
        user_repro = value.get("has_user_facing_repro")
        if user_repro is not None and not isinstance(user_repro, bool):
            raise ValueError("has_user_facing_repro must be a boolean or null")
        review = cls(
            actionability,
            reason,
            clarification,
            _text(quote, "source_only_quote", 2000) if quote is not None else None,
            user_repro,
        )
        if review.source_only_quote and actionability != BugActionability.NON_ACTIONABLE:
            raise ValueError("only a non_actionable bug can have a source_only_quote")
        if (
            actionability == BugActionability.ACTIONABLE
            and value.get("readability") != review.readability
        ):
            raise ValueError("bug readability disagrees with actionability or clarification")
        return review

    @property
    def readability(self) -> str:
        if self.actionability != BugActionability.ACTIONABLE:
            return "not_assessed"
        return "needs_summary" if self.clarification is not None else "clear"

    def validate_source(self, body: str) -> BugReview:
        source = " ".join(body.split())
        if self.source_only_quote and self.source_only_quote not in source:
            raise ValueError("source_only_quote is absent from the report")
        if self.clarification and any(
            step.source_quote not in source for step in self.clarification.reproduction_steps
        ):
            # Omit the whole recipe rather than publish an incomplete sequence.
            return replace(self, clarification=replace(self.clarification, reproduction_steps=()))
        return self

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), "readability": self.readability}


def _text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"bug review {field} must be text")
    text = " ".join(value.split())
    if not text or len(text) > limit:
        raise ValueError(f"bug review {field} must contain 1–{limit} characters")
    return text
