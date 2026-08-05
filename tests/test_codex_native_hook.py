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


def test_user_prompt_submit_fails_closed_on_error(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    A governed UserPromptSubmit blocks when no usable verdict is returned.

    The request gate is the sole pre-turn enforcement point for native
    sessions — a server outage must not let a blocked request proceed.
    """
    write_policy_hook_config(bridge_dir, ap_server_url="http://127.0.0.1:8787", ap_auth_headers={})
    monkeypatch.setattr(native_policy_hook, "_EVALUATE_POLICY_RETRY_BUDGET_S", 0.0)
    monkeypatch.setattr(native_policy_hook.httpx, "Client", make_failing_client("connect_error"))

    payload: dict[str, object] = {"hook_event_name": "UserPromptSubmit", "prompt": "hello"}
    exit_code = _run_hook(bridge_dir, payload, monkeypatch)

    captured = capsys.readouterr()
    assert exit_code == 0
    result = json.loads(captured.out)
    assert result["decision"] == "block"
    assert result["reason"]


def test_post_tool_use_fails_open_on_error(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    PostToolUse fails OPEN on a transport error — the tool already ran.

    Mirroring the runner-side ``FAIL_CLOSED_PHASES``.
    """
    write_policy_hook_config(bridge_dir, ap_server_url="http://127.0.0.1:8787", ap_auth_headers={})
    monkeypatch.setattr(native_policy_hook, "_EVALUATE_POLICY_RETRY_BUDGET_S", 0.0)
    monkeypatch.setattr(native_policy_hook.httpx, "Client", make_failing_client("connect_error"))
    payload: dict[str, object] = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "tool_output": "ok",
    }

    exit_code = _run_hook(bridge_dir, payload, monkeypatch)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""


def test_pre_tool_use_uses_relay_when_tool_relay_json_has_session_id(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Hook POSTs to relay /policies/evaluate when tool_relay.json has session_id."""
    from omnigent.claude_native_bridge import _TOOL_RELAY_FILE

    relay_token = "relay-tok-abc"
    relay_url = "http://127.0.0.1:19999"
    (bridge_dir / _TOOL_RELAY_FILE).write_text(
        json.dumps({"url": relay_url, "token": relay_token, "session_id": "conv_active"})
    )
    _DenyHttpxClient.captured = {}
    monkeypatch.setattr(native_policy_hook.httpx, "Client", _DenyHttpxClient)

    exit_code = _run_hook(
        bridge_dir,
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}},
        monkeypatch,
    )

    assert exit_code == 0
    # Route is relay, not direct server.
    assert _DenyHttpxClient.captured["url"] == f"{relay_url}/policies/evaluate"
    # Auth is relay token, not a server bearer.
    assert _DenyHttpxClient.captured["headers"] == {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {relay_token}",
    }
    # Verdict still applied.
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pre_tool_use_falls_back_to_policy_hook_json_when_relay_has_no_session_id(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Hook falls back to policy_hook.json when tool_relay.json has no session_id."""
    from omnigent.claude_native_bridge import _TOOL_RELAY_FILE

    # Relay present but no session_id — not policy-capable.
    (bridge_dir / _TOOL_RELAY_FILE).write_text(
        json.dumps({"url": "http://127.0.0.1:19999", "token": "tok"})
    )
    write_policy_hook_config(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8787",
        ap_auth_headers={"Authorization": "Bearer direct-token"},
    )
    _DenyHttpxClient.captured = {}
    monkeypatch.setattr(native_policy_hook.httpx, "Client", _DenyHttpxClient)

    _run_hook(
        bridge_dir,
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}},
        monkeypatch,
    )

    # Falls back to direct server URL.
    assert _DenyHttpxClient.captured["url"] == (
        "http://127.0.0.1:8787/v1/sessions/conv_active/policies/evaluate"
    )
    assert _DenyHttpxClient.captured["headers"] == {"Authorization": "Bearer direct-token"}


# ── route-turn (first-message model routing) ────────────────────────


