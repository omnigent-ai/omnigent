"""Tests for the codex-native policy hook entrypoint (``evaluate-policy``)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import httpx
import pytest

from omnigent import codex_native_hook, native_policy_hook
from omnigent.codex_native_bridge import (
    CodexNativeBridgeState,
    codex_home_for_bridge_dir,
    prepare_bridge_dir,
    write_bridge_state,
    write_policy_hook_config,
)
from tests.native_hook_helpers import make_failing_client


class _DenyHttpxClient:
    """
    Sync HTTP client stub that records the request and returns a DENY verdict.

    Returns a real :class:`httpx.Response` so the hook exercises its real
    JSON parsing + verdict-mapping path rather than a mock's attributes.

    :param headers: Headers passed to :class:`httpx.Client`.
    :param timeout: Timeout passed to :class:`httpx.Client`.
    """

    captured: dict[str, object] = {}

    def __init__(self, *, headers: dict[str, str], timeout: object) -> None:
        """
        Capture constructor inputs for later assertions.

        :param headers: HTTP headers the hook builds for AP.
        :param timeout: HTTP timeout object.
        :returns: None.
        """
        _DenyHttpxClient.captured["headers"] = headers

    def __enter__(self) -> _DenyHttpxClient:
        """
        Enter the context manager.

        :returns: This stub client.
        """
        return self

    def __exit__(self, *args: object) -> None:
        """
        Exit the context manager.

        :param args: Exception details (unused).
        :returns: None.
        """
        del args

    def post(self, url: str, *, json: dict[str, object]) -> httpx.Response:
        """
        Record the outgoing request and return a DENY EvaluationResponse.

        :param url: Target Omnigent URL.
        :param json: Request body (the EvaluationRequest).
        :returns: A real 200 response carrying a DENY verdict.
        """
        _DenyHttpxClient.captured["url"] = url
        _DenyHttpxClient.captured["json"] = json
        return httpx.Response(
            200,
            text='{"result":"POLICY_ACTION_DENY","reason":"rm blocked by admin policy"}',
            request=httpx.Request("POST", url),
        )


class _RaisesIfCalled:
    """
    HTTP client stub that fails the test if the hook ever POSTs.

    Used by fail-open tests where the hook must short-circuit (missing
    bridge state or policy config) before reaching the network.

    :param headers: Headers passed to :class:`httpx.Client` (unused).
    :param timeout: Timeout passed to :class:`httpx.Client` (unused).
    """

    def __init__(self, *, headers: dict[str, str], timeout: object) -> None:
        """
        Accept the constructor shape; do nothing.

        :param headers: HTTP headers (unused).
        :param timeout: HTTP timeout (unused).
        :returns: None.
        """
        del headers, timeout

    def __enter__(self) -> _RaisesIfCalled:
        """
        Enter the context manager.

        :returns: This stub client.
        """
        return self

    def __exit__(self, *args: object) -> None:
        """
        Exit the context manager.

        :param args: Exception details (unused).
        :returns: None.
        """
        del args

    def post(self, url: str, *, json: dict[str, object]) -> httpx.Response:
        """
        Fail loudly — the hook should never reach the network here.

        :param url: Target Omnigent URL (unused).
        :param json: Request body (unused).
        :returns: Never returns.
        :raises AssertionError: Always.
        """
        del url, json
        raise AssertionError(
            "evaluate-policy POSTed to Omnigent when it should have short-circuited "
            "(missing bridge state or policy_hook config)."
        )


@pytest.fixture
def bridge_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Create an isolated codex-native bridge directory with state.

    Redirects the bridge root under ``tmp_path`` so the test never
    touches the real ``~/.omnigent`` tree, then writes a valid bridge
    state whose ``session_id`` the hook reads to build the Omnigent URL.

    :param tmp_path: pytest temp directory.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: Prepared bridge directory.
    """
    monkeypatch.setattr("omnigent.codex_native_bridge._BRIDGE_ROOT", tmp_path / "codex-native")
    bdir = prepare_bridge_dir("bridge_test")
    write_bridge_state(
        bdir,
        CodexNativeBridgeState(
            session_id="conv_active",
            socket_path=str(bdir / "app-server.sock"),
            thread_id="thread_abc",
            codex_home=str(bdir / "codex-home"),
        ),
    )
    return bdir


