"""Trusted helpers for issue duplicate detection."""

from __future__ import annotations

import json
import math
import re
from typing import Any

AUTO_CLOSE_CONFIDENCE = 0.92
MAX_CANDIDATES = 10
MAX_SIMILAR_ISSUES = 3
MAX_REASON_LENGTH = 360

_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "has",
    "have",
    "how",
    "i",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "when",
    "with",
}


def build_search_terms(issue: dict[str, Any], limit: int = 6) -> str:
    """Build a compact GitHub search query from an issue."""
    title = str(issue.get("title") or "")
    body = str(issue.get("body") or "")[:1000]
    title_tokens = _search_tokens(title)
    tokens = title_tokens if len(title_tokens) >= 3 else title_tokens + _search_tokens(body)

    unique_tokens: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if token not in seen:
            unique_tokens.append(token)
            seen.add(token)
        if len(unique_tokens) == limit:
            break
    return " ".join(unique_tokens)


def merge_candidates(
    issue_number: int,
    open_candidates: list[dict[str, Any]],
    closed_candidates: list[dict[str, Any]],
    limit: int = MAX_CANDIDATES,
) -> list[dict[str, Any]]:
    """Normalize candidate search results, keeping canonical issues only."""
    merged: list[dict[str, Any]] = []
    seen: set[int] = set()

    for candidate in [*open_candidates, *closed_candidates]:
        number = candidate.get("number")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number >= issue_number
            or number in seen
        ):
            continue

        labels = _label_names(candidate.get("labels"))
        if any(label.casefold() == "duplicate" for label in labels):
            continue

        merged.append(
            {
                "number": number,
                "title": str(candidate.get("title") or "")[:500],
                "body": str(candidate.get("body") or "")[:2000],
                "state": str(candidate.get("state") or "UNKNOWN").upper(),
                "url": str(candidate.get("url") or ""),
                "createdAt": candidate.get("createdAt"),
                "updatedAt": candidate.get("updatedAt"),
                "labels": labels,
            }
        )
        seen.add(number)
        if len(merged) == limit:
            break

    return merged


def format_candidates_for_prompt(candidates: list[dict[str, Any]]) -> str:
    """Serialize candidates without adding prompt-like framing."""
    if not candidates:
        return "None found."
    return json.dumps(candidates, ensure_ascii=False, indent=2)


def validate_duplicate_decision(
    result: dict[str, Any],
    candidates: list[dict[str, Any]],
    auto_close_confidence: float = AUTO_CLOSE_CONFIDENCE,
) -> dict[str, Any]:
    """Validate the model's duplicate decision against prefetched candidates."""
    candidate_numbers = {
        candidate["number"]
        for candidate in candidates
        if isinstance(candidate.get("number"), int)
        and not isinstance(candidate.get("number"), bool)
    }
    requested_decision = result.get("duplicate_decision")
    confidence = _confidence(result.get("duplicate_confidence"))
    duplicate_of = result.get("duplicate_of")
    duplicate_of = (
        duplicate_of
        if isinstance(duplicate_of, int)
        and not isinstance(duplicate_of, bool)
        and duplicate_of in candidate_numbers
        else None
    )
    similar_issues = _validated_issue_numbers(result.get("similar_issues"), candidate_numbers)
    reason = result.get("duplicate_reasoning")

    decision = "none"
    if requested_decision == "duplicate" and duplicate_of is not None:
        if confidence >= auto_close_confidence:
            decision = "duplicate"
            similar_issues = []
        else:
            decision = "similar"
            similar_issues = _deduplicate([duplicate_of, *similar_issues])[:MAX_SIMILAR_ISSUES]
            duplicate_of = None
    elif requested_decision == "similar" and similar_issues:
        decision = "similar"
        duplicate_of = None
    else:
        duplicate_of = None
        similar_issues = []
        if requested_decision != "none":
            reason = None

    return {
        "duplicate_decision": decision,
        "duplicate_of": duplicate_of,
        "similar_issues": similar_issues,
        "duplicate_confidence": confidence,
        "duplicate_reasoning": _sanitize_reason(reason, decision),
    }


def build_duplicate_comment(decision: dict[str, Any]) -> str:
    """Build the public, idempotently identifiable bot comment."""
    marker = "<!-- omnigent-duplicate-check -->"
    reason = decision["duplicate_reasoning"]

    if decision["duplicate_decision"] == "duplicate":
        issue_number = decision["duplicate_of"]
        message = (
            f"Thanks for reporting this. This appears to be a high-confidence "
            f"duplicate of #{issue_number}.\n\n"
            f"Reason: {reason}\n\n"
            f"I’m closing this issue so discussion stays in #{issue_number}. "
            "If this report is materially different, please leave a comment and "
            "a maintainer can reopen it."
        )
    elif decision["duplicate_decision"] == "similar":
        references = ", ".join(f"#{number}" for number in decision["similar_issues"])
        message = (
            f"Thanks for reporting this. These existing issues may be related: "
            f"{references}.\n\n"
            f"Reason: {reason}\n\n"
            "I’m leaving this issue open because the match is not strong enough "
            "to treat it as a duplicate."
        )
    else:
        message = (
            "Thanks for reporting this. I did not find an existing issue that "
            "confidently matches this report.\n\n"
            f"Reason: {reason}\n\n"
            "I’m leaving this issue open for normal triage."
        )

    return f"{marker}\n{message}\n"


def _search_tokens(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_-]+", text.lower())
        if len(token) >= 3 and token not in _STOP_WORDS
    ]


def _label_names(labels: Any) -> list[str]:
    if not isinstance(labels, list):
        return []
    names = []
    for label in labels:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            names.append(name)
    return names


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    confidence = float(value)
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return 0.0
    return confidence


def _validated_issue_numbers(value: Any, allowed: set[int]) -> list[int]:
    if not isinstance(value, list):
        return []
    return _deduplicate(
        [
            number
            for number in value
            if isinstance(number, int) and not isinstance(number, bool) and number in allowed
        ]
    )[:MAX_SIMILAR_ISSUES]


def _deduplicate(numbers: list[int]) -> list[int]:
    return list(dict.fromkeys(numbers))


def _sanitize_reason(value: Any, decision: str) -> str:
    fallbacks = {
        "duplicate": "The reports describe the same behavior and expected outcome.",
        "similar": (
            "The reports overlap, but the available details do not establish that "
            "they are the same issue."
        ),
        "none": "The available candidates do not describe the same underlying problem.",
    }
    if not isinstance(value, str):
        return fallbacks[decision]

    reason = re.sub(r"https?://\S+", "[link omitted]", value)
    reason = " ".join(reason.split())
    reason = reason.translate(str.maketrans({"@": "＠", "#": "＃", "<": "", ">": ""}))
    reason = reason[:MAX_REASON_LENGTH].strip()
    return reason or fallbacks[decision]
