"""Tests for the kimi-native tool-policy hook commands."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.kimi_native import hook as kimi_native_hook
from omnigent.harnesses.kimi_native.bridge import (
    APPROVE_KEY,
    DENY_KEY,
    KimiApprovalPromptAmbiguousError,
    KimiApprovalPromptNotFoundError,
    write_hook_config,
)
from omnigent.native.native_policy_hook import _EVAL_UNAVAILABLE_REASON


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
    ("verdict", "expected_keys"),
    [
        # Accept types the "approve once" digit; cancel (web dismiss, no
        # server Escape) types the "reject" digit to close the menu.
        ("accept", [APPROVE_KEY]),
        ("cancel", [DENY_KEY]),
        # An explicit web decline injects NOTHING: the server-forwarded Escape
        # already answered kimi's menu, so a second keystroke would race it.
        ("decline", []),
    ],
)
def test_permission_request_injects_keystroke_for_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verdict: str,
    expected_keys: list[str],
) -> None:
    """Accept/cancel type an option digit; an explicit decline types nothing."""
    bridge_dir = _governed_bridge(tmp_path)
    _feed_stdin(
        monkeypatch,
        {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "tool_call_id": "tc_1",
            "tool_input": {"command": "ls -la /tmp"},
        },
    )
    posted: list[dict[str, object]] = []
    monkeypatch.setattr(
        kimi_native_hook,
        "_request_web_approval",
        lambda url, headers, body, **kwargs: posted.append({"url": url, "body": body}) or verdict,
    )
    keys = _capture_injection(monkeypatch)

    exit_code = kimi_native_hook.main(["permission-request", "--bridge-dir", str(bridge_dir)])

    assert exit_code == 0
    # Routed to the vendor-agnostic native-permission endpoint, labelled Kimi.
    assert posted[0]["url"].endswith("/v1/sessions/conv_abc/hooks/native-permission-request")
    body = posted[0]["body"]
    assert body["agent"] == "Kimi"
    assert body["policy_name"] == "kimi_native_permission"
    assert body["message"] == "Kimi wants to call **Bash**"
    assert body["operation_type"] == "Bash"
    # Re-attach id was renamed from ``_omnigent_elicitation_id``.
    assert body["elicitation_id"].startswith("elicit_kimi_")
    # The gated command reaches the card as the preview.
    assert "ls -la /tmp" in body["content_preview"]
    assert keys == expected_keys


def test_permission_request_omits_preview_without_tool_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No usable tool_input → the card body carries no content_preview field."""
    bridge_dir = _governed_bridge(tmp_path)
    _feed_stdin(monkeypatch, {"hook_event_name": "PermissionRequest", "tool_name": "Bash"})
    posted: list[dict[str, object]] = []
    monkeypatch.setattr(
        kimi_native_hook,
        "_request_web_approval",
        lambda url, headers, body, **kwargs: posted.append({"body": body}) or "accept",
    )
    _capture_injection(monkeypatch)

    assert kimi_native_hook.main(["permission-request", "--bridge-dir", str(bridge_dir)]) == 0
    assert "content_preview" not in posted[0]["body"]


@pytest.mark.parametrize(
    ("tool_input", "expected"),
    [
        # A shell command is the most useful single line.
        ({"command": "ls -la /tmp"}, "ls -la /tmp"),
        ({"command": "  echo hi  "}, "echo hi"),
        # No command → the JSON-encoded input.
        ({"path": "/etc/hosts", "mode": "r"}, '"path": "/etc/hosts"'),
        ({}, "{}"),
        # Blank command falls through to the JSON encoding.
        ({"command": "   "}, '"command"'),
        # No usable input → omitted (None), never the bare tool name.
        (None, None),
        ("not a dict", None),
    ],
)
def test_content_preview_renders_or_omits(tool_input: object, expected: str | None) -> None:
    result = kimi_native_hook._content_preview(tool_input)
    if expected is None:
        assert result is None
    else:
        assert result is not None and expected in result


def test_content_preview_truncates_to_limit() -> None:
    long_command = "a" * (kimi_native_hook._PREVIEW_MAX + 500)
    result = kimi_native_hook._content_preview({"command": long_command})
    assert result is not None and len(result) == kimi_native_hook._PREVIEW_MAX


def test_content_preview_omits_non_serializable_tool_input() -> None:
    assert kimi_native_hook._content_preview({"blob": object()}) is None


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


