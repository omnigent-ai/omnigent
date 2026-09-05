"""Centralized error handling for the omnigent server.

All user-facing errors should be raised as OmnigentError with an
appropriate error code. The FastAPI exception handler (registered in
server/app.py) catches these and returns a JSON response with the
correct HTTP status code.

Existing HTTPException usage continues to work — FastAPI handles both.
New code should prefer OmnigentError for consistency.
"""

from __future__ import annotations

from enum import Enum


class ErrorCategory(str, Enum):
    """Fault attribution for an error: who must change something to prevent it.

    Orthogonal to the HTTP-status / retryable axis. Emitted on error logs as a
    queryable ``error_category`` so failures can be split by owner for
    fault-share, alerting, and remediation.

    :cvar USER: The human's input, choice, or action. Declined an elicitation, a
        bad path, a stale id, deleted their own workspace, a prompt too large.
        Working as designed; no software fix.
    :cvar CLIENT: The calling software (web UI, SDK, integration) formed a bad
        request: wrong shape, missing field, a token it held but dropped. A
        correct client would not hit it.
    :cvar CONFIG: Deployment, credentials, or install is wrong or incomplete: no
        runner deployed, harness not configured, a bad provider key.
    :cvar SERVER: Our bug or internal/infra failure, at a site that knows the
        fault is ours. A confirmed server fault.
    :cvar UPSTREAM: A provider or dependency failed: LLM 429/5xx, a provider
        timeout.
    :cvar UNKNOWN: Not attributable where observed, with no rule written yet.
        Legal ONLY where no error code exists (the catch-all handler, an uncoded
        frame failure); never for a named :class:`ErrorCode`. A measured
        burn-down bucket, not a resting place.
    """

    USER = "user"
    CLIENT = "client"
    CONFIG = "config"
    SERVER = "server"
    UPSTREAM = "upstream"
    UNKNOWN = "unknown"


class ErrorImpact(str, Enum):
    """Whether an error actually halted progress on the session/task.

    Orthogonal to :class:`ErrorCategory`. ``category`` answers "whose fault";
    ``impact`` answers "did the unit of work actually stop". A server-owned
    ``wrong_replica`` or a recovered retry is not blocking despite looking
    alarming, so the two axes must be read together.

    :cvar BLOCKING: Halted the current turn/task, or left the session unable to
        proceed without intervention. The lost-progress signal for SLOs.
    :cvar TRANSIENT: Interrupted but self-recovering (a retry, a reconnect wait,
        a re-address). Progress is delayed, not lost, unless it later escalates
        to BLOCKING at the turn's terminal boundary.
    :cvar BENIGN: Surfaced or rejected without threatening progress (a bad
        request the session shrugs off, a single denied action, informational).
    :cvar UNKNOWN: Could not be determined. Used when an arbitrary logged
        exception has no code to read impact from; never for a named
        :class:`ErrorCode`. The turn's terminal outcome stays authoritative.
    """

    BLOCKING = "blocking"
    TRANSIENT = "transient"
    BENIGN = "benign"
    UNKNOWN = "unknown"


class ErrorPhase(str, Enum):
    """Where in the session/turn lifecycle an error happened.

    The third axis, orthogonal to category (who) and impact (did it block): it
    localizes the failure so "a blocking config error in HARNESS_SETUP" points at
    a different fix than "a blocking upstream error in TURN". Members are declared
    in lifecycle order; :func:`is_before_harness_start` splits them at the boundary
    the operator usually cares about.

    :cvar REQUEST: Request receipt, schema validation, auth (before any session
        work): 422, invalid_input, unauthorized, forbidden.
    :cvar ROUTING: Replica / runner routing (wrong_replica).
    :cvar RUNNER_LAUNCH: The host is launching or connecting the runner process
        (runner_unavailable, runner_capability_mismatch, launch failed).
    :cvar HARNESS_SETUP: Harness preconditions on the runner before the process
        starts (harness_not_configured, workspace_missing). Still before start.
    :cvar HARNESS_STARTUP: The harness process is spawning / handshaking /
        initializing. The start itself.
    :cvar TURN: A turn is running after the harness is up (LLM calls, tool
        dispatch, elicitations, harness_protocol_violation). After start.
    :cvar TEARDOWN: Session/turn finalization, persistence, cleanup.
    :cvar UNKNOWN: Not localized (e.g. a generic internal error with no active
        phase scope).
    """

    REQUEST = "request"
    ROUTING = "routing"
    RUNNER_LAUNCH = "runner_launch"
    HARNESS_SETUP = "harness_setup"
    HARNESS_STARTUP = "harness_startup"
    TURN = "turn"
    TEARDOWN = "teardown"
    UNKNOWN = "unknown"


