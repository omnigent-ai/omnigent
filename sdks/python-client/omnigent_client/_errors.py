"""Typed exceptions for the omnigent client."""

from __future__ import annotations

import httpx

_BODY_PREVIEW_CHARS = 200


class OmnigentError(Exception):
    """Base exception for all omnigent client errors."""

    def __init__(self, message: str, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class AgentNotFoundError(OmnigentError):
    """Agent name or ID not found (HTTP 404)."""


class ResponseNotFoundError(OmnigentError):
    """Response ID not found (HTTP 404)."""


class FileNotFoundError(OmnigentError):
    """File ID not found (HTTP 404)."""


class ConversationNotFoundError(OmnigentError):
    """Conversation ID not found (HTTP 404)."""


class InvalidInputError(OmnigentError):
    """Bad request — invalid input, missing fields, etc. (HTTP 400)."""


class ConflictError(OmnigentError):
    """Resource conflict — duplicate name, stale state, etc. (HTTP 409)."""


class BundleInvalidError(OmnigentError):
    """Agent bundle is invalid — corrupt tarball, bad config, etc. (HTTP 400)."""


class RateLimitedError(OmnigentError):
    """Request was rate-limited (HTTP 429) — transient, safe to retry."""


class ServerError(OmnigentError):
    """Internal server error (HTTP 5xx)."""


class ToolCallDenied(Exception):
    """Raised by ``on_tool_call_start`` hook to deny a client-side tool call.

    The exception message is sent back to the agent as the tool's output,
    so the agent knows the call was denied and can adapt.
    """


def raise_for_status(status_code: int, body: dict[str, object] | str) -> None:
    """Raise a typed exception based on HTTP status code and error body.

    Uses the server's error ``code`` field for classification when
    available, falling back to status code only. Never relies on
    substring matching in error messages.
    """
    if status_code < 400:
        return

    if isinstance(body, dict):
        error = body.get("error", {})
        if isinstance(error, dict):
            code = str(error.get("code", ""))
            message = str(error.get("message", str(body)))
        else:
            code = ""
            message = str(body)
    else:
        code = ""
        message = str(body)

    # Use the server's error code for precise classification.
    _CODE_MAP: dict[str, type[OmnigentError]] = {
        "not_found": OmnigentError,
        "invalid_input": InvalidInputError,
        "conflict": ConflictError,
        "server_error": ServerError,
    }

    if status_code == 404:
        # 404 is always "not found" — the specific type (agent, response,
        # file, conversation) is determined by the endpoint the caller
        # hit, not the error message. Callers can catch the base
        # OmnigentError or the specific subclass at the call site.
        raise OmnigentError(message, status_code, code)

    if status_code == 409:
        raise ConflictError(message, status_code, code)

    if status_code == 400:
        raise InvalidInputError(message, status_code, code)

    if status_code == 429:
        raise RateLimitedError(message, status_code, code)

    if status_code >= 500:
        raise ServerError(message, status_code, code)

    raise OmnigentError(message, status_code, code)


def response_body(resp: httpx.Response) -> dict[str, object] | str:
    """
    Decode an HTTP response body for status handling.

    Server errors and proxy/auth failures are not guaranteed to
    return JSON. This helper preserves structured JSON error
    envelopes when available and falls back to raw response text
    otherwise.

    :param resp: HTTP response returned by httpx.
    :returns: Parsed JSON object, or raw text when the body is not
        a JSON object.
    """
    try:
        data = resp.json()
    except ValueError:
        return resp.text
    if isinstance(data, dict):
        return data
    return resp.text


def require_json_object(resp: httpx.Response, endpoint: str) -> dict[str, object]:
    """
    Return a successful endpoint body as a JSON object.

    Used after :func:`raise_for_status` when an endpoint's success
    contract is a JSON object. A non-JSON response at this point is a
    protocol/base-URL/auth problem, so raise :class:`OmnigentError`
    with status, content type, and a short body preview instead of
    leaking ``JSONDecodeError``.

    :param resp: HTTP response returned by httpx.
    :param endpoint: Human-readable endpoint label, e.g.
        ``"GET /v1/conversations"``.
    :returns: Parsed JSON object body.
    :raises OmnigentError: If the body is not a JSON object.
    """
    try:
        data = resp.json()
    except ValueError as exc:
        content_type = resp.headers.get("content-type")
        content_type_detail = (
            f"content-type={content_type}"
            if content_type is not None
            else "no content-type header"
        )
        preview = _response_text_preview(resp.text)
        raise OmnigentError(
            f"{endpoint} returned non-JSON response "
            f"(status={resp.status_code}, {content_type_detail}): {preview}",
            resp.status_code,
        ) from exc
    if not isinstance(data, dict):
        raise OmnigentError(
            f"{endpoint} returned JSON {type(data).__name__}, expected object",
            resp.status_code,
        )
    return data


def _response_text_preview(text: str) -> str:
    """
    Return a compact, single-line response body preview.

    :param text: Raw response text from HTTPX.
    :returns: Whitespace-normalized body preview, capped at
        ``_BODY_PREVIEW_CHARS`` characters, or ``"<empty body>"``.
    """
    preview = " ".join(text.split())[:_BODY_PREVIEW_CHARS]
    if preview:
        return preview
    return "<empty body>"
