"""Unit tests for native-terminal-start error classification.

The runner builds every native-terminal-start failure payload through
``_native_terminal_start_error_payload``. A deleted/rebound session agent is a
session-lifecycle condition, not a terminal-startup defect: it must surface a
distinct ``session_agent_missing`` code with a client-safe remedy message,
while unclassified causes keep the generic ``native_terminal_start_failed``
startup-defect code. These are fast, layer-local guards for that split.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from omnigent.db.db_models import LABEL_VALUE_MAX_LEN
from omnigent.debug_logging import record_to_row
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harnesses.codex_native.launch_args import CodexTransportArgsError
from omnigent.inner.terminal import TerminalInstance, TmuxUnavailableError
from omnigent.runner.native import orchestration
from omnigent.runner.native.orchestration import (
    _NATIVE_TERMINAL_START_FAILED_CODE,
    _native_terminal_start_error_payload,
    _native_terminal_start_error_response,
    _publish_native_terminal_start_error,
)
from omnigent.server.routes._sessions.helpers import (
    _LAST_TASK_ERROR_CAUSE_LABEL_KEY,
    _LAST_TASK_ERROR_CODE_LABEL_KEY,
    _LAST_TASK_ERROR_MESSAGE_LABEL_KEY,
    _LAST_TASK_ERROR_REMEDIATION_LABEL_KEY,
    _LAST_TASK_ERROR_TITLE_LABEL_KEY,
    _last_task_error_from_labels,
    _truncate_label,
)
from omnigent.terminals.registry import TerminalExitedDuringLaunch

_ERROR_ID_RE = re.compile(r" Error ID: (err_[0-9a-f]{32})\.$")


def test_missing_session_agent_classified_as_lifecycle_condition() -> None:
    """A ``SESSION_AGENT_MISSING`` cause yields the distinct lifecycle code.

    The payload must carry ``session_agent_missing`` (not the generic
    startup-defect code) and a client-safe message that names the remedy,
    does not relabel the condition as a terminal-startup failure, and never
    leaks the internal spec-resolver text.
    """
    exc = OmnigentError(
        "session spec resolver: agent 'abc123' for session 'conv_1' was not found",
        code=ErrorCode.SESSION_AGENT_MISSING,
    )

    payload = _native_terminal_start_error_payload(exc, "Claude", session_id="conv_1")

    assert payload["code"] == ErrorCode.SESSION_AGENT_MISSING
    message = payload["message"]
    # Actionable, client-safe wording about the lifecycle condition.
    assert "agent no longer exists" in message
    # Must NOT relabel the lifecycle event as a generic startup defect.
    assert "Native Claude terminal failed to start" not in message
    # Must NOT leak the internal resolver detail or the raw agent id.
    assert "session spec resolver" not in message
    assert "abc123" not in message
    # Correlation id preserved so operators can cross-reference the log.
    match = _ERROR_ID_RE.search(message)
    assert match is not None, message
    assert payload["error_id"] == match.group(1)


@pytest.mark.parametrize(
    "cause",
    [
        RuntimeError("tmux server exited before the pane was ready"),
        FileNotFoundError("missing executable"),
    ],
)
def test_other_causes_keep_generic_startup_failure_code(cause: Exception) -> None:
    """A non-lifecycle cause keeps the generic startup-defect code.

    The reclassification is scoped to the missing-agent lifecycle condition;
    an unrelated startup exception must still be attributed as a
    ``native_terminal_start_failed`` terminal-startup defect.
    """
    payload = _native_terminal_start_error_payload(
        cause,
        "Claude",
        session_id="conv_1",
    )

    assert payload["code"] == _NATIVE_TERMINAL_START_FAILED_CODE
    assert payload["code"] == "native_terminal_start_failed"
    assert "agent no longer exists" not in payload["message"]


@pytest.mark.parametrize(
    ("wrapper_command", "cli"),
    [
        (None, "omnigent"),
        ("isaac omni", "isaac omni"),
    ],
)
def test_mixed_version_import_failure_guides_host_restart(
    monkeypatch: pytest.MonkeyPatch, wrapper_command: str | None, cli: str
) -> None:
    """A stale in-tree import failure is classified as a host-restart card.

    After an in-place upgrade the running host imports an upgraded module whose
    new symbol its in-memory copy lacks. The card must read as a recognized
    failure (title + cause + remediation), name both the foreground host and
    the host user-service restart in the configured CLI spelling, each with
    its original options, and never echo the on-disk install path. Each field
    must fit the per-field conversation-label budget, or a reloaded card
    silently truncates the guidance away.
    """
    if wrapper_command is None:
        monkeypatch.delenv("OMNIGENT_WRAPPER_COMMAND", raising=False)
    else:
        monkeypatch.setenv("OMNIGENT_WRAPPER_COMMAND", wrapper_command)

    exc = _stale_host_import_error()
    frames_path = exc.path
    assert frames_path is not None

    payload = _native_terminal_start_error_payload(exc, "Claude", session_id="conv_1")

    assert payload["code"] == _NATIVE_TERMINAL_START_FAILED_CODE
    # Classified: a real headline instead of "Something went wrong", with the
    # diagnosis and the fix split into their own fields.
    assert payload["title"] == "Claude can't start until you restart the host"
    cause = payload["cause"]
    remediation = payload["remediation"]
    assert "in-place upgrade" in cause
    assert "mixed-version" in cause
    assert "Restart the host" in remediation
    # Both recipes must carry the original options: a bare `host` re-run falls
    # back to the configured or a local server, and the bare disable+enable
    # deletes the stored service definition, dropping a remote `--server`.
    assert f"re-run `{cli} host` with the options you started it with" in remediation
    assert "re-enable it with the options you installed it with" in remediation
    assert "keeping `--server`" in remediation
    assert "disable &&" not in remediation
    # The web card hides `message` once a cause is set, so the log-correlation
    # ID must ride in a displayed field to stay quotable for support.
    assert payload["error_id"] in cause
    # The reload card is rebuilt from per-field labels, so each field must fit
    # the label column or the guidance truncates away on refresh.
    assert len(cause) <= LABEL_VALUE_MAX_LEN
    assert len(remediation) <= LABEL_VALUE_MAX_LEN
    # Never leak the on-disk install path, nor fall back to the log pointer.
    for text in (payload["title"], cause, remediation, payload["message"]):
        assert frames_path not in text
        assert "see the runner log" not in text
    # The ID is well-formed and sits at the end of the displayed cause field.
    assert _ERROR_ID_RE.search(cause) is not None
    # The labelless `message` fallback ends with the ID like every other branch
    # of the payload builder, so HTTP consumers keep one correlation convention.
    assert _ERROR_ID_RE.search(payload["message"]) is not None


def _stale_host_import_error() -> ImportError:
    """Build the mixed-version in-tree lazy-import failure from the bug report."""
    frames_path = (
        "/opt/pipx/venvs/omnigent/lib/python3.12/site-packages/"
        "omnigent/runner/transports/ws_tunnel/frames.py"
    )
    exc = ImportError(
        "cannot import name 'EventAckFrame' from "
        "'omnigent.runner.transports.ws_tunnel.frames' "
        f"({frames_path})",
        name="omnigent.runner.transports.ws_tunnel.frames",
        path=frames_path,
    )
    exc.name_from = "EventAckFrame"
    return exc


def test_long_wrapper_command_keeps_restart_guidance_across_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long wrapper command must not truncate the reload-visible guidance.

    ``cli_invocation()`` echoes ``OMNIGENT_WRAPPER_COMMAND`` verbatim, so an
    unusually long wrapper would push the remediation past the per-field label
    budget and silently clip ``--server`` on reload. The guidance falls back
    to a wrapper-independent phrasing, and the complete remedy survives the
    actual label persistence/read-back boundary intact.
    """
    monkeypatch.setenv(
        "OMNIGENT_WRAPPER_COMMAND",
        "/home/alice/.local/share/acme-enterprise/omnigent-wrapper/current/bin/isaac-omnigent",
    )

    payload = _native_terminal_start_error_payload(
        _stale_host_import_error(), "Claude", session_id="conv_1"
    )

    # Persist exactly as the status relay does, then project back for a reload.
    labels = {
        _LAST_TASK_ERROR_CODE_LABEL_KEY: _truncate_label(payload["code"]),
        _LAST_TASK_ERROR_MESSAGE_LABEL_KEY: _truncate_label(payload["message"]),
        _LAST_TASK_ERROR_TITLE_LABEL_KEY: _truncate_label(payload["title"]),
        _LAST_TASK_ERROR_CAUSE_LABEL_KEY: _truncate_label(payload["cause"]),
        _LAST_TASK_ERROR_REMEDIATION_LABEL_KEY: _truncate_label(payload["remediation"]),
    }
    reloaded = _last_task_error_from_labels(labels)

    assert reloaded is not None
    # The complete remedy survives the reload with no truncation marker, keeps
    # the endpoint-preserving instruction, and never embeds the long wrapper.
    assert reloaded["remediation"] == payload["remediation"]
    assert "\u2026" not in reloaded["remediation"]
    assert "keeping `--server`" in reloaded["remediation"]
    assert "isaac-omnigent" not in reloaded["remediation"]
    assert (
        "re-run your host command with the options you started it with" in reloaded["remediation"]
    )
    assert reloaded["title"] == payload["title"]
    assert reloaded["cause"] == payload["cause"]
    # The ID is carried in the cause, so it must survive the reload too.
    assert payload["error_id"] in reloaded["cause"]


