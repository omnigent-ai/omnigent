"""Fixed diagnostic categories for managed launch failures; provider text stays private."""

from __future__ import annotations

import re

import httpx
from fastapi import HTTPException

_FAILURE_RULES = (
    (
        "authentication",
        "client_certificate_missing",
        ("never received your dbcert", "client certificate is missing"),
    ),
    (
        "connectivity",
        "host_connect_timeout",
        ("managed host did not come online", "host never connected"),
    ),
    (
        "connectivity",
        "runner_connect_timeout",
        ("runner did not connect", "runner tunnel did not connect"),
    ),
    (
        "lifecycle",
        "launch_cancelled",
        ("session was deleted", "launch was cancelled", "launch was canceled"),
    ),
    ("quota", "quota_exceeded", ("quota exceeded", "quota limit", "spend limit reached")),
    (
        "capacity",
        "capacity_unavailable",
        ("no capacity", "insufficient capacity", "capacity unavailable"),
    ),
    (
        "configuration",
        "provider_configuration_invalid",
        ("not configured", "unsupported provider", "invalid configuration", "does not support"),
    ),
    (
        "authentication",
        "sign_in_required",
        (
            "missing credentials",
            "authentication required",
            "sign in again",
            "login required",
            "http 401",
        ),
    ),
    ("transient_provisioning", "rate_limited", ("http 429", "too many requests", "rate limit")),
    (
        "transient_provisioning",
        "upstream_unavailable",
        (
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "service unavailable",
            "connection reset",
        ),
    ),
    ("connectivity", "endpoint_unavailable", ("http 404", "connection refused")),
    ("timeout", "operation_timed_out", ("timed out", "timeout")),
    ("internal", "unexpected_exception", ("internal error during",)),
)


def sandbox_launch_failure_attributes(
    error: str | None, *, exception: Exception | None = None
) -> dict[str, str]:
    """Classify bounded failure text without returning text, paths, URLs, or credentials."""
    if not error or not error.strip():
        category, reason = "unknown", "missing_detail"
    else:
        detail = error[:4096].casefold()
        category, reason = "unknown", "unclassified"
        for candidate_category, candidate_reason, phrases in _FAILURE_RULES:
            if any(phrase in detail for phrase in phrases):
                category, reason = candidate_category, candidate_reason
                break
    attributes: dict[str, str] = {
        "failure_category": category,
        "failure_reason": reason,
    }
    if exception is not None:
        exception_type = type(exception).__name__
        attributes["exception_type"] = (
            exception_type
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", exception_type)
            else "other"
        )
        if isinstance(exception, HTTPException):
            attributes["http_status"] = str(exception.status_code)
        elif isinstance(exception, httpx.HTTPStatusError):
            attributes["http_status"] = str(exception.response.status_code)
    return attributes
