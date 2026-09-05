"""Tests for the centralized error code / HTTP status mapping."""

from __future__ import annotations

import pytest

from omnigent.errors import (
    _CODE_TO_CATEGORY,
    _CODE_TO_HTTP_STATUS,
    _CODE_TO_IMPACT,
    _CODE_TO_PHASE,
    ErrorCategory,
    ErrorCode,
    ErrorImpact,
    ErrorPhase,
    OmnigentError,
    category_for_code,
    classify_exception,
    impact_for_code,
    is_before_harness_start,
    phase_for_code,
)


def _all_error_code_values() -> list[str]:
    """Every string constant declared on :class:`ErrorCode`."""
    return [
        value
        for name, value in vars(ErrorCode).items()
        if not name.startswith("_") and isinstance(value, str)
    ]


def test_harness_protocol_violation_string_value() -> None:
    """The error code's string value is what appears in JSON responses.

    Clients dispatch on this string; renaming it is a wire-protocol
    change. If this assertion flips, every external consumer that
    branches on ``error.code == "harness_protocol_violation"`` breaks.
    """
    assert ErrorCode.HARNESS_PROTOCOL_VIOLATION == "harness_protocol_violation"


def test_harness_protocol_violation_maps_to_500() -> None:
    """Harness protocol violations are server-side bugs in the harness wrap.

    They surface as HTTP 500 (no client action can fix them — the harness
    implementation needs investigation). If this drifts to 4xx, callers
    might mistakenly retry or attempt user-side remediation.
    """
    assert _CODE_TO_HTTP_STATUS[ErrorCode.HARNESS_PROTOCOL_VIOLATION] == 500


def test_omnigent_error_with_harness_violation_code_returns_500() -> None:
    """End-to-end: OmnigentError(code=HARNESS_PROTOCOL_VIOLATION).http_status == 500.

    Exercises the public API path that FastAPI's exception handler uses
    to map an error to an HTTP status. If this fails, harness protocol
    violations would surface to clients as 500-with-default rather than
    500-with-the-right-code, masking the bug class.
    """
    err = OmnigentError(
        "harness emitted response.completed with outstanding elicitations",
        code=ErrorCode.HARNESS_PROTOCOL_VIOLATION,
    )
    assert err.http_status == 500
    assert err.code == ErrorCode.HARNESS_PROTOCOL_VIOLATION
    assert "outstanding elicitations" in err.message


@pytest.mark.parametrize(
    "code,expected_status",
    [
        (ErrorCode.NOT_FOUND, 404),
        (ErrorCode.INVALID_INPUT, 400),
        (ErrorCode.ALREADY_EXISTS, 409),
        (ErrorCode.CONFLICT, 409),
        (ErrorCode.INTERNAL_ERROR, 500),
        (ErrorCode.HARNESS_PROTOCOL_VIOLATION, 500),
    ],
)
def test_all_error_codes_have_http_status_mapping(code: str, expected_status: int) -> None:
    """Every public ErrorCode value MUST appear in the mapping.

    A code without a mapping silently defaults to 500 in
    OmnigentError.http_status — not wrong, but it hides drift.
    This parametrized test makes adding a new ErrorCode without
    updating the mapping a noisy failure rather than a silent
    default.
    """
    assert _CODE_TO_HTTP_STATUS[code] == expected_status


def test_every_error_code_has_a_concrete_category() -> None:
    """Anti-rot: a named ErrorCode may never be UNKNOWN or unmapped.

    UNKNOWN is reserved for sites with no error code (the catch-all handler, an
    uncoded frame failure). If a new ErrorCode is added without a
    ``_CODE_TO_CATEGORY`` entry, this fails rather than letting it default into
    the honesty-margin bucket and silently skew the fault-share numbers.
    """
    for code in _all_error_code_values():
        assert code in _CODE_TO_CATEGORY, f"{code!r} missing from _CODE_TO_CATEGORY"
        assert _CODE_TO_CATEGORY[code] is not ErrorCategory.UNKNOWN, (
            f"{code!r} maps to UNKNOWN; a named code must attribute a concrete owner"
        )