def test_stale_host_classified_before_fragile_lazy_imports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mixed-version host is classified before the fragile lazy imports.

    The harness/registry imports in the payload builder can themselves raise
    the same stale-host ``ImportError``; classifying first keeps the restart
    guidance from being lost to a second import failure.
    """
    # A broken replacement makes ``from ... import PiNativeCliOutdatedError``
    # raise, so a stale-host failure must return before reaching that import.
    broken = types.ModuleType("omnigent.harnesses.pi_native.main")
    monkeypatch.setitem(sys.modules, "omnigent.harnesses.pi_native.main", broken)

    payload = _native_terminal_start_error_payload(
        _stale_host_import_error(), "Claude", session_id="conv_1"
    )

    assert payload["title"] == "Claude can't start until you restart the host"
    assert "Restart the host" in payload["remediation"]


def test_stale_host_guidance_survives_label_limit_import_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restart guidance still goes out if the label-limit import fails too.

    The branch loads ``LABEL_VALUE_MAX_LEN`` lazily from the ORM module, which
    is not in the runner's startup import graph. On a genuinely stale host that
    import can raise the same mixed-version ``ImportError``; the branch must
    fall back to the known schema limit so this exact failure mode still gets
    its classified restart card instead of crashing the payload builder.
    """
    # A ``db_models`` stand-in without the constant makes the lazy
    # ``from ... import LABEL_VALUE_MAX_LEN`` raise ImportError, as it would on
    # a stale host that cannot import the upgraded ORM module.
    stub = types.ModuleType("omnigent.db.db_models")
    monkeypatch.setitem(sys.modules, "omnigent.db.db_models", stub)

    payload = _native_terminal_start_error_payload(
        _stale_host_import_error(), "Claude", session_id="conv_1"
    )

    assert payload["title"] == "Claude can't start until you restart the host"
    assert "Restart the host" in payload["remediation"]
    assert "keeping `--server`" in payload["remediation"]
    cause = payload["cause"]
    assert "mixed-version" in cause
    assert payload["error_id"] in cause
    # Pin the fallback literal to the schema constant so a limit change
    # surfaces here instead of silently diverging.
    assert LABEL_VALUE_MAX_LEN == 256
    assert len(cause) <= LABEL_VALUE_MAX_LEN


