"""Tests for the harness launch-failure classifier."""

from __future__ import annotations

import pytest

from omnigent.errors import ErrorCategory
from omnigent.runner.launch_failure import (
    FailureDiagnosis,
    classify_native_turn_error,
    classify_terminal_failure,
    describe_failure_code,
    diagnose_client_update_required,
)

# The tail Claude Code prints when refusing --dangerously-skip-permissions as
# root — the exact scenario a root container hits.
_ROOT_REFUSAL_OUTPUT = (
    "--dangerously-skip-permissions cannot be run with root privileges for security reasons"
)

# Claude Code's StopFailure text when the AI gateway cancels its upstream call.
_GATEWAY_499_OUTPUT = 'API Error: 499 {"error_code":"CANCELLED","message":""}'

# Claude Code's StopFailure text when its version predates the selected model.
_CLIENT_UPDATE_OUTPUT = (
    'API Error: 400 {"message":"Claude Code 2.1.217 does not support this model; '
    "version 2.1.280 or newer is required. Run 'claude update', or update the Claude "
    'desktop app, then try again."}'
)


def test_classifies_root_permission_failure() -> None:
    diagnosis = classify_terminal_failure(
        command="claude",
        exit_status=1,
        output=_ROOT_REFUSAL_OUTPUT,
    )
    assert diagnosis is not None
    assert diagnosis.title == "Claude Code can't run as root"
    assert "root" in diagnosis.cause.lower()
    assert diagnosis.remediation is not None
    assert "non-root" in diagnosis.remediation.lower()


def test_root_failure_survives_mid_word_truncation() -> None:
    # The pane snapshot may be clipped to "...for secuRITY REASONS" — the
    # matcher keys on "security reasons", which tail trimming keeps.
    diagnosis = classify_terminal_failure(
        command="claude",
        exit_status=1,
        output="root privileges\nfor security reasons",
    )
    assert diagnosis is not None
    assert diagnosis.title == "Claude Code can't run as root"


@pytest.mark.parametrize(
    ("command", "exit_status", "output"),
    [
        (
            "env",
            0,
            "'--background' is disabled by CLAUDE_CODE_DISABLE_AGENT_VIEW.\n"
            "Claude Code exited with code 1",
        ),
        ("env", 0, "error: unknown option '--codex' (did you mean --model?)"),
        ("codex", 2, "error: unexpected argument '--foo' found\n\nUsage: codex [OPTIONS]"),
    ],
)
def test_classifies_rejected_arguments(command: str, exit_status: int, output: str) -> None:
    diagnosis = classify_terminal_failure(command=command, exit_status=exit_status, output=output)
    assert diagnosis is not None
    assert diagnosis.title == "Agent CLI rejected its launch arguments"
    assert diagnosis.category is ErrorCategory.CONFIG


@pytest.mark.parametrize(
    "output",
    [
        "Not logged in · Please run /login",
        "Error: Invalid API key",
        "authentication_error: 401 Unauthorized",
    ],
)
def test_classifies_auth_failure(output: str) -> None:
    diagnosis = classify_terminal_failure(command="codex", exit_status=1, output=output)
    assert diagnosis is not None
    assert diagnosis.title == "Agent isn't signed in"
    assert diagnosis.remediation is not None


def test_classifies_missing_binary_by_exit_code() -> None:
    diagnosis = classify_terminal_failure(command="qwen", exit_status=127, output="")
    assert diagnosis is not None
    assert diagnosis.title == "Agent command not found"


def test_classifies_missing_binary_by_output() -> None:
    diagnosis = classify_terminal_failure(
        command="qwen",
        exit_status=None,
        output="bash: qwen: command not found",
    )
    assert diagnosis is not None
    assert diagnosis.title == "Agent command not found"


def test_root_wins_over_generic_auth_when_both_markers_present() -> None:
    # Ordering guard: the root case also reads like a permission problem, so it
    # must be matched before any broader rule.
    diagnosis = classify_terminal_failure(
        command="claude",
        exit_status=1,
        output="not logged in\nroot privileges\nfor security reasons",
    )
    assert diagnosis is not None
    assert diagnosis.title == "Claude Code can't run as root"


def test_unclassified_failure_returns_none() -> None:
    assert (
        classify_terminal_failure(
            command="worker-cli",
            exit_status=1,
            output="startup failed\ncomplete setup first",
        )
        is None
    )