# Lifecycle order used only for the before/after-harness-start split.
_PHASE_ORDER = (
    ErrorPhase.REQUEST,
    ErrorPhase.ROUTING,
    ErrorPhase.RUNNER_LAUNCH,
    ErrorPhase.HARNESS_SETUP,
    ErrorPhase.HARNESS_STARTUP,
    ErrorPhase.TURN,
    ErrorPhase.TEARDOWN,
)


def is_before_harness_start(phase: ErrorPhase) -> bool:
    """Whether *phase* precedes the harness process starting.

    The boundary is :attr:`ErrorPhase.HARNESS_STARTUP` (the start itself is not
    "before"). ``UNKNOWN`` returns ``False`` (we cannot claim it failed before
    start).

    :param phase: The lifecycle phase.
    :returns: ``True`` for REQUEST..HARNESS_SETUP, ``False`` otherwise.
    """
    if phase not in _PHASE_ORDER:
        return False
    return _PHASE_ORDER.index(phase) < _PHASE_ORDER.index(ErrorPhase.HARNESS_STARTUP)


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
    :cvar WRONG_REPLICA: The session's bound runner exists but its
        tunnel is not registered on the replica that served this request
        (HTTP 400). When replicas are sharded by host, a request keyed for
        one host can reach a replica that doesn't hold its tunnel — the key
        doesn't match where the tunnel lives. The request itself is valid
        (the same bytes succeed on the right replica), so the fix is to
        re-address it: reissue WITHOUT the key and reach the host via the
        default route. Distinct from ``RUNNER_UNAVAILABLE`` (no runner
        bound anywhere), which no re-addressing can fix.
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
    :cvar WORKSPACE_MISSING: The session's bound workspace no longer
        exists on the selected host (HTTP 410). Retrying cannot recreate
        deleted workspace state; the user must start a session in a valid
        workspace.
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
    WRONG_REPLICA = "wrong_replica"
    RUNNER_CAPABILITY_MISMATCH = "runner_capability_mismatch"
    # Keep the string equal to frames.HARNESS_NOT_CONFIGURED_ERROR_CODE —
    # the host's wire error code passes through as the API error code.
    HARNESS_NOT_CONFIGURED = "harness_not_configured"
    WORKSPACE_MISSING = "workspace_missing"


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
    # 400, not 503: the request reached a replica that can't serve it, but the
    # request is valid — the fix is to re-address it (reissue without the key),
    # not to wait and retry. A 4xx also keeps this expected routing event out of
    # 5xx error-rate signals and clear of infra retry policies (which would
    # resend to the same wrong replica). The distinct code string is what a
    # key-aware client keys the re-address off; a client that doesn't know the
    # code just sees a clean client-error, not a phantom outage.
    ErrorCode.WRONG_REPLICA: 400,
    ErrorCode.RUNNER_CAPABILITY_MISMATCH: 503,
    # 412 Precondition Failed: the request is well-formed but the host
    # can't satisfy it until the user runs `omnigent setup` there —
    # neither a 400 (input is fine) nor a 503 (a retry won't help).
    ErrorCode.HARNESS_NOT_CONFIGURED: 412,
    ErrorCode.WORKSPACE_MISSING: 410,
}


# Fault attribution per error code. Single source of truth, paired with
# _CODE_TO_HTTP_STATUS above. Every ErrorCode MUST have a concrete (non-UNKNOWN)
# entry; a test asserts it so a new code cannot merge uncategorized.
_CODE_TO_CATEGORY: dict[str, ErrorCategory] = {
    # A human presented no credential, lacks permission, or referenced something
    # absent or already-taken. INVALID_INPUT defaults to user (a business-rule
    # rejection of a well-formed request); the schema/transport layer raises it
    # with category=CLIENT instead (a malformed request is the caller's bug).
    ErrorCode.UNAUTHORIZED: ErrorCategory.USER,
    ErrorCode.FORBIDDEN: ErrorCategory.USER,
    ErrorCode.NOT_FOUND: ErrorCategory.USER,
    ErrorCode.INVALID_INPUT: ErrorCategory.USER,
    ErrorCode.ALREADY_EXISTS: ErrorCategory.USER,
    ErrorCode.CONFLICT: ErrorCategory.USER,
    # Our fault, raised where the site knows it is ours.
    ErrorCode.INTERNAL_ERROR: ErrorCategory.SERVER,
    ErrorCode.HARNESS_PROTOCOL_VIOLATION: ErrorCategory.SERVER,
    # WRONG_REPLICA is a routing/sharding artifact we own. It is expected and
    # self-healing, so it must not page: keep alert severity a separate axis.
    ErrorCode.WRONG_REPLICA: ErrorCategory.SERVER,
    # A valid request the caller cannot fix by retry or re-input; the
    # deployment/host must change.
    ErrorCode.RUNNER_UNAVAILABLE: ErrorCategory.CONFIG,
    ErrorCode.RUNNER_CAPABILITY_MISMATCH: ErrorCategory.CONFIG,
    ErrorCode.HARNESS_NOT_CONFIGURED: ErrorCategory.CONFIG,
    # The human deleted their own workspace on the host.
    ErrorCode.WORKSPACE_MISSING: ErrorCategory.USER,
}