def test_long_runtime_name_keeps_error_id_within_label_budget() -> None:
    """A long runtime name must not clip the error ID carried in the cause.

    ``cause`` ends with the log-correlation ID. An unusually long runtime name
    would push the diagnosis past the per-field label budget and, without
    reserving room, truncate exactly the ID this design preserves. The
    diagnosis is trimmed instead, so the complete ID survives the label
    persistence/read-back boundary intact.
    """
    long_runtime_name = "Experimental " + "native runtime " * 4

    payload = _native_terminal_start_error_payload(
        _stale_host_import_error(), long_runtime_name, session_id="conv_1"
    )
    cause = payload["cause"]
    # The diagnosis is trimmed with the shared marker so the ID keeps its room.
    assert "\u2026" in cause
    assert len(cause) <= LABEL_VALUE_MAX_LEN
    assert _ERROR_ID_RE.search(cause) is not None

    reloaded = _last_task_error_from_labels(
        {
            _LAST_TASK_ERROR_CODE_LABEL_KEY: _truncate_label(payload["code"]),
            _LAST_TASK_ERROR_MESSAGE_LABEL_KEY: _truncate_label(payload["message"]),
            _LAST_TASK_ERROR_TITLE_LABEL_KEY: _truncate_label(payload["title"]),
            _LAST_TASK_ERROR_CAUSE_LABEL_KEY: _truncate_label(payload["cause"]),
            _LAST_TASK_ERROR_REMEDIATION_LABEL_KEY: _truncate_label(payload["remediation"]),
        }
    )
    assert reloaded is not None
    assert reloaded["cause"] == cause
    assert payload["error_id"] in reloaded["cause"]


