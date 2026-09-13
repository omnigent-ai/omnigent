"""Trusted helpers for issue duplicate detection."""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from typing import Any


def _tunable(name: str, default: float) -> float:
    """Read a threshold from the environment so it can be calibrated in place."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and 0.0 <= value <= 1.0 else default


# Closing is destructive, so it needs strong lexical agreement AND high model
# confidence. The similar thresholds only gate a comment, so they sit lower —
# but non-zero, to keep coincidental keyword hits out of public links.
AUTO_CLOSE_CONFIDENCE = _tunable("DUPLICATE_CLOSE_MIN_CONFIDENCE", 0.92)
CLOSE_COSINE_FLOOR = _tunable("DUPLICATE_CLOSE_MIN_COSINE", 0.45)
SIMILAR_MIN_CONFIDENCE = _tunable("DUPLICATE_SIMILAR_MIN_CONFIDENCE", 0.5)
SIMILAR_COSINE_FLOOR = _tunable("DUPLICATE_SIMILAR_MIN_COSINE", 0.12)

MAX_CANDIDATES = 10
MAX_EXPLICIT_REFERENCES = 5
MAX_SIMILAR_ISSUES = 3
MIN_SIMILARITY_TOKENS = 4
DOCUMENT_BODY_CHARS = 2000

# Crash reports are filed by the crash handler and share a long traceback
# preamble (click/cli frames, "File ...", indented source lines). Left in, that
# boilerplate alone scores unrelated crashes at 0.79 cosine.
_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_TRACEBACK_LINE = re.compile(
    r"^\s*(?:Traceback \(most recent call last\)|File \".*?\", line \d+"
    r"|During handling of the above exception.*|The above exception was.*"
    r"|\s{4}\S.*)$",
    re.MULTILINE,
)

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

_FILLER_WORDS = {
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
    corpus: list[dict[str, Any]],
    limit: int = MAX_CANDIDATES,
    repository: str | None = None,
    floor: float = SIMILAR_COSINE_FLOOR,
) -> list[dict[str, Any]]:
    """Rank every older issue in the repository against `issue`.

    Scoring the whole repository rather than keyword-search hits keeps IDF
    weights fixed: a pair's score no longer depends on how many unrelated
    issues a query happened to return. Candidates below the floor are dropped
    rather than padding the list out to `limit`.
    """
    issue_number = issue.get("number")
    if isinstance(issue_number, bool) or not isinstance(issue_number, int):
        return []

    explicit_numbers = set(extract_issue_references(issue, repository))
    candidates_by_number: dict[int, dict[str, Any]] = {}
    for candidate in corpus:
        normalized = _normalize_candidate(issue_number, candidate)
        if normalized is not None:
            candidates_by_number.setdefault(normalized["number"], normalized)

    candidates = list(candidates_by_number.values())
    for candidate, score in zip(candidates, similarity_scores(issue, candidates), strict=True):
        candidate["similarity"] = round(score, 3)
        candidate["explicitReference"] = candidate["number"] in explicit_numbers

    # An explicitly referenced issue is kept regardless of wording: the author
    # pointed at it deliberately.
    retained = [
        candidate
        for candidate in candidates
        if candidate["similarity"] >= floor or candidate["explicitReference"]
    ]
    retained.sort(
        key=lambda candidate: (
            candidate["explicitReference"],
            candidate["similarity"],
            candidate["state"] == "OPEN",
            candidate["number"],
        ),
        reverse=True,
    )
    return retained[:limit]


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


def document_tokens(issue: dict[str, Any]) -> list[str]:
    """Tokenize an issue's title plus a bounded prefix of its prose body."""
    body = str(issue.get("body") or "")
    body = _TRACEBACK_LINE.sub(" ", _CODE_FENCE.sub(" ", body))
    return _similarity_tokens(f"{issue.get('title') or ''}\n{body[:DOCUMENT_BODY_CHARS]}")


