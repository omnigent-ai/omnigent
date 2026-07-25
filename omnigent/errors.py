"""Centralized error handling for the omnigent server.

All user-facing errors should be raised as OmnigentError with an
appropriate error code. The FastAPI exception handler (registered in
server/app.py) catches these and returns a JSON response with the
correct HTTP status code.

Existing HTTPException usage continues to work — FastAPI handles both.
New code should prefer OmnigentError for consistency.
"""

from __future__ import annotations


class ErrorCode:
    """
    Error codes and their HTTP status mappings.

    Add new codes here as needed. The string value is what appears
    in the JSON response body.

    :cvar NOT_FOUND: Resource does not exist (HTTP 404).
    :cvar INVALID_INPUT: Request validation failed (HTTP 400).
    :cvar ALREADY_EXISTS: Duplicate resource (HTTP 409).
    :cvar CONFLICT: Operation conflicts with current state (HTTP 409).
    :cvar INTERNAL_ERROR: Unexpected server error (HTTP 500).
    :cvar HARNESS_PROTOCOL_VIOLATION: A harness emitted an SSE
        sequence that violates the Omnigent↔harness contract — e.g.
        ``response.completed`` with outstanding elicitations or
        outstanding ``tool_results`` round-trips. Server bug in
        the harness implementation, not user input. Surfaces as
        the ``error.code`` on a ``TaskStatus.FAILED`` response
        (HTTP 500). See ``designs/SERVER_HARNESS_CONTRACT.md``
        §Elicitation completion invariant.
    :cvar RUNNER_UNAVAILABLE: No online runner can serve the
        requested dispatch (HTTP 503).
    :cvar UNAUTHORIZED: No valid authentication credentials (HTTP 401).
    :cvar FORBIDDEN: Authenticated but insufficient permissions (HTTP 403).
    :cvar RUNNER_CAPABILITY_MISMATCH: The selected runner cannot
        spawn the requested harness kind (HTTP 503).
    :cvar HARNESS_NOT_CONFIGURED: The session's harness is not
        configured on the selected host — its CLI is missing or no
        default credential is set (the host refused the launch with
        the ``harness_not_configured`` error code). HTTP 412
        rather than 400 (the request is valid against a configured
        host) or 503 (retrying cannot succeed without user action —
        running ``omnigent setup`` on the host machine).
    """

    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    INVALID_INPUT = "invalid_input"
    ALREADY_EXISTS = "already_exists"
    CONFLICT = "conflict"
    INTERNAL_ERROR = "internal_error"
    HARNESS_PROTOCOL_VIOLATION = "harness_protocol_violation"
    RUNNER_UNAVAILABLE = "runner_unavailable"
    RUNNER_CAPABILITY_MISMATCH = "runner_capability_mismatch"
    # Keep the string equal to frames.HARNESS_NOT_CONFIGURED_ERROR_CODE —
    # the host's wire error code passes through as the API error code.
    HARNESS_NOT_CONFIGURED = "harness_not_configured"


# Single source of truth for error code → HTTP status.
_CODE_TO_HTTP_STATUS: dict[str, int] = {
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.INVALID_INPUT: 400,
    ErrorCode.ALREADY_EXISTS: 409,
    ErrorCode.CONFLICT: 409,
    ErrorCode.INTERNAL_ERROR: 500,
    # Harness protocol violations are server-side bugs in the
    # harness implementation — surface as 500 (no client action
    # can fix them; investigation needed in the harness wrap).
    ErrorCode.HARNESS_PROTOCOL_VIOLATION: 500,
    ErrorCode.RUNNER_UNAVAILABLE: 503,
    ErrorCode.RUNNER_CAPABILITY_MISMATCH: 503,
    # 412 Precondition Failed: the request is well-formed but the host
    # can't satisfy it until the user runs `omnigent setup` there —
    # neither a 400 (input is fine) nor a 503 (a retry won't help).
    ErrorCode.HARNESS_NOT_CONFIGURED: 412,
}


class OmnigentError(Exception):
    """
    Application-level error with a machine-readable code.

    Raise this from routes, stores, or any layer. The global FastAPI
    exception handler converts it to a JSON response automatically.
    """

    def __init__(self, message: str, *, code: str = ErrorCode.INTERNAL_ERROR) -> None:
        """
        Create a new application error.

        :param message: Human-readable error description.
        :param code: Machine-readable error code from
            :class:`ErrorCode`, e.g. ``ErrorCode.NOT_FOUND``.
        """
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def http_status(self) -> int:
        """
        Map this error's code to an HTTP status code.

        :returns: HTTP status (e.g. 404 for ``NOT_FOUND``).
            Defaults to 500 for unknown codes.
        """
        return _CODE_TO_HTTP_STATUS.get(self.code, 500)


class ElicitationDeclinedError(Exception):
    """Raised when a user explicitly declines an elicitation (action == "decline").

    Distinct from a timeout or cancel: the user made an active choice to
    refuse. Callers that park on an ASK gate raise this instead of
    returning ``False`` so the turn loop can abort cleanly rather than
    feeding a DENY message to the LLM and letting it continue.

    :param message: Human-readable description, typically the policy
        reason that triggered the elicitation.
    :param policy_name: Name of the deciding policy, e.g.
        ``"intent_based_authorization"``. ``None`` when not available.
    """

    def __init__(self, message: str = "", *, policy_name: str | None = None) -> None:
        super().__init__(message)
        self.policy_name = policy_name