@pytest.mark.parametrize(
    "code,expected_category",
    [
        (ErrorCode.INTERNAL_ERROR, ErrorCategory.SERVER),
        (ErrorCode.HARNESS_PROTOCOL_VIOLATION, ErrorCategory.SERVER),
        (ErrorCode.WRONG_REPLICA, ErrorCategory.SERVER),
        (ErrorCode.RUNNER_UNAVAILABLE, ErrorCategory.CONFIG),
        (ErrorCode.HARNESS_NOT_CONFIGURED, ErrorCategory.CONFIG),
        (ErrorCode.RUNNER_CAPABILITY_MISMATCH, ErrorCategory.CONFIG),
        (ErrorCode.NOT_FOUND, ErrorCategory.USER),
        (ErrorCode.INVALID_INPUT, ErrorCategory.USER),
        (ErrorCode.UNAUTHORIZED, ErrorCategory.USER),
        (ErrorCode.WORKSPACE_MISSING, ErrorCategory.USER),
    ],
)
def test_code_category_mapping(code: str, expected_category: ErrorCategory) -> None:
    """Pin the intent of the attribution for the codes that drive dashboards."""
    assert category_for_code(code) == expected_category


def test_omnigent_error_category_defaults_from_code() -> None:
    """An OmnigentError inherits its code's category when none is passed."""
    assert OmnigentError("boom").category is ErrorCategory.SERVER  # default code
    assert OmnigentError("nope", code=ErrorCode.NOT_FOUND).category is ErrorCategory.USER


def test_omnigent_error_category_override() -> None:
    """The override wins, for a code whose cause is context-dependent.

    A schema-layer INVALID_INPUT is a client bug, not the user's typo.
    """
    err = OmnigentError(
        "bad request body",
        code=ErrorCode.INVALID_INPUT,
        category=ErrorCategory.CLIENT,
    )
    assert err.category is ErrorCategory.CLIENT
    # The code's default is still USER when not overridden.
    assert category_for_code(ErrorCode.INVALID_INPUT) is ErrorCategory.USER


def test_category_for_unknown_code_is_unknown() -> None:
    """A code outside the ErrorCode namespace attributes to UNKNOWN."""
    assert category_for_code("not_a_real_code") is ErrorCategory.UNKNOWN


def test_every_error_code_has_an_impact() -> None:
    """Every named ErrorCode must declare a progress impact.

    Adding a code without an ``_CODE_TO_IMPACT`` entry fails here rather than
    silently defaulting; the value must be a real :class:`ErrorImpact`.
    """
    for code in _all_error_code_values():
        assert code in _CODE_TO_IMPACT, f"{code!r} missing from _CODE_TO_IMPACT"
        assert isinstance(_CODE_TO_IMPACT[code], ErrorImpact)
        # UNKNOWN impact is only for arbitrary exceptions with no code.
        assert _CODE_TO_IMPACT[code] is not ErrorImpact.UNKNOWN, (
            f"{code!r} maps to UNKNOWN impact; a named code must declare a concrete one"
        )


@pytest.mark.parametrize(
    "code,expected_impact",
    [
        # Blocking: the turn/task cannot proceed without intervention.
        (ErrorCode.INTERNAL_ERROR, ErrorImpact.BLOCKING),
        (ErrorCode.HARNESS_NOT_CONFIGURED, ErrorImpact.BLOCKING),
        (ErrorCode.WORKSPACE_MISSING, ErrorImpact.BLOCKING),
        (ErrorCode.UNAUTHORIZED, ErrorImpact.BLOCKING),
        # Transient: self-healing, no lost progress.
        (ErrorCode.RUNNER_UNAVAILABLE, ErrorImpact.TRANSIENT),
        (ErrorCode.WRONG_REPLICA, ErrorImpact.TRANSIENT),
        # Benign: a rejected request that leaves the session healthy.
        (ErrorCode.NOT_FOUND, ErrorImpact.BENIGN),
        (ErrorCode.INVALID_INPUT, ErrorImpact.BENIGN),
        (ErrorCode.FORBIDDEN, ErrorImpact.BENIGN),
    ],
)
def test_code_impact_mapping(code: str, expected_impact: ErrorImpact) -> None:
    """Pin the intent: which codes actually block progress, which self-heal."""
    assert impact_for_code(code) == expected_impact


def test_omnigent_error_impact_and_blocking_flag() -> None:
    """``impact`` defaults from the code; ``blocking`` is the simple boolean."""
    internal = OmnigentError("boom")  # default code INTERNAL_ERROR
    assert internal.impact is ErrorImpact.BLOCKING
    assert internal.blocking is True

    unavailable = OmnigentError("asleep", code=ErrorCode.RUNNER_UNAVAILABLE)
    assert unavailable.impact is ErrorImpact.TRANSIENT
    assert unavailable.blocking is False