def similarity_scores(issue: dict[str, Any], candidates: list[dict[str, Any]]) -> list[float]:
    """Score each candidate against the issue with TF-IDF cosine similarity.

    Rare terms dominate, so two reports of the same bug score highly even when
    worded differently, while a shared generic word like "web" barely counts.
    """
    documents = [document_tokens(issue)] + [document_tokens(candidate) for candidate in candidates]
    vectors = _tfidf_vectors(documents)
    return [_cosine(vectors[0], vector) for vector in vectors[1:]]


def _tfidf_vectors(documents: list[list[str]]) -> list[dict[str, float]]:
    total = len(documents)
    frequencies: Counter[str] = Counter()
    for tokens in documents:
        frequencies.update(set(tokens))
    idf = {term: math.log((total + 1) / (count + 1)) + 1 for term, count in frequencies.items()}

    vectors = []
    for tokens in documents:
        if not tokens:
            vectors.append({})
            continue
        counts = Counter(tokens)
        length = len(tokens)
        vectors.append({term: (count / length) * idf[term] for term, count in counts.items()})
    return vectors


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    smaller, larger = (left, right) if len(left) <= len(right) else (right, left)
    dot = sum(weight * larger.get(term, 0.0) for term, weight in smaller.items())
    if dot == 0.0:
        return 0.0
    left_norm = math.sqrt(sum(weight * weight for weight in left.values()))
    right_norm = math.sqrt(sum(weight * weight for weight in right.values()))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def reference_disposition(candidate: dict[str, Any]) -> str:
    """How a referenced issue's state changes what we can ask the reporter for.

    `open` — the discussion is live, so the reporter can move their report there.
    `fixed` — closed as completed, so hitting it again is a regression or an old
    build, and the new report has to stay open to capture that.
    `declined` — closed as not planned, so there is nothing to move a report into.
    """
    if str(candidate.get("state") or "").upper() != "CLOSED":
        return "open"
    labels = {label.casefold() for label in _label_names(candidate.get("labels"))}
    state_reason = candidate.get("stateReason", candidate.get("state_reason"))
    if str(state_reason or "").upper() == "NOT_PLANNED" or "wontfix" in labels:
        return "declined"
    return "fixed"


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
    similarity = _similarity_map(issue, list(candidates_by_number.values()))

    def close_authorized(number: int) -> bool:
        """Both signals must agree: lexical similarity AND model confidence."""
        candidate = candidates_by_number[number]
        if (
            len(set(document_tokens(issue))) < MIN_SIMILARITY_TOKENS
            or len(set(document_tokens(candidate))) < MIN_SIMILARITY_TOKENS
        ):
            return False
        return (
            confidence >= auto_close_confidence
            and similarity.get(number, 0.0) >= CLOSE_COSINE_FLOOR
        )

    def linkable(numbers: list[int]) -> list[int]:
        """Keep only links the model is reasonably sure of and text agrees with."""
        if confidence < SIMILAR_MIN_CONFIDENCE:
            return []
        return [number for number in numbers if similarity.get(number, 0.0) >= SIMILAR_COSINE_FLOOR]

    decision = "none"
    if requested_decision == "duplicate" and duplicate_of is not None:
        if close_authorized(duplicate_of):
            decision = "duplicate"
            similar_issues = []
        else:
            similar_issues = linkable(
                _deduplicate([duplicate_of, *similar_issues])[:MAX_SIMILAR_ISSUES]
            )
            decision = "similar" if similar_issues else "none"
            duplicate_of = None
    elif requested_decision == "similar" and similar_issues:
        similar_issues = linkable(similar_issues)
        decision = "similar" if similar_issues else "none"
        duplicate_of = None
    else:
        duplicate_of = None
        similar_issues = []

    # The referenced issues' own state decides what the comment can ask for, so
    # carry it alongside the numbers rather than re-fetching at comment time.
    referenced = [duplicate_of] if duplicate_of is not None else similar_issues
    dispositions = {
        str(number): reference_disposition(candidates_by_number[number])
        for number in referenced
        if number in candidates_by_number
    }

    return {
        "duplicate_decision": decision,
        "duplicate_of": duplicate_of,
        "similar_issues": similar_issues,
        "duplicate_confidence": confidence,
        "duplicate_reasoning": _duplicate_reason(decision),
        "reference_dispositions": dispositions,
    }


