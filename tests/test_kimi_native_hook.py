"""Tests for the kimi-native tool-policy hook commands."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import httpx
import pytest

from omnigent import kimi_native_hook
from omnigent.kimi_native_bridge import APPROVE_KEY, DENY_KEY, write_hook_config
from omnigent.native_policy_hook import _EVAL_UNAVAILABLE_REASON


def _governed_bridge(tmp_path: Path, *, server: str = "http://127.0.0.1:8787") -> Path:
    """Make a bridge dir with a hook_config so the session reads as governed."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    write_hook_config(
        bridge_dir,
        server_url=server,
        headers={"Authorization": "Bearer t"},
        session_id="conv_abc",
    )
    return bridge_dir


def _feed_stdin(monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))


def test_evaluate_policy_deny_emits_kimi_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A DENY verdict becomes kimi's ``permissionDecision: deny`` + reason."""
    bridge_dir = _governed_bridge(tmp_path)
    _feed_stdin(
        monkeypatch,
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /"},
        },
    )
    monkeypatch.setattr(
        kimi_native_hook,
        "post_evaluate_with_retry",
        lambda *a, **k: (
            httpx.Response(
                200,
                json={"result": "POLICY_ACTION_DENY", "reason": "blocked by policy"},
                request=httpx.Request("POST", "http://x"),
            ),
            None,
        ),
    )

    exit_code = kimi_native_hook.main(["evaluate-policy", "--bridge-dir", str(bridge_dir)])

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == "blocked by policy"


def test_evaluate_policy_allow_emits_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ALLOW (engine default) emits no output so kimi's own prompt still runs."""
    bridge_dir = _governed_bridge(tmp_path)
    _feed_stdin(
        monkeypatch,
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}},
    )
    monkeypatch.setattr(
        kimi_native_hook,
        "post_evaluate_with_retry",
        lambda *a, **k: (
            httpx.Response(
                200,
                json={"result": "POLICY_ACTION_ALLOW"},
                request=httpx.Request("POST", "http://x"),
            ),
            None,
        ),
    )

    exit_code = kimi_native_hook.main(["evaluate-policy", "--bridge-dir", str(bridge_dir)])

    assert exit_code == 0
    assert capsys.readouterr().out == ""


def test_evaluate_policy_ungoverned_session_no_opinion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No hook_config (no session/server) → exit 0, no output, no POST."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    _feed_stdin(monkeypatch, {"hook_event_name": "PreToolUse", "tool_name": "Bash"})

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("must not POST for an ungoverned session")

    monkeypatch.setattr(kimi_native_hook, "post_evaluate_with_retry", _boom)

    exit_code = kimi_native_hook.main(["evaluate-policy", "--bridge-dir", str(bridge_dir)])

    assert exit_code == 0
    assert capsys.readouterr().out == ""


def test_evaluate_policy_fails_closed_when_unreachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A governed PreToolUse with no usable verdict fails CLOSED (deny)."""
    bridge_dir = _governed_bridge(tmp_path)
    _feed_stdin(monkeypatch, {"hook_event_name": "PreToolUse", "tool_name": "Bash"})
    monkeypatch.setattr(
        kimi_native_hook,
        "post_evaluate_with_retry",
        lambda *a, **k: (None, "connection error: simulated"),
    )

    exit_code = kimi_native_hook.main(["evaluate-policy", "--bridge-dir", str(bridge_dir)])

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"].startswith(
        _EVAL_UNAVAILABLE_REASON
    )


def _capture_injection(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch ``inject_approval_keystroke`` to record the option keys it gets."""
    keys: list[str] = []

    def _fake_inject(bridge_dir: Path, *, key: str, timeout_s: float = 0.0) -> bool:
        del bridge_dir, timeout_s
        keys.append(key)
        return True

    monkeypatch.setattr(kimi_native_hook, "inject_approval_keystroke", _fake_inject)
    return keys


@pytest.mark.parametrize(
    ("verdict", "expected_key"),
    [("allow", APPROVE_KEY), ("deny", DENY_KEY)],
)
def test_permission_request_injects_keystroke_for_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verdict: str,
    expected_key: str,
) -> None:
    """A web Approve/Deny verdict is typed into kimi's prompt as the option digit."""
    bridge_dir = _governed_bridge(tmp_path)
    _feed_stdin(
        monkeypatch,
        {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_call_id": "tc_1"},
    )
    posted: list[dict[str, object]] = []
    monkeypatch.setattr(
        kimi_native_hook,
        "_request_web_approval",
        lambda url, headers, body: posted.append({"url": url, "body": body}) or verdict,
    )
    keys = _capture_injection(monkeypatch)

    exit_code = kimi_native_hook.main(["permission-request", "--bridge-dir", str(bridge_dir)])

    assert exit_code == 0
    # Routed to the shared elicitation endpoint with the gated tool.
    assert posted[0]["url"].endswith("/v1/sessions/conv_abc/hooks/permission-request")
    assert posted[0]["body"]["tool_name"] == "Bash"
    assert keys == [expected_key]


def test_permission_request_no_verdict_injects_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No web verdict (timeout/unreachable/answered in terminal) → no keystroke."""
    bridge_dir = _governed_bridge(tmp_path)
    _feed_stdin(monkeypatch, {"hook_event_name": "PermissionRequest", "tool_name": "Bash"})
    monkeypatch.setattr(kimi_native_hook, "_request_web_approval", lambda *a, **k: None)
    keys = _capture_injection(monkeypatch)

    assert kimi_native_hook.main(["permission-request", "--bridge-dir", str(bridge_dir)]) == 0
    assert keys == []


def test_permission_request_ungoverned_no_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No hook_config → no approval request and no keystroke (never raises)."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    _feed_stdin(monkeypatch, {"hook_event_name": "PermissionRequest", "tool_name": "Bash"})

    def _boom(*_a: object, **_k: object) -> str | None:
        raise AssertionError("ungoverned session must not request approval")

    monkeypatch.setattr(kimi_native_hook, "_request_web_approval", _boom)
    keys = _capture_injection(monkeypatch)

    assert kimi_native_hook.main(["permission-request", "--bridge-dir", str(bridge_dir)]) == 0
    assert keys == []


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"hookSpecificOutput": {"decision": {"behavior": "allow"}}}, "allow"),
        ({"hookSpecificOutput": {"decision": {"behavior": "deny"}}}, "deny"),
        ({"hookSpecificOutput": {"permissionDecision": "allow"}}, "allow"),
        ({"hookSpecificOutput": {"permissionDecision": "deny"}}, "deny"),
        ({"hookSpecificOutput": {"decision": {"behavior": "allow_always"}}}, "allow"),
        ({"hookSpecificOutput": {"decision": {"behavior": "reject"}}}, "deny"),
        ({}, None),
        ({"hookSpecificOutput": {}}, None),
        ({"hookSpecificOutput": {"decision": {"behavior": "huh"}}}, None),
        ("not a dict", None),
    ],
)
def test_verdict_from_response(response: object, expected: str | None) -> None:
    assert kimi_native_hook._verdict_from_response(response) == expected


def test_unknown_subcommand_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert kimi_native_hook.main(["bogus", "--bridge-dir", "/tmp/x"]) == 2
