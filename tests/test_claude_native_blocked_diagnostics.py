"""Blocker observations preserve the evidence without a continuous pane log."""

from __future__ import annotations

import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Never

import pytest

from omnigent.harnesses.claude_native import blocked_diagnostics as diagnostics
from omnigent.harnesses.claude_native.blocked_diagnostics import (
    NativeBlockedDiagnostics,
    describe_dialog,
)

_RULE = "─" * 80
_READY = f"{_RULE}\n❯ \n{_RULE}\n"
_UNKNOWN = f"private transcript above divider\n{_RULE}\nA new notice\nPress Enter to continue"


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event_name", None) == "native_blocked_state"]


def _observer(context: Callable[[], dict[str, object]] | None = None) -> NativeBlockedDiagnostics:
    return NativeBlockedDiagnostics(
        session_id="child-session",
        socket_path="/private/socket",
        tmux_target="main",
        terminal_instance_id="instance-1",
        observation_source="runner_watcher",
        context=context,
    )


def test_unknown_dialog_excerpt_excludes_transcript_and_credentials() -> None:
    observation = describe_dialog(_UNKNOWN)
    assert observation is not None
    assert observation.kind == "unknown"
    assert observation.excerpt == "A new notice Press Enter to continue"
    assert "private transcript" not in observation.excerpt
    for secret in (
        "password: a-secret-value",
        "Authorization: Bearer hidden-value",
        "api key: wrapped-secret\nremaining-secret",
    ):
        observation = describe_dialog(_UNKNOWN.replace("A new notice", secret))
        assert observation is not None
        assert observation.excerpt == "[credential dialog omitted]"
    observation = describe_dialog(
        _UNKNOWN.replace("A new notice", "Visit https://host/?code=secret")
    )
    assert observation is not None
    assert observation.excerpt == "Visit [URL] Press Enter to continue"


def test_dialog_excerpts_require_a_structural_boundary_and_do_not_quote_permissions() -> None:
    assert describe_dialog(_UNKNOWN + _READY) is None
    without_rule = describe_dialog("A new notice\nPress Enter to continue")
    assert without_rule is not None and without_rule.excerpt is None
    permission = describe_dialog(
        f"{_RULE}\nDo you want to run secret command?\nEnter to select · Esc to cancel"
    )
    assert permission is not None
    assert permission.kind == "user_prompt" and permission.excerpt is None
    observation = describe_dialog(_UNKNOWN.replace("A new notice", "Notice " + "word " * 500))
    assert observation is not None and len(observation.excerpt or "") <= 600


def test_episode_logs_once_then_persistence_and_clear_with_same_identity(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(diagnostics, "time", SimpleNamespace(monotonic=lambda: clock.now))
    caplog.set_level(logging.INFO, logger=diagnostics.__name__)
    context_calls: list[bool] = []

    def context() -> dict[str, object]:
        context_calls.append(True)
        return {"approval_wait_state": "absent"}

    observer = _observer(context=context)
    for tick in range(125):
        clock.now = float(tick)
        observer.observe(_UNKNOWN)
    observer.observe(_READY)
    records = _records(caplog)
    assert [record.attributes["phase"] for record in records] == [
        "entered",
        "persistent",
        "cleared",
    ]
    assert len(context_calls) == 3
    assert len({record.attributes["block_episode_id"] for record in records}) == 1
    assert len({record.attributes["terminal_locator_id"] for record in records}) == 1
    assert all(record.attributes["terminal_instance_id"] == "instance-1" for record in records)
    assert all(record.session_id == "child-session" for record in records)
    assert records[1].attributes["blocked_elapsed_ms"] == 60000
    assert "/private/socket" not in repr([record.attributes for record in records])
    observer.observe(_UNKNOWN)
    assert (
        _records(caplog)[-1].attributes["block_episode_id"]
        != records[0].attributes["block_episode_id"]
    )


def test_missing_or_stale_pane_never_fabricates_clearance(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(diagnostics, "time", SimpleNamespace(monotonic=lambda: clock.now))
    caplog.set_level(logging.INFO, logger=diagnostics.__name__)
    observer = _observer()
    observer.observe(
        None,
        raw_status="waiting",
        status_file_state="readable",
        status_updated_at=1000,
        blocked_on="dialog open",
        capture_age_ms=None,
    )
    observer.observe(_READY, capture_age_ms=3000)
    clock.now = 61.0
    observer.observe(None, status_file_state="unreadable", capture_age_ms=None)
    records = _records(caplog)
    assert [record.attributes["phase"] for record in records] == ["entered", "persistent"]
    assert records[0].attributes["capture_status"] == "missing"
    assert records[0].attributes["blocked_reason"] == "dialog open"
    assert records[0].attributes["status_updated_at"] == 1000
    assert records[1].attributes["status_file_state"] == "unreadable"
    observer.observe(_READY, raw_status="idle", status_file_state="readable")
    assert _records(caplog)[-1].attributes["phase"] == "cleared"


def test_context_and_logging_failure_cannot_escape_observation(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> Never:
        raise RuntimeError("diagnostic failure")

    caplog.set_level(logging.INFO, logger=diagnostics.__name__)
    observer = _observer(context=fail)
    observer.observe(_UNKNOWN)
    assert _records(caplog)[0].attributes["context_status"] == "unavailable"
    monkeypatch.setattr(diagnostics._logger, "info", fail)
    observer.observe(_READY)


def test_known_dialog_transition_is_not_mislabeled_as_previous_blocker(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.INFO, logger=diagnostics.__name__)
    observer = _observer()
    monkeypatch.setattr(
        diagnostics,
        "describe_dialog",
        lambda _pane: diagnostics.DialogObservation("auto_mode_classifier_billing_notice"),
    )
    observer.observe("Claude Code v2.1.278")
    monkeypatch.setattr(
        diagnostics, "describe_dialog", lambda _pane: diagnostics.DialogObservation("user_prompt")
    )
    observer.observe("permission dialog")
    records = _records(caplog)
    assert [record.attributes["phase"] for record in records] == ["entered", "changed"]
    assert records[1].attributes["dialog_kind"] == "user_prompt"
    assert records[1].attributes["previous_dialog_kind"] == "auto_mode_classifier_billing_notice"
    assert records[1].attributes["block_episode_id"] == records[0].attributes["block_episode_id"]
    assert records[1].attributes["native_cli_version"] == "2.1.278"
    assert records[1].attributes["native_cli_version_source"] == "pane_banner"


def test_permission_mode_records_observation_age_instead_of_guessing_current_mode(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(diagnostics, "time", SimpleNamespace(monotonic=lambda: clock.now))
    caplog.set_level(logging.INFO, logger=diagnostics.__name__)
    observer = _observer()
    observer.observe(_READY + "⏵⏵ auto mode on (shift+tab to cycle)")
    clock.now = 5.0
    observer.observe(_UNKNOWN)
    attrs = _records(caplog)[0].attributes
    assert attrs["native_permission_mode"] == "auto"
    assert attrs["permission_mode_source"] == "pane_footer"
    assert attrs["permission_mode_observation_age_ms"] == 5000
