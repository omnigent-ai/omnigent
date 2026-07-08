"""Tests for the native Claude Code bridge executor."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.claude_native_bridge import (
    REQUEST_SESSION_ID_ENV_VAR,
    ClaudePromptReadyTimeout,
    build_hook_settings,
)
from omnigent.inner import claude_native_executor
from omnigent.inner.claude_native_executor import ClaudeNativeExecutor
from omnigent.inner.executor import ExecutorError, TurnComplete

# Minimal valid 1x1 white PNG used for multimodal attachment tests.
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
)
_TINY_PNG_DATA_URI = f"data:image/png;base64,{_TINY_PNG_B64}"
_TINY_PNG_BYTES = base64.b64decode(_TINY_PNG_B64)


@pytest.mark.asyncio
async def test_run_turn_injects_user_message_without_streaming_transcript(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Web UI turns are typed into Claude's tmux pane only.

    The background transcript forwarder is the only path allowed to
    produce visible Omnigent chat items. This fails if the executor
    regresses to tailing JSONL and producing duplicate assistant text.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "claude.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    sent_messages: list[dict[str, Any]] = []

    def fake_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """
        Capture the injected message and write a transcript line.

        :param bridge_dir_arg: Bridge directory passed by the executor.
        :param content: Text typed into the Claude tmux pane.
        :param timeout_s: tmux-target readiness timeout (ignored
            here — the fake doesn't shell out).
        :returns: None.
        """
        del timeout_s
        sent_messages.append({"bridge_dir": bridge_dir_arg, "content": content})
        transcript_path.write_text("terminal-owned output\n", encoding="utf-8")

    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        fake_inject_user_message,
    )

    executor = ClaudeNativeExecutor(bridge_dir)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hello from web"}],
            tools=[],
            system_prompt="ignored",
        )
    ]

    # The executor must deliver exactly the user's text to the
    # bridge. If this assertion changes shape, the harness has
    # picked up an extra envelope (metadata, framing, etc.) that
    # wasn't in the original CLAUDE_NATIVE design.
    assert sent_messages == [
        {
            "bridge_dir": bridge_dir,
            "content": "hello from web",
        }
    ]
    assert events == [TurnComplete(response=None)]
    assert not (bridge_dir / "transcript_forwarder.json").exists()
    assert not (bridge_dir / "transcript_forwarder.pause.json").exists()


@pytest.mark.asyncio
async def test_run_turn_recovers_prompt_timeout_and_retries_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A Claude Code boot timeout is closed, relaunched, and retried once.

    The first injection sees tmux but never sees Claude's input prompt,
    which is the startup crash class this recovery handles. The executor
    must close the poisoned terminal, use the native ensure path to
    relaunch, then deliver the same pending message exactly once on the
    retry.
    """
    monkeypatch.setenv(REQUEST_SESSION_ID_ENV_VAR, "conv_boot")
    bridge_dir = tmp_path / "bridge"
    ops: list[str] = []

    def fake_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        del bridge_dir_arg, timeout_s
        ops.append(f"inject:{content}")
        if ops == ["inject:recover me"]:
            raise ClaudePromptReadyTimeout(
                "Claude Code terminal did not become ready within 0.1s "
                "(input prompt never rendered). The message was not delivered."
            )

    async def fake_close(bridge_dir_arg: Path, session_id: str) -> None:
        del bridge_dir_arg
        ops.append(f"close:{session_id}")

    async def fake_ensure(bridge_dir_arg: Path, session_id: str) -> None:
        del bridge_dir_arg
        ops.append(f"ensure:{session_id}")

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fake_inject_user_message)
    monkeypatch.setattr(
        claude_native_executor,
        "_close_claude_terminal_for_recovery",
        fake_close,
    )
    monkeypatch.setattr(
        claude_native_executor,
        "_ensure_claude_terminal_for_recovery",
        fake_ensure,
    )

    executor = ClaudeNativeExecutor(bridge_dir)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "recover me"}],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert events == [TurnComplete(response=None)]
    assert ops == [
        "inject:recover me",
        "close:conv_boot",
        "ensure:conv_boot",
        "inject:recover me",
    ]