def _third_party_cannot_import_name() -> ImportError:
    """A dependency "cannot import name" failure with ``name_from`` set.

    A ``parametrize`` entry is an expression, so set ``name_from`` through a
    helper: the constructor kwarg exists at runtime but not in typeshed.
    """
    exc = ImportError(
        "cannot import name 'Timeout' from 'httpx'",
        name="httpx",
    )
    exc.name_from = "Timeout"
    return exc


@pytest.mark.parametrize(
    "exc",
    [
        # A "cannot import name" failure outside the omnigent tree is a plain
        # dependency problem, not a stale host: it must keep the generic log
        # pointer and must not claim a host restart will help.
        _third_party_cannot_import_name(),
        # A missing in-tree module (no ``name_from``) is not the symbol-skew
        # signature the stale-host guard is scoped to.
        ModuleNotFoundError(
            "No module named 'omnigent.runner.transports.ws_tunnel.frames'",
            name="omnigent.runner.transports.ws_tunnel.frames",
        ),
    ],
)
def test_unrelated_import_failure_keeps_generic_startup_code(exc: ImportError) -> None:
    """Only the in-tree symbol-skew signature gets the host-restart guidance."""
    payload = _native_terminal_start_error_payload(exc, "Claude", session_id="conv_1")

    assert payload["code"] == _NATIVE_TERMINAL_START_FAILED_CODE
    # The generic path stays a bare log pointer: no classified card fields.
    assert "title" not in payload
    assert "cause" not in payload
    assert "remediation" not in payload
    message = payload["message"].lower()
    assert "see the runner log" in message
    assert "restart the host" not in message


@pytest.mark.parametrize(
    ("exc", "category"),
    [
        (RuntimeError("tmux server exited before the pane was ready"), "unknown"),
        (OSError(28, "No space left on device"), "host"),
        (
            OmnigentError("agent gone", code=ErrorCode.SESSION_AGENT_MISSING),
            "user",
        ),
        (
            OmnigentError("workspace gone", code=ErrorCode.WORKSPACE_MISSING),
            "user",
        ),
        (TmuxUnavailableError("tmux is not installed or not on PATH."), "config"),
        (CodexTransportArgsError("app-server"), "config"),
    ],
)
def test_start_failure_log_row_is_blocking_with_derived_category(
    caplog: pytest.LogCaptureFixture, exc: Exception, category: str
) -> None:
    """Every start failure blocks; its owner comes from the exception, and an
    unrecognized one stays unknown rather than guessed."""
    with caplog.at_level(logging.WARNING, logger=orchestration._logger.name):
        _native_terminal_start_error_payload(exc, "Codex", session_id="conv_1")

    [record] = [
        r
        for r in caplog.records
        if getattr(r, "event_name", None) == "native_terminal_start_failed"
    ]
    attrs = record_to_row(record, source="runner")["attributes"]
    assert attrs["error_impact"] == "blocking"
    assert attrs["error_category"] == category


def test_generic_cause_names_errno_without_free_form_text() -> None:
    """The generic startup-defect message now names a structured errno cause.

    Production telemetry for these failures only carries this message, so an
    environmental cause (e.g. disk-full) must be legible without the runner
    log — but only via structured facts, never the raw exception text.
    """
    payload = _native_terminal_start_error_payload(
        OSError(28, "No space left on device"),
        "Codex",
        session_id="conv_1",
    )

    assert (
        "Native Codex terminal failed to start (OSError errno 28 ENOSPC); "
        "see the runner log for details:"
    ) in payload["message"]
    assert "No space left on device" not in payload["message"]


def test_cause_includes_errno_name_for_os_errors() -> None:
    """An ``OSError`` cause names its errno, never its free-form strerror text."""
    exc = OSError(28, "No space left on device")

    assert orchestration._native_terminal_start_failure_cause(exc) == "OSError errno 28 ENOSPC"