def _disposition_for(decision: dict[str, Any], number: int | None) -> str:
    """Look up a reference's disposition, treating anything unknown as open.

    Defaulting to `open` keeps the wording that assumes a live discussion, which
    is the safe direction: it asks the reporter to check rather than telling them
    a fix shipped.
    """
    dispositions = decision.get("reference_dispositions")
    if not isinstance(dispositions, dict):
        return "open"
    value = dispositions.get(str(number))
    return value if value in {"open", "fixed", "declined"} else "open"


def build_duplicate_comment(
    decision: dict[str, Any],
    *,
    close_issue: bool,
    reasoning: str = "",
) -> str:
    """Build the public, idempotently identifiable bot comment.

    Wording leads with the issue link — the one thing a reporter can act on —
    and avoids describing the classifier's internals. A `none` verdict produces
    no comment at all; the caller is expected not to post it.
    """
    marker = "<!-- omnigent-duplicate-check -->"

    if decision["duplicate_decision"] == "duplicate":
        issue_number = decision["duplicate_of"]
        # Only the closing case owes the reporter a justification, and only there
        # is the model's own sentence worth surfacing over a fixed string.
        explanation = f" {_one_sentence(reasoning)}" if close_issue and reasoning else ""
        if close_issue:
            message = (
                f"Thanks for reporting this. This looks like the same problem as "
                f"#{issue_number}, so I’m closing it to keep the discussion in one "
                f"place.{explanation}\n\n"
                "If it isn't the same, say so here and a maintainer will reopen it."
            )
        elif _disposition_for(decision, issue_number) == "fixed":
            message = (
                f"Thanks for reporting this. This looks like the same problem as "
                f"#{issue_number}, which has already been fixed — so the fix may "
                f"have shipped after the build you're on.\n\n"
                "Could you check whether you're on a version that includes it? If "
                "you are and this still happens, say so here — that makes it a "
                "regression rather than a duplicate, and we'll keep this open."
            )
        elif _disposition_for(decision, issue_number) == "declined":
            message = (
                f"Thanks for reporting this. This looks like the same problem as "
                f"#{issue_number}, which was closed as not planned — worth reading "
                f"for the reasoning.\n\n"
                "If your case is different from what was decided there, say what's "
                "different and we'll pick it up here."
            )
        else:
            # The reporter can settle this faster than a maintainer can: they know
            # whether the other issue covers their case. Ask them to close it
            # themselves, and say what to do when it doesn't.
            message = (
                f"Thanks for reporting this. This looks like the same problem as "
                f"#{issue_number} — could you take a look?\n\n"
                "If it covers your case, please close this one and add anything "
                f"new over on #{issue_number} so the discussion stays in one place. "
                "If it doesn't, say what's different and we'll pick it up here."
            )
    elif decision["duplicate_decision"] == "similar":
        numbers = decision["similar_issues"]
        references = ", ".join(f"#{number}" for number in numbers)
        plural = len(numbers) > 1
        dispositions = {_disposition_for(decision, number) for number in numbers}
        # A closed match cannot absorb the report: asking for a self-close would
        # send the reporter's detail somewhere nobody is reading. Mixed sets keep
        # the open ask, since at least one live issue can take it.
        if "open" in dispositions:
            covers = "they already cover" if plural else "it already covers"
            message = (
                f"Thanks for reporting this. {references} may be related — could you "
                f"take a look in case {covers} this?\n\n"
                "If it turns out to be the same problem, please close this one and add "
                "your details there. Otherwise leave a note and we'll pick it up here."
            )
        elif dispositions == {"declined"}:
            was = "were" if plural else "was"
            message = (
                f"Thanks for reporting this. {references} may be related, and {was} "
                f"closed as not planned — worth reading for the reasoning.\n\n"
                "If your case is different from what was decided there, say what's "
                "different and we'll pick it up here."
            )
        else:
            # At least one fixed match, possibly beside a declined one. Name each
            # group separately: claiming a declined issue was fixed is worse than
            # the extra clause costs.
            fixed = [n for n in numbers if _disposition_for(decision, n) == "fixed"]
            declined = [n for n in numbers if _disposition_for(decision, n) == "declined"]
            fixed_refs = ", ".join(f"#{number}" for number in fixed)
            many = len(fixed) > 1
            also = (
                " ({} {} closed as not planned, for context.)".format(
                    ", ".join(f"#{number}" for number in declined),
                    "were" if len(declined) > 1 else "was",
                )
                if declined
                else ""
            )
            message = (
                f"Thanks for reporting this. {fixed_refs} may be related, and "
                f"{'have' if many else 'has'} already been fixed — so the "
                f"{'fixes' if many else 'fix'} may have shipped after the build "
                f"you're on.{also}\n\n"
                "Could you check whether you're on a version that includes "
                f"{'them' if many else 'it'}? If you are and this still happens, "
                "say so here — that makes it a regression rather than a duplicate, "
                "and we'll keep this open."
            )
    else:
        return ""

    return f"{marker}\n{message}\n"