def test_permission_request_reparks_after_injection_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge_dir = _governed_bridge(tmp_path)
    _feed_stdin(monkeypatch, {"hook_event_name": "PermissionRequest", "tool_name": "Bash"})
    verdicts = iter(["accept", "cancel"])
    posted: list[dict[str, object]] = []
    deadlines: list[object] = []
    keys: list[str] = []

    def _request(
        url: str, headers: dict[str, str], body: dict[str, object], **kwargs: object
    ) -> str:
        del url, headers
        posted.append(body.copy())
        deadlines.append(kwargs.get("deadline"))
        return next(verdicts)

    def _inject(bridge_dir: Path, *, key: str, timeout_s: float) -> bool:
        del bridge_dir, timeout_s
        keys.append(key)
        if len(keys) == 1:
            raise KimiApprovalPromptNotFoundError("menu moved")
        return True

    monkeypatch.setattr(kimi_native_hook, "_request_web_approval", _request)
    monkeypatch.setattr(kimi_native_hook, "inject_approval_keystroke", _inject)
    monkeypatch.setattr(kimi_native_hook, "_approval_still_pending", lambda _bridge: True)
    monkeypatch.setattr(kimi_native_hook.time, "monotonic", lambda: 100.0)

    assert kimi_native_hook.main(["permission-request", "--bridge-dir", str(bridge_dir)]) == 0
    assert keys == [APPROVE_KEY, DENY_KEY]
    assert len(posted) == 2
    assert posted[0]["elicitation_id"] == posted[1]["elicitation_id"]
    assert deadlines == [
        100.0 + kimi_native_hook._PERMISSION_RETRY_WINDOW_S,
        100.0 + kimi_native_hook._PERMISSION_RETRY_WINDOW_S,
    ]


def test_permission_request_does_not_repark_ambiguous_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge_dir = _governed_bridge(tmp_path)
    _feed_stdin(monkeypatch, {"hook_event_name": "PermissionRequest", "tool_name": "Bash"})
    requests: list[dict[str, object]] = []
    monkeypatch.setattr(
        kimi_native_hook,
        "_request_web_approval",
        lambda url, headers, body, **kwargs: requests.append(body.copy()) or "accept",
    )
    keys: list[str] = []

    def _inject(bridge_dir: Path, *, key: str, timeout_s: float) -> bool:
        del bridge_dir, timeout_s
        keys.append(key)
        raise KimiApprovalPromptAmbiguousError("same menu remained")

    monkeypatch.setattr(kimi_native_hook, "inject_approval_keystroke", _inject)
    monkeypatch.setattr(
        kimi_native_hook,
        "_approval_still_pending",
        lambda _bridge: (_ for _ in ()).throw(AssertionError("must not re-park")),
    )

    assert kimi_native_hook.main(["permission-request", "--bridge-dir", str(bridge_dir)]) == 0
    assert len(requests) == 1
    assert keys == [APPROVE_KEY]


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
        # The action is returned verbatim so the caller can treat decline
        # (server-Escaped) differently from cancel.
        ({"action": "accept"}, "accept"),
        ({"action": "decline"}, "decline"),
        ({"action": "cancel"}, "cancel"),
        ({}, None),
        ({"action": "huh"}, None),
        ({"action": None}, None),
        # The old Claude PermissionRequest shape must NOT resolve to a verdict.
        ({"hookSpecificOutput": {"decision": {"behavior": "allow"}}}, None),
        ("not a dict", None),
    ],
)
def test_verdict_from_response(response: object, expected: str | None) -> None:
    assert kimi_native_hook._verdict_from_response(response) == expected


def _mock_http_action(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    """Patch httpx so the REAL verdict parse runs over ``{"action": action}``."""

    class _Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, url: str, *, json: dict[str, object]) -> httpx.Response:
            del url, json
            return httpx.Response(
                200,
                json={"action": action},
                request=httpx.Request("POST", "http://server"),
            )

    monkeypatch.setattr(kimi_native_hook.httpx, "Client", _Client)


@pytest.mark.parametrize(
    ("action", "expected_keys"),
    [
        # accept types "approve once"; cancel (no server interrupt) types
        # "reject"; an explicit decline injects nothing (server Escape rejects).
        ("accept", [APPROVE_KEY]),
        ("cancel", [DENY_KEY]),
        ("decline", []),
    ],
)
def test_permission_request_keystroke_matches_real_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    expected_keys: list[str],
) -> None:
    """End-to-end: the real ElicitationResult parse drives the keystroke.

    Only the pty inject and the HTTP transport are stubbed — ``_request_web_approval``
    and ``_verdict_from_response`` run for real, so inverting or collapsing the
    decline/cancel discrimination fails this.
    """
    bridge_dir = _governed_bridge(tmp_path)
    _feed_stdin(monkeypatch, {"hook_event_name": "PermissionRequest", "tool_name": "Bash"})
    _mock_http_action(monkeypatch, action)
    keys = _capture_injection(monkeypatch)

    assert kimi_native_hook.main(["permission-request", "--bridge-dir", str(bridge_dir)]) == 0
    assert keys == expected_keys