@pytest.mark.asyncio
async def test_run_turn_double_prompt_timeout_is_retryable_and_closes_retry_pane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    If the recovery launch also times out, the retry pane is closed.

    The executor must not loop or emit multiple failures. It returns one
    retryable error so the session can be resumed by a later message, and
    it closes the second crashed pane so the next native ensure cannot
    reuse it.
    """
    monkeypatch.setenv(REQUEST_SESSION_ID_ENV_VAR, "conv_retry_fails")
    bridge_dir = tmp_path / "bridge"
    ops: list[str] = []

    def fake_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        del bridge_dir_arg, timeout_s
        ops.append(f"inject:{content}")
        raise ClaudePromptReadyTimeout(
            "Claude Code terminal did not become ready within 0.1s "
            "(input prompt never rendered). The message was not delivered."
        )

    async def fake_close(bridge_dir_arg: Path, session_id: str) -> None:
        del bridge_dir_arg
        ops.append(f"close:{session_id}")

    async def fake_ensure(bridge_dir_arg: Path, session_id: str) -> None:
        del bridge_dir_arg
        ops.append(f"ensure:{session_id}")

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fake_inject_user_message)
    monkeypatch.setattr(
        claude_native_executor,
        "_close_claude_terminal_for_recovery",
        fake_close,
    )
    monkeypatch.setattr(
        claude_native_executor,
        "_ensure_claude_terminal_for_recovery",
        fake_ensure,
    )

    executor = ClaudeNativeExecutor(bridge_dir)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "still recover"}],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ExecutorError)
    assert error.retryable is True
    assert error.code == "timeout"
    assert "one automatic relaunch" in error.message
    assert ops == [
        "inject:still recover",
        "close:conv_retry_fails",
        "ensure:conv_retry_fails",
        "inject:still recover",
        "close:conv_retry_fails",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("close_status", [200, 404])
async def test_boot_recovery_uses_terminal_resource_api_and_auth_headers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    close_status: int,
) -> None:
    """
    Recovery tears down before ensure through the real HTTP request path.

    The permission-hook config carries the same owner bearer and routing
    headers that satisfy the server terminal routes' LEVEL_EDIT checks.
    Recovery must replay them unchanged for both the normal close case
    and the already-gone 404 close case.
    """
    root = tmp_path / "root"
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", root)
    bridge_dir = root / "bridge"
    build_hook_settings(
        bridge_dir,
        ap_server_url="http://ap",
        ap_auth_headers={
            "Authorization": "Bearer owner-token",
            "X-Databricks-Org-Id": "123",
        },
    )
    ops: list[str] = []
    requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def fake_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        del bridge_dir_arg, timeout_s
        ops.append(f"inject:{content}")
        if len(ops) == 1:
            raise ClaudePromptReadyTimeout(
                "Claude Code terminal did not become ready within 0.1s "
                "(input prompt never rendered). The message was not delivered."
            )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer owner-token"
        assert request.headers["x-databricks-org-id"] == "123"
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.method == "DELETE":
            return httpx.Response(close_status, json={"id": "terminal_claude_main"})
        if request.method == "POST":
            return httpx.Response(200, json={"id": "terminal_claude_main"})
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)

    def recovery_http_client(base_url: str, headers: dict[str, str]) -> httpx.AsyncClient:
        assert base_url == "http://ap"
        assert headers == {
            "Authorization": "Bearer owner-token",
            "X-Databricks-Org-Id": "123",
        }
        return httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            transport=transport,
            timeout=30.0,
        )

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fake_inject_user_message)
    monkeypatch.setattr(claude_native_executor, "_recovery_http_client", recovery_http_client)

    await claude_native_executor._inject_user_message_with_boot_recovery(
        bridge_dir,
        content="recover over http",
        request_session_id="conv_boot",
    )

    assert ops == ["inject:recover over http", "inject:recover over http"]
    assert requests == [
        (
            "DELETE",
            "/v1/sessions/conv_boot/resources/terminals/terminal_claude_main",
            None,
        ),
        (
            "POST",
            "/v1/sessions/conv_boot/resources/terminals",
            {
                "terminal": "claude",
                "session_key": "main",
                "ensure_native_terminal": True,
            },
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("ensure_failure", ["timeout", "status"])
async def test_boot_recovery_http_failure_is_retryable_and_cleans_up_retry_pane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ensure_failure: str,
) -> None:
    """
    Recovery HTTP failures become one retryable executor error.

    A timeout or non-2xx ensure may have partially created a retry
    pane, so the executor closes claude/main again before surfacing the
    retryable outcome.
    """
    monkeypatch.setenv(REQUEST_SESSION_ID_ENV_VAR, "conv_http_fail")
    root = tmp_path / "root"
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", root)
    bridge_dir = root / "bridge"
    build_hook_settings(
        bridge_dir,
        ap_server_url="http://ap",
        ap_auth_headers={"Authorization": "Bearer owner-token"},
    )
    requests: list[tuple[str, str]] = []

    def fake_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        del bridge_dir_arg, content, timeout_s
        raise ClaudePromptReadyTimeout(
            "Claude Code terminal did not become ready within 0.1s "
            "(input prompt never rendered). The message was not delivered."
        )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "DELETE":
            return httpx.Response(200, json={"id": "terminal_claude_main"})
        if ensure_failure == "timeout":
            raise httpx.TimeoutException("ensure timed out", request=request)
        return httpx.Response(503, json={"error": {"message": "busy"}})

    transport = httpx.MockTransport(handler)

    def recovery_http_client(base_url: str, headers: dict[str, str]) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            transport=transport,
            timeout=30.0,
        )

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fake_inject_user_message)
    monkeypatch.setattr(claude_native_executor, "_recovery_http_client", recovery_http_client)

    executor = ClaudeNativeExecutor(bridge_dir)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "recover me"}],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ExecutorError)
    assert error.retryable is True
    assert error.code == "timeout"
    assert requests == [
        ("DELETE", "/v1/sessions/conv_http_fail/resources/terminals/terminal_claude_main"),
        ("POST", "/v1/sessions/conv_http_fail/resources/terminals"),
        ("DELETE", "/v1/sessions/conv_http_fail/resources/terminals/terminal_claude_main"),
    ]


@pytest.mark.asyncio
async def test_boot_recovery_closes_retry_pane_on_non_readiness_retry_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A non-readiness retry failure still closes the freshly-ensured pane.

    When the post-recovery injection fails with a different RuntimeError
    (tmux target missing, submit verification), the original error must
    propagate — but the retry pane must be closed first, or the next
    native ensure could reuse the dead pane.
    """
    monkeypatch.setenv(REQUEST_SESSION_ID_ENV_VAR, "conv_sibling_err")
    bridge_dir = tmp_path / "bridge"
    ops: list[str] = []

    def fake_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        del bridge_dir_arg, timeout_s
        ops.append(f"inject:{content}")
        if len(ops) == 1:
            raise ClaudePromptReadyTimeout(
                "Claude Code terminal did not become ready within 0.1s "
                "(input prompt never rendered). The message was not delivered."
            )
        raise RuntimeError("tmux send-keys target claude:main is not advertised yet")

    async def fake_close(bridge_dir_arg: Path, session_id: str) -> None:
        del bridge_dir_arg
        ops.append(f"close:{session_id}")

    async def fake_ensure(bridge_dir_arg: Path, session_id: str) -> None:
        del bridge_dir_arg
        ops.append(f"ensure:{session_id}")

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fake_inject_user_message)
    monkeypatch.setattr(
        claude_native_executor,
        "_close_claude_terminal_for_recovery",
        fake_close,
    )
    monkeypatch.setattr(
        claude_native_executor,
        "_ensure_claude_terminal_for_recovery",
        fake_ensure,
    )

    executor = ClaudeNativeExecutor(bridge_dir)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "sibling failure"}],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ExecutorError)
    assert error.retryable is False
    assert "not advertised yet" in error.message
    assert ops == [
        "inject:sibling failure",
        "close:conv_sibling_err",
        "ensure:conv_sibling_err",
        "inject:sibling failure",
        "close:conv_sibling_err",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["session_id", "server_config"])
