"""Classify harness launch/terminal failures into human-readable diagnoses.

When a harness terminal exits unexpectedly (or a launch/connection step
fails), the raw signal is a terse code plus a tail of PTY output — hard to
act on. This module maps that raw signal to a :class:`FailureDiagnosis`
(``title`` / ``cause`` / ``remediation``) the web UI can render as a clear
failure card, and provides English fallbacks for the failure *codes* that
have no output to pattern-match.

The design goal is one shared classification layer for every harness: the
terminal-exit path is common to all ~20 harnesses, so a matcher added here
covers them all. A new harness quirk becomes one entry in
:data:`_TERMINAL_EXIT_MATCHERS`, never per-harness branching.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from omnigent.cli_invocation import cli_invocation
from omnigent.errors import SESSION_AGENT_MISSING_MESSAGE, ErrorCategory
from omnigent.llms.errors import detect_request_size_overflow

__all__ = [
    "FailureDiagnosis",
    "classify_native_turn_error",
    "classify_terminal_failure",
    "describe_failure_code",
    "diagnose_client_update_required",
    "diagnose_codex_compaction_model_failure",
    "diagnose_codex_model_rejection",
    "diagnose_model_unavailable",
]

# A Codex turn failed while compacting on the thread's previous model, not the selected one.
CODEX_COMPACTION_MODEL_FAILED = "codex_compaction_model_failed"


@dataclass(frozen=True)
class FailureDiagnosis:
    """A human-readable interpretation of a launch/terminal failure.

    :param title: Short headline naming what went wrong, e.g.
        ``"Claude Code can't run as root"``. Renders as the card title.
    :param cause: One or two sentences explaining *why* it failed, in terms
        the user can act on.
    :param remediation: The concrete next step, e.g. a command to run or a
        config to change. ``None`` when there is no single clear fix.
    :param category: Fault attribution stamped on the failure's log row.
    """

    title: str
    cause: str
    remediation: str | None = None
    category: ErrorCategory = ErrorCategory.CONFIG


@dataclass(frozen=True)
class _Signal:
    """The normalized terminal-exit signal a matcher inspects.

    :param command: Launched executable basename, lowercased (``""`` if unknown).
    :param exit_code: Inner process exit code, or ``None`` if unknown.
    :param output: Terminal's last captured output, lowercased (``""`` if none).
    """

    command: str
    exit_code: int | None
    output: str

    def output_contains_any(self, needles: tuple[str, ...]) -> bool:
        return any(n in self.output for n in needles)


@dataclass(frozen=True)
class _TerminalMatcher:
    """One declarative rule mapping a terminal-exit signal to a diagnosis.

    :param name: Short slug for the matcher (test/debug identifier).
    :param predicate: Returns ``True`` when this rule explains the failure,
        given the normalized :class:`_Signal`.
    :param diagnosis: The :class:`FailureDiagnosis` to return on a match.
    """

    name: str
    predicate: Callable[[_Signal], bool]
    diagnosis: FailureDiagnosis


# --- root + --dangerously-skip-permissions -----------------------------------
# Claude Code refuses that flag as root ("cannot be run with root privileges
# for security reasons"). The harness passes the flag for autonomous runs, so a
# root container's agent terminal exits immediately.
_ROOT_MARKERS = ("root privileges", "security reasons", "cannot be run with root")

# --- not authenticated --------------------------------------------------------
_AUTH_MARKERS = (
    "not logged in",
    "please run /login",
    "please run `/login`",
    "invalid api key",
    "authentication_error",
    "401 unauthorized",
    "not authenticated",
)

# --- binary missing -----------------------------------------------------------
_MISSING_MARKERS = (
    "command not found",
    "no such file or directory",
    "not recognized as an internal or external command",
    "executable file not found",
)

# --- CLI rejected its arguments -----------------------------------------------
# Usage errors from the agent CLI or a wrapper around it, e.g. a flag added by a
# host-side launcher that this CLI version (or Omnigent's env) does not allow.
_REJECTED_ARGUMENT_MARKERS = (
    "unknown option",
    "unrecognized option",
    "unexpected argument",
    "is disabled by claude_code_",
    "usage: claude",
    "usage: codex",
)


# Ordered most-specific first: the root case also reads like a permission /
# auth problem, so it must win over the broader rules below it.
_TERMINAL_EXIT_MATCHERS: tuple[_TerminalMatcher, ...] = (
    _TerminalMatcher(
        "root_permission",
        lambda s: "security reasons" in s.output and s.output_contains_any(_ROOT_MARKERS),
        FailureDiagnosis(
            title="Claude Code can't run as root",
            cause=(
                "The agent terminal exited immediately because Claude Code refuses "
                "--dangerously-skip-permissions when running as the root user."
            ),
            remediation="Run the host as a non-root user (uid != 0).",
        ),
    ),
    _TerminalMatcher(
        "missing_binary",
        lambda s: s.exit_code == 127 or s.output_contains_any(_MISSING_MARKERS),
        FailureDiagnosis(
            title="Agent command not found",
            cause=(
                "The host couldn't find the agent's CLI on its PATH, so the terminal "
                "exited before the session could start."
            ),
            remediation=f"Install the harness on the host (e.g. run `{cli_invocation()} setup`).",
        ),
    ),
    _TerminalMatcher(
        "not_authenticated",
        lambda s: s.output_contains_any(_AUTH_MARKERS),
        FailureDiagnosis(
            title="Agent isn't signed in",
            cause=(
                "The agent CLI exited because it has no valid credentials for this "
                "host — it needs to be logged in before it can run a session."
            ),
            remediation=(
                f"Sign the agent in on the host (e.g. run its `/login`, "
                f"or `{cli_invocation()} login`)."
            ),
        ),
    ),
    _TerminalMatcher(
        "rejected_arguments",
        lambda s: s.output_contains_any(_REJECTED_ARGUMENT_MARKERS),
        FailureDiagnosis(
            title="Agent CLI rejected its launch arguments",
            cause=(
                "The agent CLI exited at startup because it did not accept the "
                "arguments it was started with, often from a wrapper script, shell "
                "alias, or extra launch arguments configured on the host."
            ),
            remediation=(
                "Check the agent's launcher and any extra arguments configured on "
                "the host, then retry."
            ),
        ),
    ),
)


def classify_terminal_failure(
    *,
    command: str | None,
    exit_status: int | None,
    output: str | None,
) -> FailureDiagnosis | None:
    """Classify a required-terminal exit into a :class:`FailureDiagnosis`.

    :param command: The launched executable (may be a full path); only the
        basename is matched, case-insensitively.
    :param exit_status: The inner process's exit code, or ``None`` if unknown.
    :param output: The terminal's last captured output, or ``None``.
    :returns: A diagnosis when a matcher recognizes the failure, else ``None``
        (the caller falls back to the generic message).
    """
    signal = _Signal(
        command=(command or "").rsplit("/", 1)[-1].lower(),
        exit_code=exit_status,
        output=(output or "").lower(),
    )
    for matcher in _TERMINAL_EXIT_MATCHERS:
        if matcher.predicate(signal):
            return matcher.diagnosis
    return None


_RATE_LIMIT_ERROR = re.compile(
    r"\b(?:rate[ _-]limit[ _-](?:error|exceeded|reached)|request_limit_exceeded"
    r"|rate[ _-]limited|too many requests)\b",
    re.IGNORECASE,
)
_NATIVE_ERROR_HTTP_STATUS = re.compile(
    r"\b(?:http(?:/\d(?:\.\d)?)?|status(?:[ _]code)?|api error|request rejected)"
    r"\s*[:=(]?\s*(\d{3})\b",
    re.IGNORECASE,
)
# AI-gateway budget / usage-limit markers. The gateway returns HTTP 403 +
# PERMISSION_DENIED for these; they are not auth failures.
_BUDGET_EXHAUSTED_FRAGMENTS = (
    "has reached its limit",
    "rate limit is set to 0",
)
# Mid-stream upstream failures the gateway usually recovers from on its own;
# the runner itself stays healthy, so the turn can be continued by the user.
_TRANSIENT_UPSTREAM_FRAGMENTS = (
    "server error mid-response",
    "connection lost mid-response",
    "overloaded",
)
# 499 is the gateway reporting its own cancelled upstream call, which Claude Code never retries.
_TRANSIENT_UPSTREAM_STATUSES = {"499", "500", "502", "503", "504", "529"}
# The gateway's CANCELLED error envelope, which can arrive without a parseable status.
_UPSTREAM_CANCELLED_ENVELOPE = re.compile(r'"error_code"\s*:\s*"CANCELLED"', re.IGNORECASE)
# Claude Code's refusal when its installed version predates the selected model, e.g.
# "Claude Code 2.1.217 does not support this model; version 2.1.280 or newer is required".
_CLIENT_UPDATE_REQUIRED = re.compile(
    r"(?:Claude Code\s+(?P<installed>\S+)\s+)?does not support this model;"
    r"\s+version\s+(?P<required>\S+)\s+or newer is required",
    re.IGNORECASE,
)


def classify_native_turn_error(code: str, message: str) -> str:
    """Refine a native turn's generic code when its text identifies the cause.

    Recognizes rate limits and transient upstream model-gateway failures so
    the web UI can offer a one-click retry instead of a terminal error, a
    Claude Code too old for the selected model, and a content-length cap
    rejection (a request carrying an oversized transcript, refused by the
    deployment's byte cap before the model sees it). Also corrects
    ``codex_reauth_required`` when the message reveals that the real
    cause is a budget/usage-limit exhaustion (older runners misclassify the
    gateway's 403 as auth; the server fixes it on deploy).

    :param code: Existing error code; specific diagnoses are preserved.
    :param message: Native harness error text, from its status or transcript.
    :returns: The semantic error code, or the existing code if unrecognized.
    """
    if code == CODEX_COMPACTION_MODEL_FAILED:
        # Its budget text names the previous model's limit; the card explains it.
        return code
    lowered = message.lower()
    if any(fragment in lowered for fragment in _BUDGET_EXHAUSTED_FRAGMENTS):
        return "budget_exhausted"
    if code not in {"native_turn_error", "codex_turn_error"}:
        return code
    if _CLIENT_UPDATE_REQUIRED.search(message):
        return "client_update_required"
    if detect_request_size_overflow(message) is not None:
        return "context_length_exceeded"
    status_match = _NATIVE_ERROR_HTTP_STATUS.search(message)
    status = status_match.group(1) if status_match else None
    if status in {"401", "403"}:
        return code
    if status == "429" or _RATE_LIMIT_ERROR.search(message):
        return "rate_limit_exceeded"
    if (
        status in _TRANSIENT_UPSTREAM_STATUSES
        or _UPSTREAM_CANCELLED_ENVELOPE.search(message)
        or any(fragment in lowered for fragment in _TRANSIENT_UPSTREAM_FRAGMENTS)
    ):
        return "transient_upstream_error"
    return code


def diagnose_client_update_required(message: str) -> FailureDiagnosis | None:
    """Explain Claude Code's refusal of a model its installed version predates.

    :param message: Turn error text, e.g. ``"API Error: 400 ... Claude Code
        2.1.217 does not support this model; version 2.1.280 or newer is required"``.
    :returns: A diagnosis naming the installed and required versions and the
        update command, or ``None`` when *message* is not that refusal.
    """
    match = _CLIENT_UPDATE_REQUIRED.search(message)
    if match is None:
        return None
    installed = match.group("installed")
    client = f"Claude Code {installed}" if installed else "Claude Code"
    return FailureDiagnosis(
        title="Claude Code needs an update",
        cause=(
            f"{client} on the host doesn't support this model; "
            f"version {match.group('required')} or newer is required."
        ),
        remediation="Run `claude update` on the host, then start a new session.",
    )


def diagnose_codex_model_rejection(message: str) -> FailureDiagnosis | None:
    """Explain Codex's explicit refusal of a model on a ChatGPT account."""
    if "model is not supported when using codex with a chatgpt account" not in message.lower():
        return None
    return FailureDiagnosis(
        title="Model unavailable with ChatGPT login",
        cause=message.strip(),
        remediation=(
            "Choose a model supported by your ChatGPT account using the model picker or /model. "
            f"To use another provider, configure it for Codex with `{cli_invocation()} setup` "
            "on the host, then start a new session."
        ),
    )


# Provider model-not-found shapes, matched narrowly so unrelated 404s, auth,
# rate-limit, and budget failures keep their own handling.
# Claude Code: "There's an issue with the selected model (<id>). It may not exist..."
_MODEL_UNAVAILABLE_MARKER = "issue with the selected model"
# Anthropic's not-found body for an unknown model id, as a harness forwards it:
# `404 {"type":"error","error":{"type":"not_found_error","message":"model: <id>"}}`.
_ANTHROPIC_MODEL_NOT_FOUND = re.compile(r'"message"\s*:\s*"model:\s*[^"\s]+"', re.IGNORECASE)
_NOT_FOUND_STATUS = re.compile(r"\b404\b|not_found_error", re.IGNORECASE)
# OpenAI's rejection of an unknown or inaccessible model id; the prose form must
# name the model so other "does not exist" resource errors stay unlabeled.
_OPENAI_MODEL_NOT_FOUND = re.compile(
    r'"code"\s*:\s*"model_not_found"'
    r"|\bmodel\b[^\n]{0,120}?does not exist or you do not have access to it",
    re.IGNORECASE,
)


def _is_model_unavailable(message: str) -> bool:
    if _MODEL_UNAVAILABLE_MARKER in message.lower():
        return True
    if _OPENAI_MODEL_NOT_FOUND.search(message):
        return True
    return bool(_NOT_FOUND_STATUS.search(message) and _ANTHROPIC_MODEL_NOT_FOUND.search(message))


def diagnose_model_unavailable(message: str) -> FailureDiagnosis | None:
    """Explain a provider rejecting the selected model, with a recovery hint.

    Applies where the harness forwards the provider's rejection without a next
    step: the in-process SDK path and Pi's forwarded ``Pi model error: 404 …``
    item. Claude Code appends its own ``/model`` hint. Returns ``None`` for
    anything that is not a model-not-found rejection.
    """
    if not _is_model_unavailable(message):
        return None
    return FailureDiagnosis(
        title="Model unavailable",
        cause=message.strip(),
        remediation=(
            "Choose a different model with the model picker or /model, or check that the "
            "provider is configured and your account can access this model."
        ),
    )


def diagnose_codex_compaction_model_failure(
    code: str, *, compaction_model: str, selected_model: str
) -> FailureDiagnosis:
    """Explain a turn that failed while Codex compacted on its previous model.

    After a model switch, Codex compacts the conversation on the previous model
    before the first turn on the new one, and retries that on every later turn.
    A fork rebuilds the conversation from Omnigent's history without it.

    :param code: Classified failure code, e.g. ``"budget_exhausted"``.
    :param compaction_model: Model Codex compacted on, e.g.
        ``"system.ai.gpt-6-astra"``.
    :param selected_model: Model the session selected, e.g.
        ``"system.ai.gpt-6-1-sol"``.
    :returns: The failure diagnosis for the card.
    """
    from omnigent.models.codex_model_vocabulary import bare_model_id

    previous = bare_model_id(compaction_model)
    selected = bare_model_id(selected_model)
    # Each cause is stored as a 256-character label, so it names each model once.
    if code in {"rate_limit_exceeded", "transient_upstream_error"}:
        return FailureDiagnosis(
            title=f"Codex can't compact on {previous} right now",
            cause=(
                f"Codex compacts this conversation on the previous model, {previous}, "
                f"before switching to {selected}, and that model is temporarily unavailable."
            ),
            # These keep their Retry action, so Fork is only in the session menu.
            remediation=(
                f"Retry later, or choose Fork in the session menu to continue on {selected}."
            ),
            category=ErrorCategory.UPSTREAM,
        )
    repeated = (
        f"Codex retries it every turn before switching to {selected}, "
        "so switching models doesn't help."
    )
    fork = (
        f"Fork this session to continue on {selected}. The fork rebuilds the "
        f"conversation from its history, so it doesn't need {previous}."
    )
    if code == "budget_exhausted":
        # Lead with the gateway's refusal: the budget, not Codex, is the root cause.
        return FailureDiagnosis(
            title=f"{previous} is over its AI gateway budget",
            cause=(
                f"The AI gateway refused a compaction request on {previous}: its budget is "
                f"exhausted. {repeated}"
            ),
            remediation=f"{fork} Or ask an admin to raise that budget.",
            category=ErrorCategory.USER,
        )
    return FailureDiagnosis(
        title=f"Codex can't compact on {previous}",
        cause=f"{previous} rejected a compaction request. {repeated}",
        remediation=fork,
        category=ErrorCategory.UPSTREAM,
    )


# --- failure-code English fallbacks -------------------------------------------
# Every server-emitted failure code, mapped to a one-line human sentence, so
# even an unclassified failure reads as English instead of a raw enum. Terminal
# exits that a matcher recognizes use the richer diagnosis above; this table
# is the floor for everything else.
#
# This is the canonical source; the frontend keeps a hand-mirrored copy
# (``FAILURE_CODE_DESCRIPTIONS`` in ``web/src/components/blocks/StatusBlocks.tsx``)
# because the failure card renders client-side. Keep the two in sync when adding
# or editing a code.
_FAILURE_CODE_DESCRIPTIONS: dict[str, str] = {
    "required_terminal_exited": (
        "The agent's terminal exited unexpectedly, so the session can't continue."
    ),
    "terminal_launch_failed": "The agent's terminal couldn't be started on the host.",
    "runner_error": "Something went wrong setting up the turn on the host.",
    "runner_disconnected": "The connection to the host dropped unexpectedly.",
    "runner_unavailable": "The session's runner isn't connected to the server.",
    "connection_error": "The connection to the agent dropped mid-turn.",
    "context_length_exceeded": "The conversation grew past the model's context window.",
    "executor_error": "The agent runtime hit an error while running the turn.",
    "codex_thread_reset": (
        "Codex hit an error reloading the earlier transcript, so it started a fresh thread."
    ),
    "codex_turn_error": "Codex ran into an error during this turn.",
    CODEX_COMPACTION_MODEL_FAILED: (
        "Codex couldn't compact the conversation on the previous model, so this turn didn't run."
    ),
    "databricks_sign_in_pending": "The agent is waiting for a Databricks sign-in.",
    "agent_startup_pending": "The agent is still starting in the session terminal.",
    "databricks_sign_in_completed": "The Databricks sign-in completed and the agent is ready.",
    "codex_thread_not_started": "Codex stopped before it could start, so this turn never ran.",
    "native_turn_error": "The agent ran into an error during this turn.",
    "rate_limit_exceeded": "The model's rate limit was reached. You can retry this turn.",
    "transient_upstream_error": (
        "The model service hit a temporary error mid-response; retrying usually "
        "continues the turn."
    ),
    "budget_exhausted": (
        "The AI gateway refused this turn because a spending budget or usage limit is "
        "exhausted. Contact an admin to raise it, or use a different budget."
    ),
    "client_update_required": (
        "The agent CLI on the host is too old for the selected model. Update it on the "
        "host, then start a new session."
    ),
    "session_agent_missing": SESSION_AGENT_MISSING_MESSAGE,
}


def describe_failure_code(code: str | None) -> str | None:
    """Return a one-line English description for a failure *code*.

    Server-side counterpart of the frontend's ``FAILURE_CODE_DESCRIPTIONS``
    map. The live failure card is rendered client-side, so this function is
    currently a parity mirror exercised by tests; it exists so a server-side
    caller (e.g. a future REPL/CLI failure renderer) has the same code→sentence
    fallback the web UI uses, without re-deriving it.

    :param code: The machine-readable failure code, e.g. ``"runner_error"``.
    :returns: A human sentence, or ``None`` for an unknown/empty code (callers
        fall back to whatever message they already have).
    """
    if not code:
        return None
    return _FAILURE_CODE_DESCRIPTIONS.get(code)