def category_for_code(code: str) -> ErrorCategory:
    """Return the fault attribution for an error code.

    :param code: An :class:`ErrorCode` string value.
    :returns: The mapped category, or ``UNKNOWN`` for a code outside the
        :class:`ErrorCode` namespace. A named ``ErrorCode`` is always mapped
        (enforced by test), so ``UNKNOWN`` here means the code is not one.
    """
    return _CODE_TO_CATEGORY.get(code, ErrorCategory.UNKNOWN)


# Typical progress impact per error code. This is a DEFAULT: the authoritative
# blocking signal for a session/task is the turn's terminal outcome (see
# omnigent.runtime.session_stream._log_turn_outcome), which overrides a
# nominally-TRANSIENT error that ultimately fails the turn. Every ErrorCode MUST
# have an entry (a test asserts it).
_CODE_TO_IMPACT: dict[str, ErrorImpact] = {
    # Nothing proceeds until the caller authenticates.
    ErrorCode.UNAUTHORIZED: ErrorImpact.BLOCKING,
    ErrorCode.INTERNAL_ERROR: ErrorImpact.BLOCKING,
    ErrorCode.HARNESS_PROTOCOL_VIOLATION: ErrorImpact.BLOCKING,
    # Launch-time hard stops: the task cannot start until the host/deploy changes.
    ErrorCode.RUNNER_CAPABILITY_MISMATCH: ErrorImpact.BLOCKING,
    ErrorCode.HARNESS_NOT_CONFIGURED: ErrorImpact.BLOCKING,
    ErrorCode.WORKSPACE_MISSING: ErrorImpact.BLOCKING,
    # Self-healing: a session state that resumes on reconnect, and a routing
    # artifact the client re-addresses. No progress is lost.
    ErrorCode.RUNNER_UNAVAILABLE: ErrorImpact.TRANSIENT,
    ErrorCode.WRONG_REPLICA: ErrorImpact.TRANSIENT,
    # A single rejected request; the session stays healthy and usable.
    ErrorCode.FORBIDDEN: ErrorImpact.BENIGN,
    ErrorCode.NOT_FOUND: ErrorImpact.BENIGN,
    ErrorCode.INVALID_INPUT: ErrorImpact.BENIGN,
    ErrorCode.ALREADY_EXISTS: ErrorImpact.BENIGN,
    ErrorCode.CONFLICT: ErrorImpact.BENIGN,
}


def impact_for_code(code: str) -> ErrorImpact:
    """Return the typical progress impact for an error code.

    :param code: An :class:`ErrorCode` string value.
    :returns: The mapped impact, or ``BENIGN`` for a code outside the
        :class:`ErrorCode` namespace (an unrecognized code has not shown itself
        to block; the turn's terminal outcome stays the authoritative override).
    """
    return _CODE_TO_IMPACT.get(code, ErrorImpact.BENIGN)


# Lifecycle phase a code most likely failed in. A DEFAULT for coded errors; an
# active phase_scope (ambient ContextVar) localizes uncoded exceptions. Generic
# codes map to UNKNOWN (context, not the code, tells you where). Every ErrorCode
# has an entry (a test asserts presence); UNKNOWN is allowed here, unlike the
# other two axes, because a phase is genuinely context-dependent.
_CODE_TO_PHASE: dict[str, ErrorPhase] = {
    ErrorCode.UNAUTHORIZED: ErrorPhase.REQUEST,
    ErrorCode.FORBIDDEN: ErrorPhase.REQUEST,
    ErrorCode.NOT_FOUND: ErrorPhase.REQUEST,
    ErrorCode.INVALID_INPUT: ErrorPhase.REQUEST,
    ErrorCode.ALREADY_EXISTS: ErrorPhase.REQUEST,
    ErrorCode.CONFLICT: ErrorPhase.REQUEST,
    ErrorCode.WRONG_REPLICA: ErrorPhase.ROUTING,
    ErrorCode.RUNNER_UNAVAILABLE: ErrorPhase.RUNNER_LAUNCH,
    ErrorCode.RUNNER_CAPABILITY_MISMATCH: ErrorPhase.RUNNER_LAUNCH,
    ErrorCode.HARNESS_NOT_CONFIGURED: ErrorPhase.HARNESS_SETUP,
    ErrorCode.WORKSPACE_MISSING: ErrorPhase.HARNESS_SETUP,
    ErrorCode.HARNESS_PROTOCOL_VIOLATION: ErrorPhase.TURN,
    ErrorCode.INTERNAL_ERROR: ErrorPhase.UNKNOWN,
}