_MENTION = re.compile(r"@+([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))")
# `//host` is scheme-relative and still renders as an external link, so it is
# matched alongside the explicit schemes. Bare domains are left alone: GitHub
# does not autolink them.
_URL = re.compile(r"(?:\b(?:https?://|www\.)|(?<![\w:/])//)\S+", re.IGNORECASE)
_ISSUE_REF = re.compile(r"(?:#|\bGH-)\d+", re.IGNORECASE)
REASON_MAX_CHARS = 240


def _one_sentence(text: str) -> str:
    """Reduce model prose to one sanitized sentence fit for a public comment.

    The model's text is derived from attacker-controllable issue content, so it
    is never posted verbatim: mentions would ping real people, links could
    phish under the bot's badge, and issue refs would cross-link unrelated
    threads. Each is defanged rather than dropped so the sentence still reads.
    """
    collapsed = " ".join(text.split())
    if not collapsed:
        return ""
    collapsed = _URL.sub("[link removed]", collapsed)
    collapsed = _MENTION.sub(r"\1", collapsed)
    collapsed = _ISSUE_REF.sub("an issue", collapsed)
    head, separator, _ = collapsed.partition(". ")
    sentence = head + ("." if separator else "")
    if not sentence.endswith("."):
        sentence = f"{sentence}."
    if len(sentence) > REASON_MAX_CHARS:
        sentence = f"{sentence[:REASON_MAX_CHARS].rstrip()}…"
    return sentence


def _similarity_map(issue: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[int, float]:
    """Collect the similarity score for each candidate.

    `rank_candidates` scores against the whole repository, so its cached value
    is authoritative: IDF weights are relative to the documents they are
    computed over, and rescoring a short list would silently shift the gate.
    """
    missing = [candidate for candidate in candidates if candidate.get("similarity") is None]
    rescored = dict(
        zip(
            (candidate["number"] for candidate in missing),
            similarity_scores(issue, missing),
            strict=True,
        )
    )
    return {
        candidate["number"]: (
            float(candidate["similarity"])
            if candidate.get("similarity") is not None
            else rescored[candidate["number"]]
        )
        for candidate in candidates
    }


def _similarity_tokens(text: str) -> list[str]:
    """Split into scoring terms, dropping stop words and issue-tracker filler."""
    normalized = text.lower().replace("_", " ").replace("-", " ")
    return [
        token
        for token in re.findall(r"[a-z0-9][a-z0-9]+", normalized)
        if (len(token) >= 3 or token in _SHORT_TECH_TERMS)
        and token not in _STOP_WORDS
        and token not in _FILLER_WORDS
    ]


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
        "stateReason": str(
            candidate.get("stateReason", candidate.get("state_reason")) or ""
        ).upper(),
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