def _advertise_turn_router(bridge_dir: Path) -> None:
    """
    Write a live ``turn_router.json`` advertisement into *bridge_dir*.

    :param bridge_dir: The session's bridge directory.
    :returns: None.
    """
    import os

    from omnigent.runner.turn_routing import ADVERTISEMENT_FILE

    (bridge_dir / ADVERTISEMENT_FILE).write_text(
        json.dumps(
            {
                "url": "http://127.0.0.1:54321",
                "token": "turn-token",
                "pid": os.getpid(),
                "session_id": "conv_active",
            }
        ),
        encoding="utf-8",
    )


def _run_route_turn(
    bridge_dir: Path, payload: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> int:
    """
    Feed *payload* on stdin and run the ``route-turn`` subcommand.

    :param bridge_dir: The session's bridge directory.
    :param payload: The codex ``UserPromptSubmit`` hook payload.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: The hook process exit code.
    """
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return codex_native_hook.main(
        ["route-turn", "--bridge-dir", str(bridge_dir), "--harness", "codex-native"]
    )


def test_route_turn_fast_skips_on_the_marker(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    A consumed marker means no output and no network at all.

    This is the re-entrancy guard the replayed prompt hits: the replay
    re-fires ``UserPromptSubmit``, and a second block there would drop the
    routed turn.
    """
    from omnigent.runner.turn_routing import write_turn_routing_marker

    _advertise_turn_router(bridge_dir)
    write_turn_routing_marker(bridge_dir, session_id="conv_active", decision_id="d1")
    monkeypatch.setattr(
        codex_native_hook,
        "_post_json",
        lambda *args, **kwargs: pytest.fail("the marker must skip the network"),
    )

    exit_code = _run_route_turn(
        bridge_dir, {"prompt": "hello", "model": "gpt-5.6-sol"}, monkeypatch
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""


def test_route_turn_blocks_and_switches_on_a_routed_verdict(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    A routed verdict switches the thread, writes the marker, then blocks.

    Order is load-bearing: the marker is the runner's "I blocked it, you
    owe it a replay" handshake, so it must be on disk before the block.
    """
    from omnigent.runner.turn_routing import MARKER_FILE

    _advertise_turn_router(bridge_dir)
    sent: dict[str, object] = {}
    switched: list[str] = []

    def _post(url: str, token: str, body: dict[str, object], timeout: float) -> dict[str, object]:
        sent.update({"url": url, "token": token, "body": body, "timeout": timeout})
        return {
            "action": "route",
            "model": "gpt-5.6-luna",
            "rationale": "short lookup",
            "terminal": True,
        }

    def _switch(bdir: Path, model: str) -> bool:
        # The marker is only written after the switch is accepted.
        assert not (bdir / MARKER_FILE).exists()
        switched.append(model)
        return True

    monkeypatch.setattr(codex_native_hook, "_post_json", _post)
    monkeypatch.setattr(codex_native_hook, "_apply_thread_model", _switch)

    exit_code = _run_route_turn(
        bridge_dir,
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "what testing framework does this project use?",
            "turn_id": "turn_1",
            "model": "gpt-5.6-sol",
        },
        monkeypatch,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert sent["url"] == "http://127.0.0.1:54321/v1/sessions/conv_active/route-turn"
    assert sent["token"] == "turn-token"
    assert sent["body"] == {
        "harness": "codex-native",
        "prompt": "what testing framework does this project use?",
        "turn_id": "turn_1",
        # The live model comes from the payload; config.toml is stale.
        "model": "gpt-5.6-sol",
    }
    assert switched == ["gpt-5.6-luna"]
    assert (bridge_dir / MARKER_FILE).exists()
    result = json.loads(captured.out)
    assert result["decision"] == "block"
    assert "gpt-5.6-luna" in result["reason"]


def test_route_turn_allows_and_marks_an_already_pinned_session(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A terminal no-op writes the marker so later prompts skip the hop."""
    from omnigent.runner.turn_routing import MARKER_FILE

    _advertise_turn_router(bridge_dir)
    monkeypatch.setattr(
        codex_native_hook,
        "_post_json",
        lambda *args, **kwargs: {
            "action": "allow",
            "rationale": "already pinned",
            "terminal": True,
        },
    )
    monkeypatch.setattr(
        codex_native_hook,
        "_apply_thread_model",
        lambda *args: pytest.fail("an allow verdict must not switch the model"),
    )

    exit_code = _run_route_turn(bridge_dir, {"prompt": "hello"}, monkeypatch)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert (bridge_dir / MARKER_FILE).exists()


def test_route_turn_keeps_asking_after_a_non_terminal_no_op(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Routing can be toggled on mid-session, so no marker is written."""
    from omnigent.runner.turn_routing import MARKER_FILE

    _advertise_turn_router(bridge_dir)
    monkeypatch.setattr(
        codex_native_hook,
        "_post_json",
        lambda *args, **kwargs: {"action": "allow", "rationale": "routing off"},
    )

    exit_code = _run_route_turn(bridge_dir, {"prompt": "hello"}, monkeypatch)

    assert exit_code == 0
    assert capsys.readouterr().out == ""
    assert not (bridge_dir / MARKER_FILE).exists()


def test_route_turn_allows_the_prompt_when_the_switch_fails(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    No block without an applied switch — otherwise the prompt is lost.

    The runner's replay only fires when the marker appears, so a hook that
    blocked without switching would drop the user's message entirely.
    """
    from omnigent.runner.turn_routing import MARKER_FILE

    _advertise_turn_router(bridge_dir)
    monkeypatch.setattr(
        codex_native_hook,
        "_post_json",
        lambda *args, **kwargs: {
            "action": "route",
            "model": "gpt-5.6-luna",
            "rationale": "x",
            "terminal": True,
        },
    )
    monkeypatch.setattr(codex_native_hook, "_apply_thread_model", lambda *args: False)

    exit_code = _run_route_turn(bridge_dir, {"prompt": "hello"}, monkeypatch)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert "could not switch" in captured.err
    # No marker: it is the replay handshake, and this prompt is running.
    assert not (bridge_dir / MARKER_FILE).exists()


def test_route_turn_no_ops_without_an_advertisement(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = _run_route_turn(bridge_dir, {"prompt": "hello"}, monkeypatch)
    assert exit_code == 0
    assert capsys.readouterr().out == ""


def test_route_turn_no_ops_on_an_empty_prompt(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _advertise_turn_router(bridge_dir)
    monkeypatch.setattr(
        codex_native_hook,
        "_post_json",
        lambda *args, **kwargs: pytest.fail("an empty prompt must not reach the router"),
    )
    assert _run_route_turn(bridge_dir, {"prompt": "   "}, monkeypatch) == 0
    assert capsys.readouterr().out == ""


def test_route_turn_no_ops_when_the_endpoint_is_unreachable(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A routing outage must never block a user's turn."""
    from omnigent.runner.turn_routing import MARKER_FILE

    _advertise_turn_router(bridge_dir)
    monkeypatch.setattr(codex_native_hook, "_post_json", lambda *args, **kwargs: None)

    assert _run_route_turn(bridge_dir, {"prompt": "hello"}, monkeypatch) == 0
    assert capsys.readouterr().out == ""
    assert not (bridge_dir / MARKER_FILE).exists()


def test_route_turn_falls_open_on_the_ladders_own_request_budget(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    Codex's hook waits ``HOOK_REQUEST_TIMEOUT_S`` for a verdict, and no longer.

    Same hazard as claude's: the typed prompt is held in the TUI until this
    expires. Asserted against the constant, not elapsed time.
    """
    from omnigent.runner.turn_routing import HOOK_REQUEST_TIMEOUT_S, MARKER_FILE

    _advertise_turn_router(bridge_dir)
    seen: list[float] = []

    def _timed_out(url: str, token: str, body: object, timeout: float) -> None:
        del url, token, body
        seen.append(timeout)
        return

    monkeypatch.setattr(codex_native_hook, "_post_json", _timed_out)

    assert _run_route_turn(bridge_dir, {"prompt": "hello"}, monkeypatch) == 0
    assert seen == [HOOK_REQUEST_TIMEOUT_S]
    assert HOOK_REQUEST_TIMEOUT_S < 10.0
    assert capsys.readouterr().out == ""
    assert not (bridge_dir / MARKER_FILE).exists()


def test_the_thread_switch_is_capped_inside_the_harness_hook_budget() -> None:
    """
    Hop 2b sits inside hop 1 alongside hop 2a, with room to spare.

    Codex's hook does two things after being invoked — ask for a verdict, then
    switch the thread — and the harness kills it on one budget. If the two
    inner budgets could together exceed the outer one, a slow switch would be
    killed mid-``thread/settings/update``: the block marker is written after
    the switch, so the harness would drop the prompt with nothing to replay it.
    """
    from omnigent.runner.turn_routing import (
        HARNESS_HOOK_TIMEOUT_S,
        HOOK_REQUEST_TIMEOUT_S,
        SETTINGS_UPDATE_TIMEOUT_S,
    )

    assert HARNESS_HOOK_TIMEOUT_S > HOOK_REQUEST_TIMEOUT_S + SETTINGS_UPDATE_TIMEOUT_S
    # A local app-server RPC over a unix socket, so seconds is generous.
    assert SETTINGS_UPDATE_TIMEOUT_S < 10.0


@pytest.mark.parametrize(
    "advertise",
    [False, True],
    ids=["no_advertisement", "unreachable_endpoint"],
)
def test_route_turn_traces_every_fall_open(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    advertise: bool,
) -> None:
    """A session that did not route always records which gate stopped it.

    Without this the two ways first-message routing goes quiet — the hook
    fell open, or the harness never fired it at all — are indistinguishable
    from the logs, which is exactly how a reported "it never routed" ends
    up unattributable.
    """
    from omnigent.runner.turn_routing import TRACE_FILE

    if advertise:
        _advertise_turn_router(bridge_dir)
        monkeypatch.setattr(codex_native_hook, "_post_json", lambda *args, **kwargs: None)

    assert _run_route_turn(bridge_dir, {"prompt": "hello"}, monkeypatch) == 0

    traced = [
        json.loads(line)
        for line in (bridge_dir / TRACE_FILE).read_text(encoding="utf-8").splitlines()
    ]
    assert [entry["outcome"] for entry in traced] == ["fail-open"]
    assert traced[0]["detail"]


def test_route_turn_traces_the_route_it_applied(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from omnigent.runner.turn_routing import TRACE_FILE

    _advertise_turn_router(bridge_dir)
    monkeypatch.setattr(
        codex_native_hook,
        "_post_json",
        lambda *args, **kwargs: {
            "action": "route",
            "model": "gpt-5.6-luna",
            "terminal": True,
        },
    )
    monkeypatch.setattr(codex_native_hook, "_apply_thread_model", lambda *args: True)

    assert _run_route_turn(bridge_dir, {"prompt": "hello"}, monkeypatch) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "block"
    traced = [
        json.loads(line)
        for line in (bridge_dir / TRACE_FILE).read_text(encoding="utf-8").splitlines()
    ]
    assert [entry["outcome"] for entry in traced] == ["route"]
    assert "gpt-5.6-luna" in traced[0]["detail"]


# ── the actuator: what spelling reaches thread/settings/update ───────


class _FakeAppServerClient:
    """
    App-server client stub scripting ``model/list`` and recording requests.

    :param catalog: ``model/list`` rows to serve, or ``None`` to make the
        call raise (an unreadable catalog).
    """

    def __init__(self, catalog: list[dict[str, object]] | None) -> None:
        """
        Build the stub.

        :param catalog: Rows to serve, or ``None`` to fail the call.
        :returns: None.
        """
        self._catalog = catalog
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    async def connect(self) -> None:
        """
        Pretend to dial the app-server.

        :returns: None.
        """

    async def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        """
        Record one request and answer it.

        :param method: App-server method name.
        :param params: Method parameters.
        :returns: A JSON-RPC response envelope.
        """
        self.requests.append((method, params))
        if method == "model/list":
            if self._catalog is None:
                raise RuntimeError("app-server refused model/list")
            return {"result": {"data": self._catalog, "nextCursor": None}}
        return {"result": {}}

    async def close(self) -> None:
        """
        Record the close.

        :returns: None.
        """
        self.closed = True


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    client: _FakeAppServerClient,
) -> _FakeAppServerClient:
    """
    Route the hook's app-server connect at a stub.

    :param monkeypatch: pytest monkeypatch fixture.
    :param client: Stub to hand back from ``client_for_transport``.
    :returns: The stub the hook will use.
    """
    monkeypatch.setattr(
        "omnigent.codex_native_app_server.client_for_transport",
        lambda *args, **kwargs: client,
    )
    return client


# A live databricks-gateway ``model/list`` response.
_LIVE_CATALOG: list[dict[str, object]] = [
    {"id": "gpt-5.6-sol", "model": "gpt-5.6-sol", "isDefault": True},
    {"id": "gpt-5.6-luna", "model": "gpt-5.6-luna"},
    {"id": "system.ai.glm-5-2", "model": "system.ai.glm-5-2"},
]


@pytest.mark.parametrize(
    ("routed", "applied"),
    [
        # The routed arm arrives as a catalog id; codex only has metadata for
        # its own dotted slug, and ``/model`` only highlights that one.
        ("databricks-gpt-5-6-luna", "gpt-5.6-luna"),
        # Extended-catalog rows are listed under the catalog spelling, which
        # IS codex's id for them — translating must not mangle it.
        ("system.ai.glm-5-2", "system.ai.glm-5-2"),
        # Nothing in the catalog names it: send it anyway (the gateway may
        # still serve it) rather than skipping the switch.
        ("databricks-claude-opus-5", "databricks-claude-opus-5"),
    ],
)
def test_apply_thread_model_switches_in_codex_spelling(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    routed: str,
    applied: str,
) -> None:
    """The thread switch and the config.toml mirror both speak codex."""
    from omnigent.codex_native_bridge import read_codex_config_model

    codex_home_for_bridge_dir(bridge_dir).mkdir(parents=True, exist_ok=True)
    client = _install_fake_client(monkeypatch, _FakeAppServerClient(_LIVE_CATALOG))

    assert codex_native_hook._apply_thread_model(bridge_dir, routed) is True

    assert client.requests == [
        ("model/list", {"includeHidden": True}),
        ("thread/settings/update", {"threadId": "thread_abc", "model": applied}),
    ]
    assert client.closed is True
    assert read_codex_config_model(bridge_dir) == applied


def test_apply_thread_model_falls_back_to_the_routed_id_without_a_catalog(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable catalog must not cost the switch — send the id verbatim."""
    from omnigent.codex_native_bridge import read_codex_config_model

    codex_home_for_bridge_dir(bridge_dir).mkdir(parents=True, exist_ok=True)
    client = _install_fake_client(monkeypatch, _FakeAppServerClient(None))

    assert codex_native_hook._apply_thread_model(bridge_dir, "databricks-gpt-5-6-luna") is True

    assert client.requests[-1] == (
        "thread/settings/update",
        {"threadId": "thread_abc", "model": "databricks-gpt-5-6-luna"},
    )
    assert read_codex_config_model(bridge_dir) == "databricks-gpt-5-6-luna"


def test_apply_thread_model_pages_the_catalog(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slug on a later page still translates."""

    class _PagedClient(_FakeAppServerClient):
        async def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            """
            Serve ``model/list`` in two pages.

            :param method: App-server method name.
            :param params: Method parameters.
            :returns: A JSON-RPC response envelope.
            """
            self.requests.append((method, params))
            if method != "model/list":
                return {"result": {}}
            if params.get("cursor") is None:
                return {"result": {"data": [{"id": "gpt-5.5"}], "nextCursor": "p2"}}
            return {"result": {"data": [{"id": "gpt-5.6-luna"}], "nextCursor": None}}

    codex_home_for_bridge_dir(bridge_dir).mkdir(parents=True, exist_ok=True)
    client = _install_fake_client(monkeypatch, _PagedClient(None))

    assert codex_native_hook._apply_thread_model(bridge_dir, "databricks-gpt-5-6-luna") is True

    assert client.requests[-1] == (
        "thread/settings/update",
        {"threadId": "thread_abc", "model": "gpt-5.6-luna"},
    )


def test_route_turn_ignores_a_marker_another_session_left_in_the_dir(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A ``/clear`` rotation hands this dir to a NEW conversation.

    The old session's marker must not make the new one fast-skip: it would
    never route its first message, and the routing it never got would be
    attributed to the superseded session id.
    """
    from omnigent.runner.turn_routing import turn_routing_marker_session, write_turn_routing_marker

    _advertise_turn_router(bridge_dir)
    write_turn_routing_marker(bridge_dir, session_id="conv_superseded", decision_id="d0")
    asked: list[str] = []

    def _post(url: str, token: str, body: dict[str, object], timeout: float) -> dict[str, object]:
        del token, body, timeout
        asked.append(url)
        return {"action": "allow", "rationale": "already pinned", "terminal": True}

    monkeypatch.setattr(codex_native_hook, "_post_json", _post)

    assert _run_route_turn(bridge_dir, {"prompt": "hello"}, monkeypatch) == 0
    # It asked, and the terminal answer re-keyed the marker onto THIS session.
    assert asked == ["http://127.0.0.1:54321/v1/sessions/conv_active/route-turn"]
    assert turn_routing_marker_session(bridge_dir) == "conv_active"


def test_route_turn_writes_a_session_scoped_marker_on_a_terminal_no_op(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The marker names the session and the decision it belongs to."""
    from omnigent.runner.turn_routing import MARKER_FILE

    _advertise_turn_router(bridge_dir)
    monkeypatch.setattr(
        codex_native_hook,
        "_post_json",
        lambda *args, **kwargs: {
            "action": "allow",
            "rationale": "smart routing is off for this session",
            "terminal": True,
            "decision_id": "decision-7",
        },
    )

    assert _run_route_turn(bridge_dir, {"prompt": "hello"}, monkeypatch) == 0

    marker = json.loads((bridge_dir / MARKER_FILE).read_text(encoding="utf-8"))
    assert marker["session_id"] == "conv_active"
    assert marker["decision_id"] == "decision-7"


def test_route_turn_is_not_registered_for_a_session_that_cannot_route(
    tmp_path: Path,
) -> None:
    """
    A routing-off session's ``hooks.json`` carries no route-turn command.

    Registered unconditionally, every submit of every codex-native session paid
    the hook's routing round trip (25s worst case on a degraded server) only to
    be told the session does not route. The policy gate stays, unaffected.
    """
    from omnigent.codex_native_app_server import _codex_policy_hooks_settings

    off = _codex_policy_hooks_settings(tmp_path, sys.executable, turn_routing=False)
    commands = [
        hook["command"] for entry in off["hooks"]["UserPromptSubmit"] for hook in entry["hooks"]
    ]
    assert not any("route-turn" in command for command in commands)
    assert any("evaluate-policy" in command for command in commands)

    on = _codex_policy_hooks_settings(tmp_path, sys.executable, turn_routing=True)
    on_commands = [
        hook["command"] for entry in on["hooks"]["UserPromptSubmit"] for hook in entry["hooks"]
    ]
    assert sum("route-turn" in command for command in on_commands) == 1
    assert any("evaluate-policy" in command for command in on_commands)


def test_the_turn_router_advertisement_is_the_switch_for_the_hook(tmp_path: Path) -> None:
    """The runner only advertises for a session that launched with routing on."""
    import os

    from omnigent.codex_native_app_server import _turn_router_advertised
    from omnigent.runner.turn_routing import ADVERTISEMENT_FILE

    assert _turn_router_advertised(tmp_path) is False
    (tmp_path / ADVERTISEMENT_FILE).write_text(
        json.dumps(
            {
                "url": "http://127.0.0.1:54321",
                "token": "turn-token",
                "pid": os.getpid(),
                "session_id": "conv_active",
            }
        ),
        encoding="utf-8",
    )
    assert _turn_router_advertised(tmp_path) is True