def phase_for_code(code: str) -> ErrorPhase:
    """Return the likely lifecycle phase for an error code.

    :param code: An :class:`ErrorCode` string value.
    :returns: The mapped phase, or ``UNKNOWN`` for a generic/unrecognized code
        (an active ``phase_scope`` localizes those at log time).
    """
    return _CODE_TO_PHASE.get(code, ErrorPhase.UNKNOWN)


class OmnigentError(Exception):
    """
    Application-level error with a machine-readable code.

    Raise this from routes, stores, or any layer. The global FastAPI
    exception handler converts it to a JSON response automatically.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = ErrorCode.INTERNAL_ERROR,
        category: ErrorCategory | None = None,
        impact: ErrorImpact | None = None,
        phase: ErrorPhase | None = None,
    ) -> None:
        """
        Create a new application error.

        :param message: Human-readable error description.
        :param code: Machine-readable error code from
            :class:`ErrorCode`, e.g. ``ErrorCode.NOT_FOUND``.
        :param category: Fault-attribution override. Defaults to the mapping for
            ``code`` (see :func:`category_for_code`); pass it only where the same
            code has two causes, e.g. a schema-layer ``INVALID_INPUT`` that is a
            client bug rather than a user typo.
        :param impact: Progress-impact override. Defaults to the mapping for
            ``code`` (see :func:`impact_for_code`); pass it where the raise site
            knows the actual outcome, e.g. a normally-benign code that this time
            aborted the turn.
        :param phase: Lifecycle-phase override. Defaults to the mapping for
            ``code`` (see :func:`phase_for_code`); pass it where the raise site
            knows the stage better than the code does.
        """
        super().__init__(message)
        self.code = code
        self.message = message
        self._category_override = category
        self._impact_override = impact
        self._phase_override = phase

    @property
    def http_status(self) -> int:
        """
        Map this error's code to an HTTP status code.

        :returns: HTTP status (e.g. 404 for ``NOT_FOUND``).
            Defaults to 500 for unknown codes.
        """
        return _CODE_TO_HTTP_STATUS.get(self.code, 500)

    @property
    def category(self) -> ErrorCategory:
        """Fault attribution: the constructor override if given, else the code's
        mapping."""
        return self._category_override or category_for_code(self.code)

    @property
    def impact(self) -> ErrorImpact:
        """Progress impact: the constructor override if given, else the code's
        mapping."""
        return self._impact_override or impact_for_code(self.code)

    @property
    def blocking(self) -> bool:
        """True when this error halts the turn/task (``impact`` is BLOCKING)."""
        return self.impact is ErrorImpact.BLOCKING

    @property
    def phase(self) -> ErrorPhase:
        """Lifecycle phase: the constructor override if given, else the code's
        mapping."""
        return self._phase_override or phase_for_code(self.code)


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


# Exception type names (matched across the MRO) treated as a transient upstream
# transport blip. Matched by name so this module imports no httpx / starlette.
_TRANSPORT_EXC_NAMES = frozenset(
    {
        "HTTPError",
        "TransportError",
        "TimeoutException",
        "NetworkError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "PoolTimeout",
        "RemoteProtocolError",
        "WebSocketDisconnect",
    }
)


def classify_exception(exc: BaseException) -> tuple[ErrorCategory, ErrorImpact]:
    """Best-effort (category, impact) for any logged exception.

    The debug-log sink calls this for every record carrying ``exc_info`` that a
    callsite did not already attribute, so exception logs across the whole
    codebase are covered without per-site edits.

    - :class:`OmnigentError` (and its subclasses) keep their own axes.
    - Transport failures (connection/timeout, plus httpx / WebSocket disconnects
      matched by type name) read as a transient upstream blip.
    - Anything else is genuinely unattributed: UNKNOWN on both axes rather than a
      guessed owner. The turn's terminal outcome remains the authoritative
      blocking signal.

    :param exc: The exception to classify.
    :returns: A ``(category, impact)`` pair.
    """
    if isinstance(exc, OmnigentError):
        return exc.category, exc.impact
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return ErrorCategory.UPSTREAM, ErrorImpact.TRANSIENT
    if _TRANSPORT_EXC_NAMES.intersection(klass.__name__ for klass in type(exc).__mro__):
        return ErrorCategory.UPSTREAM, ErrorImpact.TRANSIENT
    return ErrorCategory.UNKNOWN, ErrorImpact.UNKNOWN