def _run_hook(
    bridge_dir: Path, payload: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> int:
    """
    Feed *payload* on stdin and run the ``evaluate-policy`` subcommand.

    :param bridge_dir: The session's bridge directory.
    :param payload: The codex hook JSON payload, e.g.
        ``{"hook_event_name": "PreToolUse", "tool_name": "Bash", ...}``.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: The hook process exit code.
    """
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return codex_native_hook.main(["evaluate-policy", "--bridge-dir", str(bridge_dir)])


def test_pre_tool_use_converts_posts_and_returns_deny(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    A PreToolUse hook converts to proto, POSTs to AP, and maps DENY back.

    This is the full codex enforcement path: read bridge state +
    policy_hook config → convert payload → POST /policies/evaluate →
    map the DENY verdict to ``permissionDecision: deny``. It fails if any
    link breaks (wrong URL/session, missing conversion, missing auth, or
    a mis-mapped verdict that would let the blocked command run).
    """
    _DenyHttpxClient.captured = {}
    write_policy_hook_config(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8787",
        ap_auth_headers={"Authorization": "Bearer test-token"},
    )
    monkeypatch.setattr(native_policy_hook.httpx, "Client", _DenyHttpxClient)

    exit_code = _run_hook(
        bridge_dir,
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /"},
        },
        monkeypatch,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    # URL is built from the bridge state's session_id, not the payload.
    assert _DenyHttpxClient.captured["url"] == (
        "http://127.0.0.1:8787/v1/sessions/conv_active/policies/evaluate"
    )
    # The codex payload is converted to the proto EvaluationRequest shape.
    sent = _DenyHttpxClient.captured["json"]
    assert sent["event"]["type"] == "PHASE_TOOL_CALL"
    assert sent["event"]["data"] == {"name": "Bash", "arguments": {"command": "rm -rf /"}}
    # Auth headers from policy_hook.json reach AP.
    assert _DenyHttpxClient.captured["headers"] == {"Authorization": "Bearer test-token"}
    # The DENY verdict maps back to codex's PreToolUse deny output.
    result = json.loads(captured.out)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert result["hookSpecificOutput"]["permissionDecisionReason"] == "rm blocked by admin policy"
    assert captured.err == ""


def test_user_prompt_submit_converts_posts_and_blocks(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    A UserPromptSubmit hook converts to PHASE_REQUEST and maps DENY to block.

    This is the request-phase enforcement path for native Codex sessions
    (the server-level ``_evaluate_input_policy`` skips native message
    events). The prompt rides in ``event.data.text``; a DENY maps to the
    top-level ``decision: "block"`` contract — NOT ``permissionDecision`` —
    which drops the prompt before the model sees it. A break here means a
    blocked prompt would still reach the model.
    """
    _DenyHttpxClient.captured = {}
    write_policy_hook_config(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8787",
        ap_auth_headers={"Authorization": "Bearer test-token"},
    )
    monkeypatch.setattr(native_policy_hook.httpx, "Client", _DenyHttpxClient)

    exit_code = _run_hook(
        bridge_dir,
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "delete the prod database",
        },
        monkeypatch,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    sent = _DenyHttpxClient.captured["json"]
    assert sent["event"]["type"] == "PHASE_REQUEST"
    assert sent["event"]["data"] == {"text": "delete the prod database"}
    # DENY → top-level decision/reason block (not permissionDecision).
    result = json.loads(captured.out)
    assert result == {"decision": "block", "reason": "rm blocked by admin policy"}
    assert captured.err == ""


def test_pre_tool_use_stamps_model_from_config(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The hook stamps ``config.toml``'s model onto the request context.

    This is the race-free model source for the codex cost gate: the hook
    reads the user's live ``/model`` selection from ``config.toml`` at gate
    time and puts it on ``event.context.model`` so the server evaluates
    against it (preferred over the engine's resolved model). If this
    regresses, a terminal ``/model`` downgrade never reaches the gate and
    the session stays wrongly blocked.
    """
    _DenyHttpxClient.captured = {}
    home = codex_home_for_bridge_dir(bridge_dir)
    home.mkdir(parents=True, exist_ok=True)
    # The current /model selection, as codex persists it to config.toml.
    (home / "config.toml").write_text('model = "gpt-5.4"\n')
    write_policy_hook_config(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8787",
        ap_auth_headers={"Authorization": "Bearer test-token"},
    )
    monkeypatch.setattr(native_policy_hook.httpx, "Client", _DenyHttpxClient)

    exit_code = _run_hook(
        bridge_dir,
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}},
        monkeypatch,
    )

    assert exit_code == 0
    sent = _DenyHttpxClient.captured["json"]
    # The live model is carried in the request so the gate sees gpt-5.4.
    assert sent["event"]["context"]["model"] == "gpt-5.4"
    # The harness is stamped so the gate can tailor messages to codex.
    assert sent["event"]["context"]["harness"] == "codex-native"


def test_pre_tool_use_stamps_harness_without_config_model(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harness is stamped even when config.toml has no model.

    The harness drives the deny message's switch-instruction wording, which
    must be correct regardless of whether the model is determinable — so it
    is stamped unconditionally (unlike the model, which is only stamped when
    config.toml provides one).
    """
    _DenyHttpxClient.captured = {}
    # No config.toml written → read_codex_config_model returns None.
    write_policy_hook_config(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8787",
        ap_auth_headers={"Authorization": "Bearer test-token"},
    )
    monkeypatch.setattr(native_policy_hook.httpx, "Client", _DenyHttpxClient)

    exit_code = _run_hook(
        bridge_dir,
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}},
        monkeypatch,
    )

    assert exit_code == 0
    sent = _DenyHttpxClient.captured["json"]
    assert sent["event"]["context"]["harness"] == "codex-native"
    # Model absent (no config) — stays unstamped, the gate falls back.
    assert "model" not in sent["event"]["context"]


def test_missing_bridge_state_is_fail_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    With no bridge state, the hook emits nothing and never POSTs.

    A bridge dir that has not been initialized must not crash codex or
    block tools — the hook returns 0 with no verdict. ``_RaisesIfCalled``
    asserts the network was never reached.
    """
    monkeypatch.setattr("omnigent.codex_native_bridge._BRIDGE_ROOT", tmp_path / "codex-native")
    empty_dir = prepare_bridge_dir("bridge_no_state")
    monkeypatch.setattr(native_policy_hook.httpx, "Client", _RaisesIfCalled)

    exit_code = _run_hook(
        empty_dir,
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}},
        monkeypatch,
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    # No verdict emitted → codex applies its own default (fail-open).
    assert captured.out == ""


def test_missing_policy_config_is_fail_open(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    With bridge state but no policy_hook config, the hook never POSTs.

    The session has state but no Omnigent coordinates were written (e.g. a
    local run with no Omnigent server), so there is nothing to enforce against.
    The hook returns 0 with no output and does not touch the network.
    """
    monkeypatch.setattr(native_policy_hook.httpx, "Client", _RaisesIfCalled)

    exit_code = _run_hook(
        bridge_dir,
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}},
        monkeypatch,
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""


@pytest.mark.parametrize("mode", ["connect_error", "non_2xx", "empty_body", "malformed_json"])
def test_pre_tool_use_fails_closed_when_verdict_unavailable(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    """
    A governed PreToolUse call denies when no usable verdict is returned.

    For native harnesses this hook is the sole TOOL_CALL enforcement point,
    so a server outage / non-2xx / empty / malformed response must fail
    CLOSED (deny) instead of "no opinion" — the bypass reported in #536.
    """
    write_policy_hook_config(bridge_dir, ap_server_url="http://127.0.0.1:8787", ap_auth_headers={})
    monkeypatch.setattr(native_policy_hook, "_EVALUATE_POLICY_RETRY_BUDGET_S", 0.0)
    monkeypatch.setattr(native_policy_hook.httpx, "Client", make_failing_client(mode))

    exit_code = _run_hook(
        bridge_dir,
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /"},
        },
        monkeypatch,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    result = json.loads(captured.out)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny", result
    assert result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize(
    "payload",
    [
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_output": "ok",
        },
        {"hook_event_name": "UserPromptSubmit", "prompt": "hello"},
    ],
)
def test_non_tool_call_phases_fail_open_on_error(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> None:
    """
    Off the tool-call gate, an unobtainable verdict stays fail-open.

    PostToolUse runs after the tool executed and the request gate is
    advisory, so neither denies on a transport error — mirroring the
    runner-side ``FAIL_CLOSED_PHASES`` (PR #163).
    """
    write_policy_hook_config(bridge_dir, ap_server_url="http://127.0.0.1:8787", ap_auth_headers={})
    monkeypatch.setattr(native_policy_hook, "_EVALUATE_POLICY_RETRY_BUDGET_S", 0.0)
    monkeypatch.setattr(native_policy_hook.httpx, "Client", make_failing_client("connect_error"))

    exit_code = _run_hook(bridge_dir, payload, monkeypatch)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