def test_none_inputs_do_not_raise() -> None:
    assert classify_terminal_failure(command=None, exit_status=None, output=None) is None


def test_command_path_is_matched_by_basename() -> None:
    # A full path shouldn't defeat the (currently command-agnostic) matchers.
    diagnosis = classify_terminal_failure(
        command="/usr/local/bin/claude",
        exit_status=1,
        output=_ROOT_REFUSAL_OUTPUT,
    )
    assert isinstance(diagnosis, FailureDiagnosis)


@pytest.mark.parametrize("code", ["native_turn_error", "codex_turn_error"])
@pytest.mark.parametrize(
    "message",
    [
        (
            "API Error: Request rejected (429) · REQUEST_LIMIT_EXCEEDED: Exceeded "
            "workspace input tokens per minute rate limit for databricks-test-model. "
            "Work with your Databricks account team to request a higher FMAPI rate limit tier."
        ),
        "API Error: Request rejected (429)",
        'API Error: 429 {"error": {"type": "rate_limit_error"}}',
        "REQUEST_LIMIT_EXCEEDED: request throttled",
        "Rate limit exceeded",
        "rate-limit reached for this model",
        "rate limited",
        "Rate limited",
        "rate-limited",
        "rate_limited",
        "HTTP 429",
        "HTTP/1.1 429",
        "status_code: 429",
        "Too Many Requests",
        'API Error: 429 {"error": {"code": "insufficient_quota"}}',
        "HTTP 429: billing_hard_limit_reached",
        "API Error: 429: Your credit balance is too low to access the API.",
    ],
)
def test_classifies_native_429_and_rate_limit_errors(code: str, message: str) -> None:
    assert classify_native_turn_error(code, message) == "rate_limit_exceeded"


@pytest.mark.parametrize(
    "message",
    [
        "An unexpected error occurred",
        "API Error: Request rejected (401) · UNAUTHENTICATED",
        "API Error: Request rejected (403) · PERMISSION_DENIED",
        (
            "API Error: Request rejected (403) · PERMISSION_DENIED: "
            "See rate_limit_error troubleshooting"
        ),
        "API Error: 401 Unauthorized. Previous request: rate limit exceeded.",
        "HTTP/1.1 403: See rate_limit_exceeded troubleshooting",
        "There's an issue with the selected model. It may not exist.",
        "You've hit your usage limit.",
        "Error loading model-429",
        "Request rejected (4290)",
    ],
)
def test_preserves_other_native_turn_errors(message: str) -> None:
    assert classify_native_turn_error("native_turn_error", message) == "native_turn_error"


@pytest.mark.parametrize("code", ["workspace_missing", "invalid_input"])
def test_rate_limit_text_does_not_override_specific_failure_codes(code: str) -> None:
    assert classify_native_turn_error(code, "HTTP 429: rate limit exceeded") == code


_CONTENT_LENGTH_REJECTION = (
    '{"error_code":"BAD_REQUEST","message":"Server received a request which '
    "exceeds maximum allowed content length. RequestSize(bytes): 33967957, "
    'Limit(bytes): 33554432"}'
)


@pytest.mark.parametrize("code", ["native_turn_error", "codex_turn_error"])
@pytest.mark.parametrize(
    "message",
    [
        _CONTENT_LENGTH_REJECTION,
        (
            "Server received a request which exceeds maximum allowed content "
            "length. RequestSize(bytes): 33967957, Limit(bytes): 33554432"
        ),
    ],
)
def test_classifies_content_length_cap_rejection_as_context_overflow(
    code: str, message: str
) -> None:
    assert classify_native_turn_error(code, message) == "context_length_exceeded"


def test_content_length_text_without_sizes_stays_generic() -> None:
    message = "Server received a request which exceeds maximum allowed content length."
    assert classify_native_turn_error("native_turn_error", message) == "native_turn_error"


@pytest.mark.parametrize("code", ["codex_reauth_required", "workspace_missing"])
def test_content_length_text_does_not_override_specific_failure_codes(code: str) -> None:
    assert classify_native_turn_error(code, _CONTENT_LENGTH_REJECTION) == code