async def test_boot_recovery_config_failures_stay_retryable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    missing: str,
) -> None:
    """
    Recovery pre-flight config failures surface as retryable errors.

    An unresolvable session id or missing server routing metadata means
    recovery could not run — the underlying boot timeout is still the
    transient failure, so the executor must not downgrade it to a
    permanent (non-retryable) error.
    """
    if missing == "session_id":
        monkeypatch.delenv(REQUEST_SESSION_ID_ENV_VAR, raising=False)
        monkeypatch.setattr(claude_native_executor, "read_active_session_id", lambda _: None)
    else:
        monkeypatch.setenv(REQUEST_SESSION_ID_ENV_VAR, "conv_cfg")
        monkeypatch.setattr(claude_native_executor, "read_permission_hook_config", lambda _: {})
    bridge_dir = tmp_path / "bridge"

    def fake_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        del bridge_dir_arg, content, timeout_s
        raise ClaudePromptReadyTimeout(
            "Claude Code terminal did not become ready within 0.1s "
            "(input prompt never rendered). The message was not delivered."
        )

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fake_inject_user_message)

    executor = ClaudeNativeExecutor(bridge_dir)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "recover me"}],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ExecutorError)
    assert error.retryable is True
    assert error.code == "timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize("lapse_signal", ["401", "oidc_redirect"])