def test_unknown_subcommand_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert kimi_native_hook.main(["bogus", "--bridge-dir", "/tmp/x"]) == 2


def test_request_web_approval_reparks_after_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        httpx.ReadTimeout("poll expired", request=httpx.Request("POST", "http://server")),
        httpx.Response(
            200,
            json={"action": "accept"},
            request=httpx.Request("POST", "http://server"),
        ),
    ]
    requests: list[dict[str, object]] = []

    class _Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, url: str, *, json: dict[str, object]) -> httpx.Response:
            requests.append({"url": url, "json": json})
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

    monkeypatch.setattr(kimi_native_hook.httpx, "Client", _Client)

    body = {"elicitation_id": "elicit_kimi_0123456789abcdef0123456789abcdef"}
    assert kimi_native_hook._request_web_approval("http://server", {}, body) == "accept"
    assert len(requests) == 2
    assert requests[0]["json"] == requests[1]["json"] == body


def test_request_web_approval_reparks_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        httpx.Response(200, content=b"", request=httpx.Request("POST", "http://server")),
        httpx.Response(
            200,
            json={"action": "accept"},
            request=httpx.Request("POST", "http://server"),
        ),
    ]
    requests: list[dict[str, object]] = []

    class _Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, url: str, *, json: dict[str, object]) -> httpx.Response:
            requests.append({"url": url, "json": json})
            return responses.pop(0)

    monkeypatch.setattr(kimi_native_hook.httpx, "Client", _Client)

    body = {"elicitation_id": "elicit_kimi_0123456789abcdef0123456789abcdef"}
    assert kimi_native_hook._request_web_approval("http://server", {}, body) == "accept"
    assert len(requests) == 2
    assert requests[0]["json"] == requests[1]["json"] == body


def test_request_web_approval_does_not_repark_after_menu_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    response = httpx.Response(200, content=b"", request=httpx.Request("POST", "http://server"))
    requests: list[dict[str, object]] = []
    probes: list[Path] = []

    class _Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, url: str, *, json: dict[str, object]) -> httpx.Response:
            requests.append({"url": url, "json": json})
            return response

    monkeypatch.setattr(kimi_native_hook.httpx, "Client", _Client)
    monkeypatch.setattr(
        kimi_native_hook,
        "approval_prompt_visible",
        lambda bridge_dir: probes.append(bridge_dir) or False,
    )

    body = {"elicitation_id": "elicit_kimi_0123456789abcdef0123456789abcdef"}
    assert (
        kimi_native_hook._request_web_approval("http://server", {}, body, bridge_dir=tmp_path)
        is None
    )
    assert len(requests) == 1
    assert probes == [tmp_path, tmp_path]


def test_permission_read_timeout_leaves_global_deadline_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeouts: list[httpx.Timeout] = []

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            timeouts.append(kwargs["timeout"])

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def post(self, url: str, *, json: dict[str, object]) -> httpx.Response:
            return httpx.Response(
                200,
                json={"action": "accept"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(kimi_native_hook.httpx, "Client", _Client)
    monkeypatch.setattr(kimi_native_hook.time, "monotonic", lambda: 0.0)

    body = {"elicitation_id": "elicit_kimi_0123456789abcdef0123456789abcdef"}
    assert kimi_native_hook._request_web_approval("http://server", {}, body) == "accept"
    request_budget = (
        kimi_native_hook._PERMISSION_RETRY_WINDOW_S
        - kimi_native_hook._PERMISSION_DEADLINE_MARGIN_S
    )
    assert timeouts[0].connect is not None
    assert timeouts[0].read is not None
    assert timeouts[0].connect + timeouts[0].read <= request_budget


def test_permission_poll_budget_is_below_kimi_hook_ceiling() -> None:
    assert (
        kimi_native_hook._PERMISSION_RETRY_WINDOW_S
        + kimi_native_hook._SURFACE_TIMEOUT_S
        + kimi_native_hook._APPROVAL_SURFACE_BUDGET_S
        < kimi_native_hook._KIMI_HOOK_TIMEOUT_S
    )
