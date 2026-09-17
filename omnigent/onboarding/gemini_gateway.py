"""Endpoint conventions for agy's native Gemini API transport."""

from urllib.parse import urlsplit

from omnigent.errors import ErrorCode, OmnigentError

GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com"
GEMINI_BASE_URL_ENV = "GOOGLE_GEMINI_BASE_URL"


def validate_gemini_base_url(value: str) -> str:
    """Return an HTTP(S) API root, without agy's generated /v1beta suffix."""
    value = value.strip().rstrip("/")
    error = (
        "Gemini gateway URL must be an http:// or https:// API root "
        "without credentials, a query, or a fragment."
    )
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
        _ = parsed.port
    except ValueError:
        raise OmnigentError(error, code=ErrorCode.INVALID_INPUT) from None
    if not valid:
        raise OmnigentError(
            error,
            code=ErrorCode.INVALID_INPUT,
        )
    if parsed.path.endswith(("/v1beta", "/openai")) or "/models/" in parsed.path:
        raise OmnigentError(
            "Use the Gemini gateway API root, without /v1beta, /openai, or a model path; "
            "agy appends /v1beta/models/... itself.",
            code=ErrorCode.INVALID_INPUT,
        )
    return value