async def test_boot_recovery_reauths_once_on_lapsed_bearer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    lapse_signal: str,
) -> None:
    """
    Recovery re-mints the bearer once when the snapshotted token lapsed.

    The bridge's auth headers are snapshotted at launch; a long-idle
    session's recovery close can hit a 401 or an OAuth-login redirect.
    The request must retry exactly once with the freshly minted bearer.
    """
    root = tmp_path / "root"
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", root)
    bridge_dir = root / "bridge"
    build_hook_settings(
        bridge_dir,
        ap_server_url="http://ap",
        ap_auth_headers={
            "Authorization": "Bearer stale-token",
            "X-Databricks-Org-Id": "123",
        },
    )
    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers["authorization"])
        if request.headers["authorization"] == "Bearer stale-token":
            if lapse_signal == "401":
                return httpx.Response(401)
            return httpx.Response(302, headers={"location": "http://ap/oidc/login"})
        assert request.headers["x-databricks-org-id"] == "123"
        return httpx.Response(200, json={"id": "terminal_claude_main"})

    transport = httpx.MockTransport(handler)

    def recovery_http_client(base_url: str, headers: dict[str, str]) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            transport=transport,
            timeout=30.0,
        )

    def fake_policy_hook_reauth(base_url: str, headers: dict[str, str]) -> Any:
        assert base_url == "http://ap"
        return lambda: {**headers, "Authorization": "Bearer fresh-token"}

    monkeypatch.setattr(claude_native_executor, "_recovery_http_client", recovery_http_client)
    monkeypatch.setattr(claude_native_executor, "policy_hook_reauth", fake_policy_hook_reauth)

    await claude_native_executor._close_claude_terminal_for_recovery(bridge_dir, "conv_auth")

    assert seen_auth == ["Bearer stale-token", "Bearer fresh-token"]


@pytest.mark.asyncio
async def test_run_turn_does_not_advertise_active_omnigent_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    The executor does not create a second AP-visible tool path.

    Claude-native chat visibility is terminal-originated. Web-chat
    submission is an input adapter, so tool activity must come back
    from Claude's transcript rather than from a transient Omnigent turn.
    """
    bridge_dir = tmp_path / "bridge"
    sent_messages: list[dict[str, Any]] = []

    def fake_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """
        Capture a web-message injection.

        :param bridge_dir_arg: Bridge directory passed by the executor.
        :param content: Text typed into the Claude tmux pane.
        :param timeout_s: tmux-target readiness timeout (ignored).
        :returns: None.
        """
        del timeout_s
        sent_messages.append({"bridge_dir": bridge_dir_arg, "content": content})

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fake_inject_user_message)

    executor = ClaudeNativeExecutor(bridge_dir)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "use a tool"}],
            tools=[
                {
                    "name": "sys_os_read",
                    "description": "Read a file.",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            system_prompt="ignored",
        )
    ]

    assert sent_messages == [{"bridge_dir": bridge_dir, "content": "use a tool"}]
    assert events == [TurnComplete(response=None)]
    assert not (bridge_dir / "tool_relay.json").exists()


@pytest.mark.asyncio
async def test_run_turn_rejects_stale_session_after_clear(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Old-session turns must not type into the post-``/clear`` Claude pane.

    The request session id comes from the harness spawn env. If it no
    longer matches the bridge's active session, the executor must fail
    before calling tmux injection.
    """
    (tmp_path / "bridge.json").write_text(
        '{"active_session_id": "conv_new"}',
        encoding="utf-8",
    )
    monkeypatch.setenv(REQUEST_SESSION_ID_ENV_VAR, "conv_old")

    def fail_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """
        Fail if stale-session protection reaches tmux injection.

        :param bridge_dir_arg: Bridge directory passed by the executor.
        :param content: Text that would be typed into tmux.
        :param timeout_s: tmux-target readiness timeout.
        :returns: Never returns.
        """
        del bridge_dir_arg, content, timeout_s
        raise AssertionError("stale session injected into tmux")

    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        fail_inject_user_message,
    )

    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "old tab message"}],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "no longer active after /clear" in events[0].message