@pytest.mark.parametrize(
    "code",
    # Budget detection runs before the early-return guard, so it applies to
    # codex_reauth_required (old runners) as well as the two generic codes.
    ["native_turn_error", "codex_turn_error", "codex_reauth_required"],
)
@pytest.mark.parametrize(
    "message",
    [
        # Realistic AI-gateway budget exhaustion message (budget name/id synthetic).
        (
            'unexpected status 403 Forbidden: {"error_code":"PERMISSION_DENIED","message":'
            '"Budget \\"test-budget\\" (00000000-0000-0000-0000-000000000001) has reached'
            " its limit of $100. To continue, contact an admin to increase the budget or"
            ' use a different budget."}'
        ),
        # Realistic budget message with the old re-auth hint appended by older runners.
        (
            'unexpected status 403 Forbidden: {"error_code":"PERMISSION_DENIED","message":'
            '"Budget \\"test-budget\\" (00000000-0000-0000-0000-000000000001) has reached'
            " its limit of $100. To continue, contact an admin to increase the budget or"
            ' use a different budget."}\n\n'
            "If this looks like an auth issue, running `codex login` may help."
        ),
        # Minimal form — just the key phrase.
        "Budget X has reached its limit of $0.",
        # Disabled per-user rate limit (rate limit is set to 0).
        (
            'unexpected status 403 Forbidden: {"error_code":"PERMISSION_DENIED",'
            '"message":"rate limit is set to 0 for user test@example.com"}'
        ),
    ],
)
def test_classifies_budget_exhausted(code: str, message: str) -> None:
    assert classify_native_turn_error(code, message) == "budget_exhausted"


def test_genuine_reauth_codex_reauth_required_is_preserved() -> None:
    """A real auth failure under codex_reauth_required must not be reclassified."""
    message = (
        "401 Unauthorized: your login has expired.\n\n"
        "If this looks like an auth issue, running `codex login` may help."
    )
    assert classify_native_turn_error("codex_reauth_required", message) == "codex_reauth_required"


@pytest.mark.parametrize("code", ["native_turn_error", "codex_turn_error"])
@pytest.mark.parametrize(
    "message",
    [
        "API Error: Server error mid-response. The response above may be incomplete.",
        "Connection lost mid-response. The response above may be incomplete.",
        "connection lost mid-response",
        "API Error: 503 Service Unavailable",
        "HTTP 500 Internal Server Error",
        "HTTP/1.1 502 Bad Gateway",
        "status_code: 504 Gateway Timeout",
        "API Error: 529",
        "The model is overloaded. Please retry your request.",
        # The gateway's cancelled upstream call: Claude Code retries 503s, never 499.
        _GATEWAY_499_OUTPUT,
        "API Error: 499",
        "API Error: Request rejected (499) · CANCELLED",
        'unexpected status 499 Client Closed Request: {"error_code": "CANCELLED","message":""}',
        '{"error_code":"CANCELLED","message":""}',
    ],
)
def test_classifies_transient_upstream_errors(code: str, message: str) -> None:
    assert classify_native_turn_error(code, message) == "transient_upstream_error"


@pytest.mark.parametrize("code", ["native_turn_error", "codex_turn_error"])
@pytest.mark.parametrize(
    "message",
    [
        # User interrupts and client-side aborts are not gateway failures.
        "API Error: Request was aborted.",
        "Request was aborted.",
        "[Request interrupted by user]",
        "[Request interrupted by user for tool use]",
        "The operation was aborted",
        "Turn cancelled by the user",
        "Interrupted by user",
        # Only a 3-digit 499 status or the CANCELLED envelope counts.
        "Request rejected (4990)",
        "API Error: 4991",
        'API Error: 400 {"error_code":"INVALID_PARAMETER_VALUE","message":"query cancelled"}',
    ],
)
def test_interrupts_and_aborts_are_not_transient(code: str, message: str) -> None:
    assert classify_native_turn_error(code, message) == code


@pytest.mark.parametrize("code", ["workspace_missing", "codex_reauth_required"])
def test_gateway_499_does_not_override_specific_failure_codes(code: str) -> None:
    assert classify_native_turn_error(code, _GATEWAY_499_OUTPUT) == code