def test_cause_names_direct_cause_type_for_chained_exception() -> None:
    """A wrapping exception names its direct cause's type, not any message text."""
    exc = RuntimeError("private launch configuration detail")
    exc.__cause__ = httpx.ReadTimeout("private upstream URL")

    cause = orchestration._native_terminal_start_failure_cause(exc)

    assert cause == "RuntimeError (cause ReadTimeout)"


def test_cause_never_includes_exception_message_text() -> None:
    """No part of the exception's message — secrets or multi-line text — ever appears."""
    exc = RuntimeError("token=super-secret-value andmultiline\nsecond line with more detail")

    cause = orchestration._native_terminal_start_failure_cause(exc)

    assert cause == "RuntimeError"
    assert "secret" not in cause
    assert "token" not in cause
    assert "\n" not in cause


def test_cause_is_class_name_only_when_no_structured_facts_available() -> None:
    """An exception with a message but no errno/code/cause is class-name only."""
    assert orchestration._native_terminal_start_failure_cause(RuntimeError()) == "RuntimeError"


def test_cause_names_omnigent_error_code() -> None:
    """An ``OmnigentError`` cause names its structured error code, not its message."""
    exc = OmnigentError("some internal detail", code=ErrorCode.INTERNAL_ERROR)

    assert (
        orchestration._native_terminal_start_failure_cause(exc)
        == f"OmnigentError code {ErrorCode.INTERNAL_ERROR}"
    )
    assert "internal detail" not in orchestration._native_terminal_start_failure_cause(exc)


def test_cause_names_omnigent_error_code_and_cause_type() -> None:
    """A coded launch-config failure keeps the underlying transport cause visible."""
    exc = OmnigentError("could not fetch", code=ErrorCode.INTERNAL_ERROR)
    exc.__cause__ = httpx.ReadTimeout("slow")

    assert orchestration._native_terminal_start_failure_cause(exc) == (
        f"OmnigentError code {ErrorCode.INTERNAL_ERROR} (cause ReadTimeout)"
    )


def test_codex_early_exit_with_unknown_status_does_not_invent_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OMNIGENT_HARNESS_STDERR_ENABLED", raising=False)
    instance = TerminalInstance(
        name="codex",
        session_key="main",
        socket_path=tmp_path / "terminal.sock",
        private_dir=tmp_path,
    )
    read_output = Mock(side_effect=AssertionError("capture is disabled"))
    monkeypatch.setattr(instance, "last_exit_text", read_output)

    payload = _native_terminal_start_error_payload(
        TerminalExitedDuringLaunch(instance), "Codex", session_id="conv_1"
    )

    assert "Codex terminal exited before becoming available." in payload["message"]
    assert "with status" not in payload["message"]
    assert "Codex startup terminal output:" not in payload["message"]
    assert _ERROR_ID_RE.search(payload["message"]) is not None
    read_output.assert_not_called()