@pytest.mark.asyncio
async def test_enqueue_session_message_rejects_stale_session_after_clear(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Stale-session steering must not reach the post-``/clear`` Claude pane.
    """
    (tmp_path / "bridge.json").write_text(
        '{"active_session_id": "conv_new"}',
        encoding="utf-8",
    )
    monkeypatch.setenv(REQUEST_SESSION_ID_ENV_VAR, "conv_old")

    def fail_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """
        Fail if stale-session steering reaches tmux injection.

        :param bridge_dir_arg: Bridge directory passed by the executor.
        :param content: Text that would be typed into tmux.
        :param timeout_s: tmux-target readiness timeout.
        :returns: Never returns.
        """
        del bridge_dir_arg, content, timeout_s
        raise AssertionError("stale session injected into tmux")

    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        fail_inject_user_message,
    )

    executor = ClaudeNativeExecutor(tmp_path)
    injected = await executor.enqueue_session_message(
        session_key="main",
        content="old steering",
    )

    assert injected is False


@pytest.mark.asyncio
async def test_enqueue_session_message_injects_steering_into_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    In-flight server messages are typed into Claude's tmux pane.

    This catches regressions where web UI steering is accepted by the
    harness but never reaches the native Claude Code process.
    """
    sent_messages: list[dict[str, Any]] = []

    def fake_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """
        Capture a steering injection.

        :param bridge_dir_arg: Bridge directory passed by the executor.
        :param content: Text typed into the Claude tmux pane.
        :param timeout_s: tmux-target readiness timeout (ignored).
        :returns: None.
        """
        del timeout_s
        sent_messages.append({"bridge_dir": bridge_dir_arg, "content": content})

    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        fake_inject_user_message,
    )

    executor = ClaudeNativeExecutor(tmp_path)
    accepted = await executor.enqueue_session_message("session-key", "steer me")

    assert accepted is True
    # Steering injection delivers raw text only — no envelope. The
    # session_key is intentionally NOT included since there is one
    # tmux pane per conversation; mixing in routing metadata would
    # cause Claude to see arbitrary key-value pairs as user input.
    assert sent_messages == [
        {
            "bridge_dir": tmp_path,
            "content": "steer me",
        }
    ]


