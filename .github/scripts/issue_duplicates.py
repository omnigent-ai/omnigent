"""Trusted helpers for issue duplicate detection."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from typing import Any

AUTO_CLOSE_CONFIDENCE = 0.92
MAX_CANDIDATES = 10
MAX_SEARCH_QUERIES = 4
MAX_EXPLICIT_REFERENCES = 5
MAX_SIMILAR_ISSUES = 3
MIN_CLOSE_TITLE_TOKENS = 4
MIN_CLOSE_DOCUMENT_TOKENS = 8
MIN_CLOSE_TITLE_JACCARD = 0.8
MIN_CLOSE_DOCUMENT_JACCARD = 0.25
MIN_CLOSE_DOCUMENT_CONTAINMENT = 0.5

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

_SEARCH_NOISE = {
    "ability",
    "add",
    "allow",
    "bug",
    "can",
    "cannot",
    "does",
    "every",
    "feature",
    "get",
    "issue",
    "make",
    "new",
    "only",
    "same",
    "should",
    "support",
    "use",
    "using",
}

_SHORT_TECH_TERMS = {"ci", "db", "go", "os", "ui"}


def build_search_queries(issue: dict[str, Any], limit: int = MAX_SEARCH_QUERIES) -> list[str]:
    """Build short phrase queries spread across an issue title."""
    if limit <= 0:
        return []

    title = str(issue.get("title") or "")
    body = str(issue.get("body") or "")[:1000]
    title_tokens = _query_tokens(title)
    tokens = title_tokens if len(title_tokens) >= 2 else title_tokens + _query_tokens(body)

    if not tokens:
        return []
    if len(tokens) == 1:
        return tokens

    pairs = list(
        dict.fromkeys(" ".join(tokens[index : index + 2]) for index in range(len(tokens) - 1))
    )
    if limit == 1:
        return pairs[:1]
    if len(pairs) <= limit:
        return pairs

    indexes = [index * (len(pairs) - 1) // (limit - 1) for index in range(limit)]
    return [pairs[index] for index in dict.fromkeys(indexes)]


def extract_issue_references(
    issue: dict[str, Any],
    repository: str | None = None,
    limit: int = MAX_EXPLICIT_REFERENCES,
) -> list[int]:
    """Extract older issue references from title and body text."""
    issue_number = issue.get("number")
    if isinstance(issue_number, bool) or not isinstance(issue_number, int):
        return []

    text = f"{issue.get('title') or ''}\n{issue.get('body') or ''}"
    references = []
    if repository:
        repository_pattern = re.escape(repository)
        reference_pattern = re.compile(
            rf"(?<![\w/-])#(\d{{1,10}})\b|"
            rf"(?:https://github\.com/)?{repository_pattern}(?:/issues/|#)(\d{{1,10}})\b",
            re.IGNORECASE,
        )
        values = (
            next(value for value in match.groups() if value)
            for match in reference_pattern.finditer(text)
        )
    else:
        values = re.findall(r"(?:#|/issues/)(\d{1,10})\b", text)

    for value in values:
        number = int(value)
        if number < issue_number and number not in references:
            references.append(number)
        if len(references) == limit:
            break
    return references


def rank_candidates(
    issue: dict[str, Any],
    search_candidates: list[dict[str, Any]],
    referenced_candidates: list[dict[str, Any]],
    limit: int = MAX_CANDIDATES,
) -> list[dict[str, Any]]:
    """Normalize, deduplicate, and rank candidate issues for classification."""
    issue_number = issue.get("number")
    if isinstance(issue_number, bool) or not isinstance(issue_number, int):
        return []

    explicit_numbers = set(extract_issue_references(issue))
    candidates_by_number: dict[int, dict[str, Any]] = {}
    search_hits: Counter[int] = Counter()

    for candidate in search_candidates:
        normalized = _normalize_candidate(issue_number, candidate)
        if normalized is None:
            continue
        number = normalized["number"]
        search_hits[number] += 1
        candidates_by_number.setdefault(number, normalized)

    for candidate in referenced_candidates:
        normalized = _normalize_candidate(issue_number, candidate)
        if normalized is not None and normalized["number"] in explicit_numbers:
            candidates_by_number[normalized["number"]] = normalized

    issue_tokens = set(_query_tokens(str(issue.get("title") or "")))

    def score(candidate: dict[str, Any]) -> tuple[Any, ...]:
        number = candidate["number"]
        candidate_tokens = set(_query_tokens(candidate["title"]))
        shared = issue_tokens & candidate_tokens
        union = issue_tokens | candidate_tokens
        overlap = len(shared) / len(union) if union else 0.0
        technical_matches = sum(_is_technical_token(token) for token in shared)
        return (
            number in explicit_numbers,
            search_hits[number],
            technical_matches,
            len(shared),
            overlap,
            candidate["state"] == "OPEN",
            number,
        )

    ranked = sorted(candidates_by_number.values(), key=score, reverse=True)[:limit]
    for candidate in ranked:
        number = candidate["number"]
        candidate["explicitReference"] = number in explicit_numbers
        candidate["searchHits"] = search_hits[number]
    return ranked


def format_candidates_for_prompt(candidates: list[dict[str, Any]]) -> str:
    """Serialize candidates without adding prompt-like framing."""
    if not candidates:
        return "None found."
    return json.dumps(candidates, ensure_ascii=False, indent=2)


def parse_triage_output(raw: str) -> dict[str, Any]:
    """Parse exactly one JSON object, optionally wrapped in one code fence."""
    value = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", value, re.DOTALL | re.IGNORECASE)
    if fenced is not None:
        value = fenced.group(1).strip()

    try:
        result = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("triage output must be exactly one JSON object") from error
    if not isinstance(result, dict):
        raise ValueError("triage output must be a JSON object")
    return result


def deterministic_duplicate_match(issue: dict[str, Any], candidate: dict[str, Any]) -> bool:
    """Require strong lexical agreement before auto-closing."""
    issue_title = set(_similarity_tokens(str(issue.get("title") or "")))
    candidate_title = set(_similarity_tokens(str(candidate.get("title") or "")))
    if len(issue_title) < MIN_CLOSE_TITLE_TOKENS or len(candidate_title) < MIN_CLOSE_TITLE_TOKENS:
        return False

    title_union = issue_title | candidate_title
    title_jaccard = len(issue_title & candidate_title) / len(title_union)
    if title_jaccard < MIN_CLOSE_TITLE_JACCARD:
        return False

    issue_document = set(
        _similarity_tokens(f"{issue.get('title') or ''}\n{str(issue.get('body') or '')[:2000]}")
    )
    candidate_document = set(
        _similarity_tokens(
            f"{candidate.get('title') or ''}\n{str(candidate.get('body') or '')[:2000]}"
        )
    )
    if (
        len(issue_document) < MIN_CLOSE_DOCUMENT_TOKENS
        or len(candidate_document) < MIN_CLOSE_DOCUMENT_TOKENS
    ):
        return False

    shared_document = issue_document & candidate_document
    document_jaccard = len(shared_document) / len(issue_document | candidate_document)
    document_containment = len(shared_document) / min(len(issue_document), len(candidate_document))
    return (
        document_jaccard >= MIN_CLOSE_DOCUMENT_JACCARD
        and document_containment >= MIN_CLOSE_DOCUMENT_CONTAINMENT
    )


def validate_duplicate_decision(
    result: dict[str, Any],
    issue: dict[str, Any],
    candidates: list[dict[str, Any]],
    auto_close_confidence: float = AUTO_CLOSE_CONFIDENCE,
) -> dict[str, Any]:
    """Validate the model's duplicate decision against prefetched candidates."""
    candidates_by_number = {
        candidate["number"]: candidate
        for candidate in candidates
        if isinstance(candidate.get("number"), int)
        and not isinstance(candidate.get("number"), bool)
    }
    candidate_numbers = set(candidates_by_number)
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
    decision = "none"
    if requested_decision == "duplicate" and duplicate_of is not None:
        if confidence >= auto_close_confidence and deterministic_duplicate_match(
            issue, candidates_by_number[duplicate_of]
        ):
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

    return {
        "duplicate_decision": decision,
        "duplicate_of": duplicate_of,
        "similar_issues": similar_issues,
        "duplicate_confidence": confidence,
        "duplicate_reasoning": _duplicate_reason(decision),
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
        if (len(token) >= 3 or token in _SHORT_TECH_TERMS) and token not in _STOP_WORDS
    ]


