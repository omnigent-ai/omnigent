"""Exact draft ownership after a non-destructive composer reflow."""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnigent.harnesses.claude_native import bridge
from omnigent.harnesses.claude_native.model_switch import _draft
from tests.harnesses.claude_native._model_switch_support import (
    _session_model_bridge,
    _SessionModelComposer,
)


@pytest.fixture(autouse=True)
def _trust_test_bridge_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path)


@pytest.mark.parametrize(
    "outcome",
    [
        "owned",
        "replacement-spacing",
        "logical-newline",
        "session",
        "cancelled",
        "deadline",
        "unavailable",
    ],
)
def test_a_reflow_must_prove_the_exact_logical_replay_draft_before_clearing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    capture = json.loads(
        (
            Path(__file__).with_name("fixtures") / "claude-blocked-route-render-wrap.json"
        ).read_text()
    )
    original = capture["input"]
    composer = _SessionModelComposer(draft=original)
    bridge_dir = _session_model_bridge(tmp_path, composer)
    composer.install(monkeypatch)
    cancelled = threading.Event()
    wide = False
    restored = False
    requested_widths: list[int] = []

    def pane(socket_path: str, target: str, *, join_wrapped: bool = False) -> str:
        if not wide and composer.text == original:
            return capture["joined" if join_wrapped else "raw"]
        return composer._capture_pane(socket_path, target, join_wrapped=join_wrapped)

    @contextmanager
    def reflow(
        directory: Path, *, recipient: bridge.ClaudeLegacyModelPane, minimum_columns: int
    ) -> Iterator[bool]:
        nonlocal wide, restored
        assert directory == bridge_dir
        assert recipient.active_session_id == "web-session"
        requested_widths.append(minimum_columns)
        if outcome == "unavailable":
            yield False
            return
        wide = True
        if outcome == "replacement-spacing":
            composer.text = original.replace("Context marker", "Context  marker")
            composer.cursor = len(composer.text)
        elif outcome == "session":
            bridge.write_active_session_id(bridge_dir, "replacement-web-session")
        elif outcome == "cancelled":
            cancelled.set()
        elif outcome == "deadline":
            bridge.time.sleep(1.0)
        try:
            yield True
        finally:
            wide = False
            restored = True

    monkeypatch.setattr(bridge, "_capture_pane", pane)
    monkeypatch.setattr(bridge, "_reflow_composer", reflow)
    expected = _draft(capture["raw"]) if outcome == "logical-newline" else original
    assert expected is not None
    initial_default = composer.saved_default
    with bridge.cancellable_injection(cancelled):
        if outcome == "owned":
            result = bridge.inject_model_command(
                bridge_dir, model="private-glm[1m]", owned_draft=expected, timeout_s=1.0
            )
            assert result.requested_model == composer.model == "private-glm[1m]"
        else:
            with pytest.raises(bridge.ClaudeModelSwitchRefused):
                bridge.inject_model_command(
                    bridge_dir, model="private-glm[1m]", owned_draft=expected, timeout_s=1.0
                )

    assert requested_widths and requested_widths[0] > capture["columns"]
    assert restored is (outcome != "unavailable")
    assert composer.saved_default == initial_default
    if outcome == "owned":
        assert composer.text == ""
        assert composer.submitted == ["/status", "/model", "/status"]
        assert expected not in composer.submitted
    else:
        assert composer.text == (
            original.replace("Context marker", "Context  marker")
            if outcome == "replacement-spacing"
            else original
        )
        assert composer.sends == composer.submitted == []


@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "cancelled",
        "query-replaced",
        "body-replaced",
        "advertisement",
        "native-session",
        "version",
        "resize-failed",
        "unknown",
        "wide",
    ],
)
def test_reflow_restores_size_only_for_the_original_recipient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    composer = _SessionModelComposer()
    bridge_dir = _session_model_bridge(tmp_path, composer)
    recipient = bridge._legacy_model_pane(bridge_dir)
    assert recipient is not None
    cancelled = threading.Event()
    policy = "manual" if outcome == "cancelled" else "latest"
    metadata = f"105 50 {policy}\n" + (f"{policy}\n" if policy == "manual" else "")
    if outcome == "unknown":
        metadata = "unreadable"
    elif outcome == "wide":
        metadata = "200 50 latest\n"

    def query(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if outcome == "query-replaced":
            bridge.write_active_session_id(bridge_dir, "replacement-web-session")
        return subprocess.CompletedProcess([], 0, stdout=metadata)

    sends: list[tuple[str, ...]] = []

    def send(_socket: str, *args: str, **kwargs: object) -> None:
        bridge._check_injection_cancelled()
        sends.append(args)
        if outcome == "resize-failed" and len(sends) == 1:
            raise RuntimeError("resize failed")

    monkeypatch.setattr("subprocess.run", Mock(side_effect=query))
    monkeypatch.setattr(bridge, "_run_tmux", send)
    with bridge.cancellable_injection(cancelled):
        try:
            with bridge._reflow_composer(
                bridge_dir, recipient=recipient, minimum_columns=140
            ) as resized:
                assert resized is (outcome not in {"query-replaced", "unknown", "wide"})
                if outcome == "cancelled":
                    cancelled.set()
                    raise bridge.ClaudeInjectionCancelled("cancelled")
                if outcome == "body-replaced":
                    bridge.write_active_session_id(bridge_dir, "replacement-web-session")
                elif outcome == "advertisement":
                    bridge.write_tmux_target(
                        bridge_dir,
                        socket_path=Path(recipient.socket_path),
                        tmux_target=recipient.tmux_target,
                    )
                elif outcome == "native-session":
                    (bridge_dir / "state.json").write_text(
                        '{"claude_session_id":"replacement-native-session"}'
                    )
                elif outcome == "version":
                    path = bridge_dir / bridge.CONTEXT_RAW_FILE
                    raw = json.loads(path.read_text())
                    raw["version"] = "2.1.324"
                    path.write_text(json.dumps(raw))
        except RuntimeError as exc:
            assert outcome in {"cancelled", "resize-failed"}
            assert str(exc) == ("cancelled" if outcome == "cancelled" else "resize failed")
        assert bridge._INJECTION_CANCEL_EVENT.get() is cancelled

    if outcome in {"query-replaced", "unknown", "wide"}:
        assert sends == []
    else:
        assert sends[0] == ("resize-window", "-t", "claude:0.0", "-x", "140")
        if outcome in {"body-replaced", "advertisement", "native-session"}:
            assert len(sends) == 1
        else:
            restore_policy = (
                ("set-option", "-w", "-t", "claude:0.0", "window-size", policy)
                if policy == "manual"
                else ("set-option", "-wu", "-t", "claude:0.0", "window-size")
            )
            assert sends[1:] == [
                (
                    "resize-window",
                    "-t",
                    "claude:0.0",
                    "-x",
                    "105",
                    "-y",
                    "50",
                    ";",
                    *restore_policy,
                )
            ]