def test_codex_early_exit_diagnostic_failure_preserves_exit_cause(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_HARNESS_STDERR_ENABLED", "1")
    instance = TerminalInstance(
        name="codex",
        session_key="main",
        socket_path=tmp_path / "terminal.sock",
        private_dir=tmp_path,
    )
    instance._remember_exit_status("1 2")
    read_output = Mock(side_effect=ValueError("private diagnostic failure detail"))
    monkeypatch.setattr(instance, "last_exit_text", read_output)

    payload = _native_terminal_start_error_payload(
        TerminalExitedDuringLaunch(instance), "Codex", session_id="conv_1"
    )

    assert "Codex terminal exited with status 2 before becoming available." in payload["message"]
    assert "Codex startup terminal output:" not in payload["message"]
    assert "private diagnostic failure detail" not in payload["message"]
    read_output.assert_called_once_with()


def test_outdated_pi_cli_surfaces_its_actionable_message() -> None:
    """An outdated Pi CLI shows the upgrade message, not the log pointer.

    The raise site composes a client-safe message (installed version, floor,
    upgrade command); the payload must carry it verbatim so the person sees
    the remedy instead of "see the runner log".
    """
    from omnigent.harnesses.pi_native.main import PiNativeCliOutdatedError

    remedy = (
        "The installed Pi CLI (0.83.0) is older than the minimum Omnigent "
        "supports (0.84.2). Upgrade with: npm install -g "
        "@earendil-works/pi-coding-agent, then start the session again."
    )
    payload = _native_terminal_start_error_payload(
        PiNativeCliOutdatedError(remedy), "Pi", session_id="conv_1"
    )

    assert payload["code"] == _NATIVE_TERMINAL_START_FAILED_CODE
    assert remedy in payload["message"]
    assert "see the runner log" not in payload["message"]
    assert _ERROR_ID_RE.search(payload["message"]) is not None


@pytest.mark.parametrize(
    ("exc", "runtime_name"),
    [
        (
            TmuxUnavailableError(
                "tmux 3.2 is too old; managed terminals require tmux 3.3 or newer"
            ),
            "Claude",
        ),
        (CodexTransportArgsError("app-server"), "Codex"),
    ],
)
def test_host_setup_error_surfaces_its_remedy(
    exc: Exception, runtime_name: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A known setup error shows how to fix it in the session."""
    payload = _native_terminal_start_error_payload(exc, runtime_name, session_id="conv_1")

    assert payload["code"] == ErrorCode.HARNESS_NOT_CONFIGURED
    assert str(exc) in payload["message"]
    assert "see the runner log" not in payload["message"]
    assert _ERROR_ID_RE.search(payload["message"]) is not None

    response = _native_terminal_start_error_response(exc, runtime_name, session_id="conv_1")
    assert response.status_code == 412
    assert json.loads(response.body)["error"]["code"] == ErrorCode.HARNESS_NOT_CONFIGURED
    assert not any(record.exc_info for record in caplog.records)


def test_claude_early_exit_surfaces_classified_diagnosis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-Codex runtime's exit-at-spawn is classified like Codex's.

    The exit status and captured output ride on the exception for every
    runtime, so a Claude terminal whose CLI reports missing credentials must
    surface the shared diagnosis instead of the generic log pointer — without
    echoing the raw pane text.
    """
    monkeypatch.setenv("OMNIGENT_HARNESS_STDERR_ENABLED", "1")
    instance = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "terminal.sock",
        private_dir=tmp_path,
        command="claude",
    )
    instance._remember_exit_status("1 1")
    monkeypatch.setattr(instance, "last_exit_text", lambda: "Not logged in · Please run /login")

    payload = _native_terminal_start_error_payload(
        TerminalExitedDuringLaunch(instance), "Claude", session_id="conv_1"
    )

    message = payload["message"]
    assert "Agent isn't signed in" in message
    assert "no valid credentials" in message
    assert "/login" in message
    assert "Native Claude terminal failed to start" not in message
    assert "see the runner log" not in message
    # The diagnosis is rendered, never the raw pane text.
    assert "Not logged in" not in message
    assert _ERROR_ID_RE.search(message) is not None


def test_pi_early_exit_without_match_falls_back_to_exit_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unclassified exit keeps the runtime's exit summary plus gated output."""
    monkeypatch.setenv("OMNIGENT_HARNESS_STDERR_ENABLED", "1")
    instance = TerminalInstance(
        name="pi",
        session_key="main",
        socket_path=tmp_path / "terminal.sock",
        private_dir=tmp_path,
        command="pi",
    )
    instance._remember_exit_status("1 3")
    monkeypatch.setattr(instance, "last_exit_text", lambda: "some unrecognized pane text")

    payload = _native_terminal_start_error_payload(
        TerminalExitedDuringLaunch(instance), "Pi", session_id="conv_1"
    )

    message = payload["message"]
    assert "Pi terminal exited with status 3 before becoming available." in message
    assert "Pi startup terminal output:\nsome unrecognized pane text" in message
    assert "Native Pi terminal failed to start" not in message


def test_claude_early_exit_output_gated_when_capture_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With stderr capture disabled, a non-Codex exit reads no pane output."""
    monkeypatch.delenv("OMNIGENT_HARNESS_STDERR_ENABLED", raising=False)
    instance = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "terminal.sock",
        private_dir=tmp_path,
        command="claude",
    )
    instance._remember_exit_status("1 1")
    read_output = Mock(side_effect=AssertionError("capture is disabled"))
    monkeypatch.setattr(instance, "last_exit_text", read_output)

    payload = _native_terminal_start_error_payload(
        TerminalExitedDuringLaunch(instance), "Claude", session_id="conv_1"
    )

    message = payload["message"]
    assert "Claude terminal exited with status 1 before becoming available." in message
    assert "startup terminal output:" not in message
    read_output.assert_not_called()


def test_unrelated_omnigent_error_is_not_treated_as_missing_agent() -> None:
    """An ``OmnigentError`` with a different code is not reclassified.

    Only the ``SESSION_AGENT_MISSING`` code selects the lifecycle branch; an
    ``OmnigentError`` carrying an unrelated code must fall through to the
    generic startup-defect classification.
    """
    exc = OmnigentError("something else broke", code=ErrorCode.INTERNAL_ERROR)

    payload = _native_terminal_start_error_payload(exc, "Claude", session_id="conv_1")

    assert payload["code"] == _NATIVE_TERMINAL_START_FAILED_CODE
    assert "agent no longer exists" not in payload["message"]


@pytest.mark.parametrize(
    "lifecycle_code",
    [None, ErrorCode.SESSION_AGENT_MISSING, ErrorCode.WORKSPACE_MISSING],
)
def test_startup_failure_diagnostics_belong_to_failing_child(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    lifecycle_code: ErrorCode | None,
) -> None:
    """A shared runner's parent must not receive a child's startup failure evidence."""
    monkeypatch.setattr(orchestration, "runner_primary_session_id", lambda: "parent-session")
    private_detail = "private launch configuration"
    if lifecycle_code is not None:
        exc = OmnigentError(private_detail, code=lifecycle_code)
    else:
        exc = RuntimeError(private_detail)
        exc.__cause__ = httpx.ReadTimeout("private upstream URL")

    events: list[dict[str, object]] = []
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        payload = _publish_native_terminal_start_error(
            lambda _session_id, event: events.append(event), "child-session", "Codex", exc
        )

    assert len(events) == 1
    context = events[0]["failure_context"]
    assert isinstance(context, dict)
    assert context["failure_id"] == payload["error_id"]
    assert context["failure_source"] == "native_terminal_start"
    assert context["exception_type"] == type(exc).__name__
    assert private_detail not in str(context)
    record = next(r for r in caplog.records if payload["error_id"] in r.getMessage())
    row = record_to_row(record, source="runner")
    assert row["session_id"] == "child-session"
    assert row["event_name"] == "native_terminal_start_failed"
    attributes = row["attributes"]
    assert isinstance(attributes, dict)
    assert attributes["error_id"] == payload["error_id"]
    assert attributes["code"] == payload["code"]
    assert attributes["runtime"] == "Codex"
    assert attributes["exception_type"] == type(exc).__name__
    assert private_detail not in str(attributes)
    assert private_detail not in payload["message"]
    if lifecycle_code is not None:
        assert row["stack_trace"] is None
    else:
        assert attributes["exception_cause_type"] == "ReadTimeout"
        assert "ReadTimeout" in str(row["stack_trace"])
        # The generic startup-defect branch names the direct cause's type as
        # a structured, non-sensitive fact — never the free-form message.
        assert "(cause ReadTimeout)" in payload["message"]


@pytest.mark.parametrize("code", [ErrorCode.SESSION_AGENT_MISSING, ErrorCode.WORKSPACE_MISSING])
def test_ensure_response_for_a_removed_session_resource_is_410(code: ErrorCode) -> None:
    """A removed session resource is a lifecycle condition, not a runner failure."""
    removed = _native_terminal_start_error_response(
        OmnigentError("resource gone", code=code), "Claude", session_id="conv_1"
    )
    assert removed.status_code == 410
    assert json.loads(removed.body)["error"]["code"] == code


def test_ensure_response_for_other_failure_is_500() -> None:
    other = _native_terminal_start_error_response(
        OmnigentError("boom", code=ErrorCode.INTERNAL_ERROR), "Claude", session_id="conv_1"
    )
    assert other.status_code == 500


def test_ensure_response_and_diagnostic_share_error_and_session_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The HTTP error's opaque ID locates its cause on the requested session."""
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        response = _native_terminal_start_error_response(
            OSError("private launch path"), "Codex", session_id="ensured-session"
        )

    payload = json.loads(response.body)["error"]
    record = next(r for r in caplog.records if payload["error_id"] in r.getMessage())
    row = record_to_row(record, source="runner")
    assert response.status_code == 500
    assert row["session_id"] == "ensured-session"
    attributes = row["attributes"]
    assert isinstance(attributes, dict)
    assert attributes["error_id"] == payload["error_id"]
    assert attributes["exception_type"] == "OSError"
    assert "private launch path" not in str(attributes)
    assert "private launch path" not in payload["message"]
    # This ``OSError`` has no numeric errno (single-arg constructor), so the
    # structured cause is the class name only.
    assert "(OSError)" in payload["message"]