def _query_tokens(text: str) -> list[str]:
    return [token for token in _search_tokens(text) if token not in _SEARCH_NOISE]


def _similarity_tokens(text: str) -> list[str]:
    return _query_tokens(text.replace("_", " ").replace("-", " "))


def _is_technical_token(token: str) -> bool:
    return "_" in token or "-" in token or any(character.isdigit() for character in token)


def _normalize_candidate(issue_number: int, candidate: dict[str, Any]) -> dict[str, Any] | None:
    number = candidate.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number >= issue_number:
        return None

    labels = _label_names(candidate.get("labels"))
    if any(label.casefold() == "duplicate" for label in labels):
        return None

    state = str(candidate.get("state") or "UNKNOWN").upper()
    if state not in {"OPEN", "CLOSED"}:
        return None

    return {
        "number": number,
        "title": str(candidate.get("title") or "")[:500],
        "body": str(candidate.get("body") or "")[:2000],
        "state": state,
        "url": str(candidate.get("url") or ""),
        "createdAt": candidate.get("createdAt"),
        "updatedAt": candidate.get("updatedAt"),
        "labels": labels,
    }


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


def _duplicate_reason(decision: str) -> str:
    return {
        "duplicate": "The reports describe the same behavior and expected outcome.",
        "similar": (
            "The reports overlap, but automatic checks do not establish that they "
            "are the same issue."
        ),
        "none": "The available candidates do not describe the same underlying problem.",
    }[decision]