def test_omnigent_error_impact_override() -> None:
    """A raise site that knows the real outcome can override the code default.

    A normally-benign invalid_input that aborted the turn is blocking.
    """
    err = OmnigentError(
        "aborted mid-turn",
        code=ErrorCode.INVALID_INPUT,
        impact=ErrorImpact.BLOCKING,
    )
    assert err.impact is ErrorImpact.BLOCKING
    assert err.blocking is True


def test_classify_exception_omnigent_error_passthrough() -> None:
    """An OmnigentError classifies to its own axes."""
    err = OmnigentError("gone", code=ErrorCode.HARNESS_NOT_CONFIGURED)
    assert classify_exception(err) == (ErrorCategory.CONFIG, ErrorImpact.BLOCKING)


def test_classify_exception_transport_is_transient_upstream() -> None:
    """Stdlib transport failures read as a transient upstream blip.

    ``ConnectionError`` / ``TimeoutError`` are caught directly; httpx / starlette
    types are matched by name (see :data:`omnigent.errors._TRANSPORT_EXC_NAMES`)
    without importing those packages, exercised in the debug-logging tests.
    """
    assert classify_exception(ConnectionError("reset")) == (
        ErrorCategory.UPSTREAM,
        ErrorImpact.TRANSIENT,
    )
    assert classify_exception(TimeoutError("slow")) == (
        ErrorCategory.UPSTREAM,
        ErrorImpact.TRANSIENT,
    )


def test_classify_exception_arbitrary_is_unknown() -> None:
    """An unattributable exception is UNKNOWN on both axes, not a guessed owner."""
    assert classify_exception(ValueError("nope")) == (
        ErrorCategory.UNKNOWN,
        ErrorImpact.UNKNOWN,
    )


def test_every_error_code_has_a_phase() -> None:
    """Every named ErrorCode declares a lifecycle phase (UNKNOWN allowed).

    Unlike category/impact, UNKNOWN is a valid phase for a generic code
    (internal_error) because the phase is context-driven, not code-driven.
    """
    for code in _all_error_code_values():
        assert code in _CODE_TO_PHASE, f"{code!r} missing from _CODE_TO_PHASE"
        assert isinstance(_CODE_TO_PHASE[code], ErrorPhase)


@pytest.mark.parametrize(
    "code,expected_phase,before_start",
    [
        (ErrorCode.INVALID_INPUT, ErrorPhase.REQUEST, True),
        (ErrorCode.UNAUTHORIZED, ErrorPhase.REQUEST, True),
        (ErrorCode.WRONG_REPLICA, ErrorPhase.ROUTING, True),
        (ErrorCode.RUNNER_UNAVAILABLE, ErrorPhase.RUNNER_LAUNCH, True),
        (ErrorCode.HARNESS_NOT_CONFIGURED, ErrorPhase.HARNESS_SETUP, True),
        (ErrorCode.WORKSPACE_MISSING, ErrorPhase.HARNESS_SETUP, True),
        (ErrorCode.HARNESS_PROTOCOL_VIOLATION, ErrorPhase.TURN, False),
        (ErrorCode.INTERNAL_ERROR, ErrorPhase.UNKNOWN, False),
    ],
)
def test_code_phase_and_harness_boundary(
    code: str, expected_phase: ErrorPhase, before_start: bool
) -> None:
    """Pin the phase per code and the before/after-harness-start split."""
    assert phase_for_code(code) == expected_phase
    assert is_before_harness_start(expected_phase) is before_start


def test_omnigent_error_phase_default_and_override() -> None:
    """`phase` follows the code by default; a raise-site override wins."""
    assert (
        OmnigentError("no harness", code=ErrorCode.HARNESS_NOT_CONFIGURED).phase
        is ErrorPhase.HARNESS_SETUP
    )
    assert (
        OmnigentError("boom", code=ErrorCode.INTERNAL_ERROR, phase=ErrorPhase.TURN).phase
        is ErrorPhase.TURN
    )


def test_harness_boundary_is_startup_not_before() -> None:
    """The harness *start* itself (HARNESS_STARTUP) is not 'before start'."""
    assert is_before_harness_start(ErrorPhase.HARNESS_SETUP) is True
    assert is_before_harness_start(ErrorPhase.HARNESS_STARTUP) is False
    assert is_before_harness_start(ErrorPhase.TURN) is False
    assert is_before_harness_start(ErrorPhase.UNKNOWN) is False