@pytest.mark.asyncio
async def test_concurrent_injections_do_not_overlap_in_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two injections must not write to the tmux pane at the same time.

    Repro for the claude-native "12"/"23" message-combining symptom.
    ``inject_user_message`` is not atomic: it issues several ``tmux
    send-keys`` calls in sequence (clear line, type literal text, send
    Enter). The executor runs each injection via ``asyncio.to_thread``
    and does NOT serialize them, so a ``run_turn`` injection and a
    mid-turn ``enqueue_session_message`` injection can land in the
    thread pool concurrently and interleave their keystrokes against the
    same pane — e.g. typing "1" and "2" into one prompt as "12".

    This test drives those two real code paths concurrently. The fake
    ``inject_user_message`` records the maximum number of injections
    inside its (otherwise atomic) critical region at once. The invariant
    under test is that the executor serializes terminal writes, so that
    maximum must be 1.
    """
    monkeypatch.delenv(REQUEST_SESSION_ID_ENV_VAR, raising=False)

    state = {"now": 0, "max": 0}
    state_lock = threading.Lock()
    release = threading.Event()

    def fake_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """Record peak concurrency, then hold the call open until released.

        :param bridge_dir_arg: Bridge directory (ignored).
        :param content: Text that would be typed into tmux (ignored).
        :param timeout_s: tmux-target readiness timeout (ignored).
        :returns: None.
        """
        del bridge_dir_arg, content, timeout_s
        with state_lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        # Hold the keystroke sequence open so a second, concurrent
        # injection (if the executor fails to serialize) is observed
        # inside this critical region at the same time, bumping max to 2.
        release.wait(timeout=2.0)
        with state_lock:
            state["now"] -= 1

    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        fake_inject_user_message,
    )

    executor = ClaudeNativeExecutor(tmp_path)

    async def _drive_run_turn() -> None:
        """Consume a run_turn (the initial-message injection path)."""
        async for _ in executor.run_turn(
            messages=[{"role": "user", "content": "one"}],
            tools=[],
            system_prompt="",
        ):
            pass

    # Path A: run_turn injection. Path B: mid-turn steering injection.
    # Both call inject_user_message via asyncio.to_thread concurrently.
    run_turn_task = asyncio.create_task(_drive_run_turn())
    enqueue_task = asyncio.create_task(executor.enqueue_session_message("k", "two"))

    # Sync gate: wait until at least one injection is inside the region.
    for _ in range(200):
        if state["max"] >= 1:
            break
        await asyncio.sleep(0.01)
    # Give the second injection a chance to enter concurrently. With
    # proper serialization it cannot — it would block until the first
    # releases — so max stays 1. Without it, both enter and max hits 2.
    for _ in range(50):
        if state["max"] >= 2:
            break
        await asyncio.sleep(0.01)

    release.set()
    await asyncio.gather(run_turn_task, enqueue_task)

    # max == 2 means both injections wrote to the pane simultaneously,
    # which is exactly the interleaving that combines "1" and "2" into
    # "12". A correct executor serializes terminal writes → max == 1.
    assert state["max"] == 1, (
        f"concurrent injections overlapped in the tmux pane "
        f"(peak concurrency {state['max']}); the executor must serialize "
        f"terminal writes so keystrokes from different messages cannot "
        f"interleave into a single prompt (the '12'/'23' bug)."
    )


# -- Multimodal attachment tests ------------------------------------------


def _stub_inject(
    sent: list[dict[str, Any]],
) -> Any:
    """
    Build a fake ``inject_user_message`` that captures calls.

    :param sent: Mutable list that receives one dict per invocation,
        keyed by ``bridge_dir`` and ``content``.
    :returns: Callable matching ``inject_user_message``'s signature.
    """

    def _fake(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        del timeout_s
        sent.append({"bridge_dir": bridge_dir_arg, "content": content})

    return _fake


@pytest.mark.asyncio
async def test_run_turn_materializes_image_to_bridge_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An ``input_image`` block with a resolved data URI is decoded to a
    file in the bridge directory and referenced by path in the text
    injected into Claude's terminal.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": _TINY_PNG_DATA_URI,
                            "filename": "screenshot.png",
                        },
                        {"type": "input_text", "text": "what is this?"},
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    # Turn completes successfully after injection.
    assert events == [TurnComplete(response=None)]
    assert len(sent) == 1
    injected = sent[0]["content"]

    # Attachment reference line appears before the user's text.
    # If the image block was silently dropped (pre-fix behavior),
    # the injected text would be just "what is this?" with no path.
    assert "[Attached:" in injected, (
        "Image block was dropped — _content_to_text did not materialize it"
    )
    assert "screenshot.png" in injected
    assert "what is this?" in injected
    # Attachment line must come before user text.
    attach_pos = injected.index("[Attached:")
    text_pos = injected.index("what is this?")
    assert attach_pos < text_pos, (
        "Attachment reference should precede user text so Claude sees the "
        "file path before the question"
    )

    # The file was written to disk with the correct content.
    uploads = tmp_path / "uploads"
    written = list(uploads.iterdir())
    # Exactly 1 file — the materialized PNG.
    assert len(written) == 1, (
        f"Expected 1 written file, got {len(written)}. If 0, the attachment was not materialized."
    )
    assert written[0].name == "screenshot.png"
    # Byte-level check: decoded content matches the original PNG.
    assert written[0].read_bytes() == _TINY_PNG_BYTES


@pytest.mark.asyncio
async def test_run_turn_image_only_no_text_still_injects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A message with only an image (no text) materializes the file and
    injects the path reference. The executor must not yield an error.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": _TINY_PNG_DATA_URI,
                            "filename": "photo.png",
                        },
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    # Must complete, not error — an image-only message is valid input.
    # If _content_to_text returned "" (dropping the image), the
    # executor would yield ExecutorError instead of TurnComplete.
    assert events == [TurnComplete(response=None)]
    assert len(sent) == 1
    assert "photo.png" in sent[0]["content"]


@pytest.mark.asyncio
async def test_run_turn_unresolved_file_id_skipped_gracefully(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An ``input_image`` block with only a ``file_id`` (content resolver
    did not run) is skipped. The text portion is still injected.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "file_id": "file_abc123"},
                        {"type": "input_text", "text": "analyze this"},
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert events == [TurnComplete(response=None)]
    assert len(sent) == 1
    # The unresolved image block is skipped; only text survives.
    assert sent[0]["content"] == "analyze this"
    # No uploads directory created — nothing to materialize.
    assert not (tmp_path / "uploads").exists()


@pytest.mark.asyncio
async def test_run_turn_dedup_same_filename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Two image blocks with the same filename produce distinct files
    (the second gets a unique suffix to avoid overwriting the first).
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    image_block = {
        "type": "input_image",
        "image_url": _TINY_PNG_DATA_URI,
        "filename": "dup.png",
    }
    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [image_block, image_block],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert events == [TurnComplete(response=None)]
    uploads = tmp_path / "uploads"
    written = sorted(uploads.iterdir())
    # Two distinct files, not one overwritten file.
    assert len(written) == 2, (
        f"Expected 2 files (dedup suffix), got {len(written)}. "
        "If 1, the second image overwrote the first."
    )
    # Both contain the same PNG bytes.
    for f in written:
        assert f.read_bytes() == _TINY_PNG_BYTES


@pytest.mark.asyncio
async def test_run_turn_image_without_filename_gets_generated_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An image block without a ``filename`` field gets a generated name
    with the correct extension derived from the MIME type.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": _TINY_PNG_DATA_URI},
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert events == [TurnComplete(response=None)]
    uploads = tmp_path / "uploads"
    written = list(uploads.iterdir())
    assert len(written) == 1
    # Generated name should have .png extension from the data URI MIME.
    assert written[0].suffix == ".png", (
        f"Expected .png extension, got {written[0].suffix}. "
        "MIME-to-extension mapping may be missing for image/png."
    )


@pytest.mark.asyncio
async def test_enqueue_session_message_materializes_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Steering messages with multimodal content blocks also materialize
    attachments (same path as ``run_turn``).
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = ClaudeNativeExecutor(tmp_path)
    accepted = await executor.enqueue_session_message(
        "session-key",
        [
            {
                "type": "input_image",
                "image_url": _TINY_PNG_DATA_URI,
                "filename": "steering_img.png",
            },
            {"type": "input_text", "text": "look at this"},
        ],
    )

    assert accepted is True
    assert len(sent) == 1
    injected = sent[0]["content"]
    assert "steering_img.png" in injected
    assert "look at this" in injected
    # File was written to the bridge directory.
    written = list((tmp_path / "uploads").iterdir())
    assert len(written) == 1
    assert written[0].name == "steering_img.png"


@pytest.mark.asyncio
async def test_run_turn_malformed_data_uri_skipped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An image block with a malformed data URI is skipped gracefully.
    The text portion is still injected without error.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,NOT_VALID_BASE64!@#",
                        },
                        {"type": "input_text", "text": "still send this"},
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    # Turn completes — the bad image is skipped, text is injected.
    assert events == [TurnComplete(response=None)]
    assert len(sent) == 1
    assert sent[0]["content"] == "still send this"
    # No file written for the malformed URI.
    assert not (tmp_path / "uploads").exists()


@pytest.mark.asyncio
async def test_run_turn_path_traversal_filename_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A filename with path traversal components is stripped to the base name.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": _TINY_PNG_DATA_URI,
                            "filename": "../../.bashrc",
                        },
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert events == [TurnComplete(response=None)]
    uploads = tmp_path / "uploads"
    written = list(uploads.iterdir())
    assert len(written) == 1
    assert written[0].name == ".bashrc"
    assert written[0].parent == uploads
