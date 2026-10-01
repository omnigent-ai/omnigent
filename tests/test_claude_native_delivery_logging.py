"""Delivery diagnostics distinguish missing evidence from a submitted prompt."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.debug_logging import current_session_id_scope, record_to_row
from omnigent.harnesses.claude_native import bridge, delivery_diagnostics


@pytest.mark.parametrize(
    ("scenario", "verification", "outcome"),
    [
        ("normal", "draft_absent", "returned"),
        ("session_fallback", "draft_absent", "returned"),
        ("unknown_command", "draft_absent", "returned"),
        ("blank_line", "unverified", "returned"),
        ("retry", "draft_absent", "returned"),
        ("timeout", "draft_still_present", "error"),
        ("empty_capture", "inconclusive_capture", "returned"),
        ("missing_glyph", "inconclusive_capture", "returned"),
        ("transport_error", "not_started", "error"),
        ("observation_error", "not_started", "returned"),
        ("startup_error", "not_started", "error"),
        ("pending_before_paste", "not_started", "error"),
        ("pending_before_submit", "not_started", "error"),
        ("cancelled", "not_started", "interrupted"),
    ],
)
def test_delivery_diagnostics(
    scenario: str,
    verification: str,
    outcome: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "private customer prompt"
    if scenario == "unknown_command":
        secret = "/private-customer-command"
    content = "\n" + secret if scenario == "blank_line" else secret
    elapsed = 0.0
    pane = "❯ "
    enters = 0
    pending_checks = 0

    def pending_prompt(*args: object) -> bool:
        nonlocal pending_checks
        pending_checks += 1
        return pending_checks == (1 if scenario == "pending_before_paste" else 2)

    def broken_observation(*args: object, **kwargs: object) -> None:
        raise RuntimeError(secret)

    def sleep(seconds: float) -> None:
        nonlocal elapsed
        elapsed += seconds

    def ready(*args: object, **kwargs: object) -> None:
        sleep(0.02)
        if scenario == "startup_error":
            raise RuntimeError(secret)
        if scenario == "cancelled":
            raise bridge.ClaudeInjectionCancelled(secret)

    def run_tmux(socket: str, *args: str) -> None:
        nonlocal pane, enters
        assert not any(
            getattr(record, "event_name", "").startswith("claude_native_")
            for record in caplog.records
        )
        if args[0] == "paste-buffer":
            if scenario == "transport_error":
                raise RuntimeError(secret)
            pane = "❯ " + content
        if args[-1] == "Enter":
            enters += 1
            if scenario in {"blank_line", "timeout"} or (scenario == "retry" and enters == 1):
                return
            pane = {"empty_capture": "", "missing_glyph": "terminal output"}.get(scenario, "❯ ")

    monkeypatch.setattr(
        bridge,
        "time",
        SimpleNamespace(monotonic=lambda: elapsed, sleep=sleep, time=lambda: elapsed),
    )
    monkeypatch.setattr(delivery_diagnostics, "time", bridge.time)
    monkeypatch.setattr(
        bridge,
        "_wait_for_tmux_info",
        lambda *_a, **_k: {"socket_path": "/unused/socket", "tmux_target": "main"},
    )
    monkeypatch.setattr(bridge, "_restore_occupied_input", lambda *_a, **_k: None)
    monkeypatch.setattr(bridge, "_wait_for_claude_prompt_ready", ready)
    monkeypatch.setattr(bridge, "_run_tmux", run_tmux)
    monkeypatch.setattr(bridge, "_capture_pane", lambda *_a, **_k: pane)
    if scenario == "observation_error":
        monkeypatch.setattr(delivery_diagnostics, "_draft_observation", broken_observation)
    if scenario.startswith("pending_"):
        monkeypatch.setattr(bridge, "has_pending_user_prompt", pending_prompt)
    if scenario == "unknown_command":
        monkeypatch.setattr(bridge, "_unknown_command_rejection_appeared", lambda *_a, **_k: True)
    monkeypatch.setattr(bridge, "_PASTE_COMMIT_TIMEOUT_S", 0.03)
    monkeypatch.setattr(bridge, "_PASTE_SETTLE_S", 0.0)
    monkeypatch.setattr(bridge, "_CLAUDE_READY_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(bridge, "_SUBMIT_VERIFY_TIMEOUT_S", 0.06)
    monkeypatch.setattr(bridge, "_SUBMIT_RETRY_INTERVAL_S", 0.02)

    if scenario == "session_fallback":
        (tmp_path / bridge._CONFIG_FILE).write_text(
            json.dumps({"active_session_id": "bridge-session"})
        )

    with (
        caplog.at_level("INFO", logger=delivery_diagnostics.__name__),
        current_session_id_scope(None if scenario == "session_fallback" else "child-session"),
    ):
        if outcome == "returned":
            bridge.inject_user_message(tmp_path, content=content)
        else:
            expected = bridge.ClaudeInjectionCancelled if scenario == "cancelled" else RuntimeError
            if scenario.startswith("pending_"):
                expected = bridge.ClaudeUserPromptPending
            with pytest.raises(expected):
                bridge.inject_user_message(tmp_path, content=content)

    records = [
        r for r in caplog.records if getattr(r, "event_name", "").startswith("claude_native_")
    ]
    assert len(records) == 1
    record = records[0]
    assert record.event_name == "claude_native_delivery_finished"
    attrs = record.attributes
    assert attrs["verification"] == verification
    assert attrs["outcome"] == outcome
    assert record.levelname == (
        "INFO" if verification == "draft_absent" and outcome == "returned" else "WARNING"
    )
    expected_session = "bridge-session" if scenario == "session_fallback" else "child-session"
    assert record.session_id == expected_session
    assert delivery_diagnostics._prompt_delivery_trace.get() is None
    row = record_to_row(record, "harness")
    assert secret not in json.dumps(row)
    assert secret not in record.getMessage()
    assert row["attributes"]["verification"] == verification
    assert attrs["stage_waiting_for_prompt_ms"] == 20

    if scenario == "observation_error":
        assert enters == 1
        assert pane == "❯ "
        assert attrs["submit_sent"] is True
    if scenario == "unknown_command":
        assert enters == 2
        assert attrs["attempt"] == 2
        attempts = json.loads(row["attributes"]["attempts"])
        assert len(attempts) == 2
        assert all(attempt["submit_sent"] for attempt in attempts)
        assert all(attempt["verification"] == "draft_absent" for attempt in attempts)

    if scenario in {"normal", "empty_capture"}:
        assert attrs["draft_polls"] == 1
        assert attrs["draft_empty_captures"] == 0
        assert attrs["draft_wait_ms"] == 0
        assert attrs["draft_pane_rows"] == 1
        assert attrs["draft_pane_max_columns"] == len("❯ " + secret)
        assert attrs["submit_polls"] == 1
        assert attrs["submit_wait_ms"] == 10
        assert attrs["submit_pane_rows"] == (0 if scenario == "empty_capture" else 1)
        assert attrs["submit_pane_max_columns"] == (0 if scenario == "empty_capture" else 2)
        assert attrs["submit_capture_empty"] == (scenario == "empty_capture")
        assert attrs["stage_verifying_submit_ms"] == 10
        assert attrs["elapsed_ms"] == 30

    if scenario == "blank_line":
        assert attrs["draft_seen"] is False
        assert attrs["draft_needle_visible_below_prompt"] is True
        assert attrs["leading_blank_line"] is True
        assert attrs["submit_sent"] is True
        assert enters == 1
        assert pane == "❯ \n" + secret
    elif scenario in {"normal", "retry", "timeout", "empty_capture", "missing_glyph"}:
        assert attrs["retries"] == enters - 1
        if scenario == "retry":
            assert enters == 2
    if scenario in {"startup_error", "cancelled"}:
        assert attrs["stage"] == "waiting_for_prompt"
    elif scenario == "transport_error":
        assert attrs["stage"] == "pasting"
    elif scenario.startswith("pending_"):
        assert attrs["stage"] == "checking_pending_prompt"
        assert attrs["submit_sent"] is False
        assert enters == 0


@pytest.mark.parametrize("delivery_fails", [False, True])
def test_summary_logging_failure_preserves_delivery_outcome(
    delivery_fails: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    log_calls = 0

    def broken_log(*args: object, **kwargs: object) -> None:
        nonlocal log_calls
        log_calls += 1
        raise OSError("log destination unavailable")

    @delivery_diagnostics.trace_delivery(
        session_id_reader=bridge.read_active_session_id,
        cancelled_error=bridge.ClaudeInjectionCancelled,
    )
    def deliver(bridge_dir: Path, *, content: str) -> str:
        if delivery_fails:
            raise RuntimeError("delivery failed")
        return "delivered"

    monkeypatch.setattr(delivery_diagnostics._logger, "log", broken_log)
    with caplog.at_level("INFO", logger=delivery_diagnostics.__name__):
        if delivery_fails:
            with pytest.raises(RuntimeError, match="delivery failed"):
                deliver(tmp_path, content="test prompt")
        else:
            assert deliver(tmp_path, content="test prompt") == "delivered"
    assert log_calls == 1
    assert delivery_diagnostics._prompt_delivery_trace.get() is None


@pytest.mark.parametrize("delivery_fails", [False, True])
def test_trace_setup_failure_preserves_delivery_outcome(
    delivery_fails: bool, tmp_path: Path
) -> None:
    delivered = False

    def unreadable_session(bridge_dir: Path) -> str | None:
        raise PermissionError("diagnostic session lookup failed")

    @delivery_diagnostics.trace_delivery(
        session_id_reader=unreadable_session,
        cancelled_error=bridge.ClaudeInjectionCancelled,
    )
    def deliver(bridge_dir: Path, *, content: str) -> str:
        nonlocal delivered
        delivered = True
        if delivery_fails:
            raise RuntimeError("delivery failed")
        return "delivered"

    with current_session_id_scope(None):
        if delivery_fails:
            with pytest.raises(RuntimeError, match="delivery failed"):
                deliver(tmp_path, content="test prompt")
        else:
            assert deliver(tmp_path, content="test prompt") == "delivered"
    assert delivered
    assert delivery_diagnostics._prompt_delivery_trace.get() is None