@pytest.mark.parametrize(
    "message",
    [
        # Deterministic failures must keep the generic, non-retryable code.
        "prompt is too long: 250000 tokens > 200000 maximum",
        "API Error: 400 · context_length_exceeded: maximum context window reached",
        "There's an issue with the selected model. It may not exist.",
        "API Error: Request rejected (404) · model not found",
        "API Error: Request rejected (401) · UNAUTHENTICATED",
        "API Error: Request rejected (403) · PERMISSION_DENIED",
    ],
)
def test_deterministic_native_errors_are_not_transient(message: str) -> None:
    assert classify_native_turn_error("native_turn_error", message) == "native_turn_error"


def test_429_still_classifies_as_rate_limit_not_transient() -> None:
    assert (
        classify_native_turn_error("native_turn_error", "API Error: 429") == "rate_limit_exceeded"
    )


@pytest.mark.parametrize("code", ["native_turn_error", "codex_turn_error"])
@pytest.mark.parametrize(
    "message",
    [
        _CLIENT_UPDATE_OUTPUT,
        "inner executor error: " + _CLIENT_UPDATE_OUTPUT,
        "Claude Code 2.1.263 does not support this model; version 2.1.280 or newer is required",
        "does not support this model; version 3.0.0 or newer is required",
    ],
)
def test_classifies_client_update_required(code: str, message: str) -> None:
    assert classify_native_turn_error(code, message) == "client_update_required"


@pytest.mark.parametrize(
    "message",
    [
        "API Error: 400 INVALID_PARAMETER_VALUE: This model does not support image input.",
        "Claude Code 2.1.217 does not support this feature; version 2.1.280 or newer is required",
        "There's an issue with the selected model. It may not exist.",
        "Node.js 22.13.0 or newer is required",
    ],
)
def test_other_unsupported_model_errors_are_not_client_update(message: str) -> None:
    assert classify_native_turn_error("native_turn_error", message) == "native_turn_error"
    assert diagnose_client_update_required(message) is None


@pytest.mark.parametrize("code", ["workspace_missing", "codex_reauth_required"])
def test_client_update_text_does_not_override_specific_failure_codes(code: str) -> None:
    assert classify_native_turn_error(code, _CLIENT_UPDATE_OUTPUT) == code


def test_client_update_diagnosis_names_both_versions_and_the_fix() -> None:
    diagnosis = diagnose_client_update_required(_CLIENT_UPDATE_OUTPUT)

    assert diagnosis is not None
    assert diagnosis.title == "Claude Code needs an update"
    assert "2.1.217" in diagnosis.cause
    assert "version 2.1.280 or newer" in diagnosis.cause
    assert diagnosis.remediation is not None
    assert "`claude update`" in diagnosis.remediation
    assert "on the host" in diagnosis.remediation
    assert diagnosis.category is ErrorCategory.CONFIG


def test_client_update_diagnosis_without_installed_version_still_names_required() -> None:
    diagnosis = diagnose_client_update_required(
        "does not support this model; version 3.0.0 or newer is required"
    )

    assert diagnosis is not None
    assert diagnosis.cause == (
        "Claude Code on the host doesn't support this model; version 3.0.0 or newer is required."
    )


def test_client_update_diagnosis_ignores_unrelated_errors() -> None:
    assert diagnose_client_update_required(_GATEWAY_499_OUTPUT) is None
    assert diagnose_client_update_required("") is None


@pytest.mark.parametrize(
    ("code", "expected_substring"),
    [
        ("required_terminal_exited", "terminal exited"),
        ("terminal_launch_failed", "couldn't be started"),
        ("runner_error", "setting up the turn"),
        ("runner_disconnected", "host dropped"),
        ("connection_error", "connection"),
        ("context_length_exceeded", "context window"),
        ("rate_limit_exceeded", "You can retry this turn"),
        ("transient_upstream_error", "temporary error"),
        ("client_update_required", "too old for the selected model"),
        ("budget_exhausted", "budget"),
        ("databricks_sign_in_pending", "Databricks sign-in"),
        ("agent_startup_pending", "still starting"),
        ("codex_thread_not_started", "never ran"),
    ],
)
def test_describe_failure_code_known(code: str, expected_substring: str) -> None:
    description = describe_failure_code(code)
    assert description is not None
    assert expected_substring in description


@pytest.mark.parametrize("code", [None, "", "some_unknown_code"])
def test_describe_failure_code_unknown(code: str | None) -> None:
    assert describe_failure_code(code) is None
