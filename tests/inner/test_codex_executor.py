"""Tests for CodexExecutor."""

import asyncio
import base64
import contextlib
import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from omnigent.inner.codex_executor import (
    _TURN_EVENT_WARN_SECONDS,
    CodexExecutor,
    _build_initial_prompt,
    _codex_cli_version,
    _CodexAppServerSession,
    _databricks_codex_config_overrides,
    _dynamic_tool_result_payload,
    _prompt_for_turn,
    _to_codex_input_items,
)
from omnigent.inner.executor import (
    ExecutorError,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    TurnComplete,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


@dataclass
class _FakeAppSession:
    scripted_turns: list[list[object]]
    closed: bool = False
    interrupted: bool = False
    calls: list[dict] = None  # type: ignore[assignment]

    def __post_init__(self):
        self.calls = []

    async def run_turn(self, **kwargs):
        self.calls.append(kwargs)
        for event in self.scripted_turns.pop(0):
            yield event

    async def close(self):
        self.closed = True

    async def interrupt_turn(self):
        self.interrupted = True
        return True


class _FakePipe:
    def __init__(self):
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    async def readline(self) -> bytes:
        return b""

    async def read(self, n: int) -> bytes:
        return b""


class _OverflowingPipe:
    def __init__(self):
        self.read_calls = 0

    async def read(self, n: int) -> bytes:
        if self.read_calls == 0:
            self.read_calls += 1
            return (b"x" * n) + b"\n"
        return b""


class _ChunkedPipe:
    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)

    async def read(self, n: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _FakeProcess:
    def __init__(self):
        self.stdin = _FakePipe()
        self.stdout = _FakePipe()
        self.stderr = _FakePipe()
        self.returncode: int | None = None
        self.terminated = False
        self.pid = 12345

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


class TestCodexExecutor(unittest.TestCase):
    def test_databricks_codex_config_overrides(self):
        overrides = _databricks_codex_config_overrides(
            model="databricks-gpt-5-4-mini",
            base_url="https://example.cloud.databricks.com/ai-gateway/codex/v1",
            auth_command=('databricks auth token --host "https://example.cloud.databricks.com"'),
        )
        self.assertIn('model="databricks-gpt-5-4-mini"', overrides)
        self.assertIn('model_provider="omnigent_databricks"', overrides)
        self.assertIn("model_supports_reasoning_summaries=true", overrides)
        self.assertTrue(any("/ai-gateway/codex/v1" in item for item in overrides))
        self.assertFalse(any("/serving-endpoints" in item for item in overrides))
        self.assertTrue(any('auth={command="sh"' in item for item in overrides))
        self.assertTrue(any("databricks auth token --host" in item for item in overrides))
        self.assertTrue(any("refresh_interval_ms=900000" in item for item in overrides))
        self.assertFalse(any('env_key="DATABRICKS_TOKEN"' in item for item in overrides))

    def test_codex_config_overrides_neutralize_toml_breakout(self):
        """A model id full of TOML metacharacters stays a literal string.

        Defense-in-depth for the model_override RCE: the model value is
        ``json.dumps``-escaped, so even a string crafted to close the
        ``model="..."`` field and inject its own ``auth.command`` parses
        back as one inert model name and never overwrites the real
        token-minting auth command.
        """
        import tomllib

        payload = 'x",auth={command="sh",args=["-c","touch /tmp/pwned"]},wire_api="responses"}'
        real_auth = 'databricks auth token --host "https://example.cloud.databricks.com"'
        overrides = _databricks_codex_config_overrides(
            model=payload,
            base_url="https://example.cloud.databricks.com/ai-gateway/codex/v1",
            auth_command=real_auth,
        )
        parsed = tomllib.loads("\n".join(overrides))
        # The whole payload round-trips as the literal model name.
        self.assertEqual(parsed["model"], payload)
        # The injected auth command did not survive — the legit one did.
        provider = parsed["model_providers"]["omnigent_databricks"]
        self.assertEqual(provider["auth"]["args"], ["-c", real_auth])

    def test_constructor_databricks_flag_with_profile(self):
        with (
            patch("omnigent.inner.codex_executor._find_codex_cli", return_value="/usr/bin/codex"),
            patch(
                "omnigent.inner.codex_executor._databricks_gateway_host",
                return_value="https://example.cloud.databricks.com",
            ),
        ):
            executor = CodexExecutor(gateway=True)
        self.assertTrue(executor._gateway)
        self.assertEqual(
            executor._env["DATABRICKS_HOST"],
            "https://example.cloud.databricks.com",
        )
        self.assertNotIn("DATABRICKS_TOKEN", executor._env)
        self.assertIn("model=", executor._codex_config_overrides[0])
        self.assertIn('model_provider="omnigent_databricks"', executor._codex_config_overrides[1])

    def test_constructor_does_not_force_codex_debug_env_by_default(self):
        with (
            patch("omnigent.inner.codex_executor._find_codex_cli", return_value="/usr/bin/codex"),
            patch.dict("os.environ", {}, clear=True),
        ):
            executor = CodexExecutor()

        self.assertNotIn("RUST_LOG", executor._env)
        self.assertNotIn("RUST_BACKTRACE", executor._env)

    def test_constructor_databricks_flag_with_profile_uses_profile_credentials(self):
        with (
            patch("omnigent.inner.codex_executor._find_codex_cli", return_value="/usr/bin/codex"),
            patch.dict("os.environ", {}, clear=True),
            patch(
                "omnigent.inner.codex_executor._databricks_gateway_host",
                return_value="https://example-profile-workspace.cloud.databricks.com",
            ),
        ):
            executor = CodexExecutor(
                gateway=True,
                databricks_profile="test-profile",
                model="databricks-gpt-5-4-mini",
            )

        self.assertEqual(
            executor._env["DATABRICKS_HOST"],
            "https://example-profile-workspace.cloud.databricks.com",
        )
        self.assertNotIn("DATABRICKS_TOKEN", executor._env)
        # The fix: with an explicit profile, the bearer-token helper selects by
        # --profile (unambiguous), never --host. A regression to --host makes a
        # workspace with two profiles on one host return an empty token → 401.
        self.assertTrue(
            any(
                "databricks auth token --profile" in override
                for override in executor._codex_config_overrides
            )
        )
        self.assertFalse(
            any("--host" in override for override in executor._codex_config_overrides)
        )
        # `--force-refresh` only exists in Databricks CLI >= v0.296.0, so it
        # must be applied via a `--help` capability probe ($force), never
        # passed unconditionally — an older CLI rejects the unknown flag and
        # yields an empty token → silent 401.
        auth_override = next(
            o for o in executor._codex_config_overrides if "databricks auth token" in o
        )
        self.assertIn("databricks auth token --help", auth_override)
        self.assertIn("force=--force-refresh", auth_override)
        self.assertNotIn('--profile "test-profile" --force-refresh', auth_override)

    def test_constructor_databricks_flag_with_host_override_skips_profile_lookup(self):
        with (
            patch("omnigent.inner.codex_executor._find_codex_cli", return_value="/usr/bin/codex"),
            patch.dict("os.environ", {}, clear=True),
            patch("omnigent.inner.codex_executor._databricks_gateway_host") as gateway_host,
        ):
            executor = CodexExecutor(
                gateway=True,
                databricks_profile="missing-profile",
                gateway_host="https://example.databricks.com/",
                base_url_override="https://example.databricks.com/ai-gateway/codex/v1",
                gateway_auth_command="printf token",
                model="databricks-gpt-5-4-mini",
            )

        gateway_host.assert_not_called()
        self.assertEqual(
            executor._env["DATABRICKS_HOST"],
            "https://example.databricks.com",
        )
        self.assertNotIn("DATABRICKS_TOKEN", executor._env)
        self.assertTrue(
            any("printf token" in override for override in executor._codex_config_overrides)
        )
        self.assertFalse(
            any(
                "databricks auth token --host" in item for item in executor._codex_config_overrides
            )
        )

    def test_constructor_databricks_flag_with_host_override_requires_base_url(self):
        with (
            patch("omnigent.inner.codex_executor._find_codex_cli", return_value="/usr/bin/codex"),
            patch.dict("os.environ", {}, clear=True),
            self.assertRaisesRegex(OSError, "GATEWAY_BASE_URL"),
        ):
            CodexExecutor(
                gateway=True,
                gateway_host="https://example.databricks.com/",
                gateway_auth_command="printf token",
            )

    def test_constructor_databricks_flag_with_host_override_requires_auth_command(self):
        with (
            patch("omnigent.inner.codex_executor._find_codex_cli", return_value="/usr/bin/codex"),
            patch.dict("os.environ", {}, clear=True),
            self.assertRaisesRegex(OSError, "GATEWAY_AUTH_COMMAND"),
        ):
            CodexExecutor(
                gateway=True,
                gateway_host="https://example.databricks.com/",
                base_url_override="https://example.databricks.com/ai-gateway/codex/v1",
            )

    def test_constructor_databricks_flag_no_creds_raises(self):
        with (
            patch("omnigent.inner.codex_executor._find_codex_cli", return_value="/usr/bin/codex"),
            patch.dict("os.environ", {}, clear=True),
            patch("omnigent.inner.codex_executor._databricks_gateway_host", return_value=None),
        ):
            with self.assertRaises(EnvironmentError):
                CodexExecutor(gateway=True)

    def test_build_initial_prompt_serializes_history(self):
        prompt = _build_initial_prompt(
            [
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
                {"role": "user", "content": "three"},
            ]
        )
        self.assertIn("Conversation so far:", prompt)
        self.assertIn("assistant: two", prompt)
        self.assertIn("user: three", prompt)

    def test_dynamic_tool_result_payload_uses_content_items(self):
        payload = _dynamic_tool_result_payload({"answer": 5})
        self.assertEqual(payload["success"], True)
        self.assertEqual(payload["contentItems"][0]["type"], "inputText")
        self.assertIn('"answer": 5', payload["contentItems"][0]["text"])

    def test_prompt_for_new_thread_keeps_history(self):
        messages = [
            {"role": "user", "content": "The secret codeword is ZEBRA-99."},
            {"role": "assistant", "content": "Understood."},
            {"role": "user", "content": "Summarize our conversation."},
        ]
        prompt = _prompt_for_turn(messages, is_new_thread=True)
        self.assertIn("Conversation so far:", prompt)
        self.assertIn("ZEBRA-99", prompt)
        self.assertIn("Summarize our conversation.", prompt)

    def test_prompt_for_existing_thread_uses_latest_user_message(self):
        messages = [
            {"role": "user", "content": "The secret codeword is ZEBRA-99."},
            {"role": "assistant", "content": "Understood."},
            {"role": "user", "content": "Summarize our conversation."},
        ]
        prompt = _prompt_for_turn(messages, is_new_thread=False)
        self.assertEqual(prompt, "Summarize our conversation.")

    def test_run_turn_delegates_to_app_server_session(self):
        async def _t():
            fake_session = _FakeAppSession(
                [
                    [
                        TextChunk(text="Checking..."),
                        ToolCallRequest(name="calculate", args={"expression": "2+3"}),
                        ToolCallComplete(
                            name="calculate",
                            status=ToolCallStatus.SUCCESS,
                            result={"result": 5},
                        ),
                        TurnComplete(response="5"),
                    ]
                ]
            )

            executor = CodexExecutor(
                codex_path="/bin/echo",
                app_session_factory=lambda **kwargs: fake_session,
            )
            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "calc", "session_id": "s1"}],
                    [
                        {
                            "name": "calculate",
                            "description": "Calculate",
                            "parameters": {
                                "type": "object",
                                "properties": {"expression": {"type": "string"}},
                            },
                        }
                    ],
                    "Be helpful.",
                )
            ]

            self.assertIsInstance(events[0], TextChunk)
            self.assertIsInstance(events[1], ToolCallRequest)
            self.assertIsInstance(events[2], ToolCallComplete)
            self.assertIsInstance(events[3], TurnComplete)
            self.assertEqual(fake_session.calls[0]["system_prompt"], "Be helpful.")
            self.assertEqual(fake_session.calls[0]["model"], "gpt-5.4-mini")
            self.assertEqual(fake_session.calls[0]["tools"][0]["name"], "calculate")

        _run(_t())

    def test_run_turn_databricks_uses_databricks_default_model(self):
        async def _t():
            fake_session = _FakeAppSession([[TurnComplete(response="done")]])
            with patch(
                "omnigent.inner.codex_executor._databricks_gateway_host",
                return_value="https://example.cloud.databricks.com",
            ):
                executor = CodexExecutor(
                    codex_path="/bin/echo",
                    gateway=True,
                    app_session_factory=lambda **kwargs: fake_session,
                )

            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hi", "session_id": "s1"}],
                    [],
                    "",
                )
            ]

            self.assertEqual(events[-1].response, "done")
            self.assertEqual(fake_session.calls[0]["model"], "databricks-gpt-5-5")

        _run(_t())

    def test_run_turn_passes_session_send_tool_through(self):
        async def _t():
            fake_session = _FakeAppSession([[TurnComplete(response="done")]])

            executor = CodexExecutor(
                codex_path="/bin/echo",
                app_session_factory=lambda **kwargs: fake_session,
            )
            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hi", "session_id": "s1"}],
                    [
                        {
                            "name": "sys_session_send",
                            "description": "Session send",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "tool": {"type": "string"},
                                    "session": {"type": "string"},
                                    "args": {"type": "object"},
                                },
                            },
                        }
                    ],
                    "Be helpful.",
                )
            ]

            self.assertEqual(fake_session.calls[0]["tools"][0]["name"], "sys_session_send")
            self.assertIsInstance(events[-1], TurnComplete)

        _run(_t())

    def test_app_server_run_turn_keeps_native_shell_by_default(self):
        async def _t():
            session = _CodexAppServerSession(
                codex_path="/bin/echo",
                cwd="/tmp/workspace",
                env={},
                tool_executor=None,
            )
            session.start = AsyncMock()
            session._proc = _FakeProcess()
            session._request = AsyncMock(
                side_effect=[
                    {"result": {"thread": {"id": "thread-1"}}},
                    {"result": {"turn": {"id": "turn-1"}}},
                    [
                        {
                            "method": "turn/completed",
                            "params": {
                                "turn": {"id": "turn-1"},
                            },
                        }
                    ][0],
                ]
            )

            async def _inject_turn_completed() -> None:
                await asyncio.sleep(0.01)
                session._events.put_nowait(
                    {
                        "method": "turn/completed",
                        "params": {
                            "turn": {"id": "turn-1"},
                        },
                    }
                )

            inject_task = asyncio.create_task(_inject_turn_completed())
            _ = [
                event
                async for event in session.run_turn(
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[
                        {
                            "name": "sys_os_shell",
                            "description": "Shell",
                            "parameters": {
                                "type": "object",
                                "properties": {"command": {"type": "string"}},
                            },
                        }
                    ],
                    system_prompt="Be helpful.",
                    model="gpt-5.4-mini",
                    cwd=".",
                    sandbox="workspace-write",
                )
            ]
            await inject_task

            thread_start_call = session._request.await_args_list[0]
            params = thread_start_call.args[1]
            self.assertNotIn("shell_tool", params["config"]["features"])
            self.assertEqual(params["dynamicTools"][0]["name"], "sys_os_shell")

        _run(_t())

    def test_app_server_run_turn_applies_effort_via_thread_settings_update(self):
        """Reasoning effort rides thread/settings/update, not turn/start.

        Codex's ``TurnStartParams`` has no ``effort`` field, so an effort set
        on ``turn/start`` is silently dropped by serde and never takes effect.
        It must go through ``thread/settings/update`` (whose
        ``ThreadSettingsUpdateParams`` carries ``effort``) — the same path the
        TUI ``/model`` picker uses.
        """

        async def _t():
            session = _CodexAppServerSession(
                codex_path="/bin/echo",
                cwd="/tmp/workspace",
                env={},
                tool_executor=None,
            )
            session.start = AsyncMock()
            session._proc = _FakeProcess()
            session._request = AsyncMock(
                side_effect=[
                    {"result": {"thread": {"id": "thread-1"}}},  # thread/start
                    {"result": {}},  # thread/settings/update
                    {"result": {"turn": {"id": "turn-1"}}},  # turn/start
                ]
            )

            async def _inject_turn_completed() -> None:
                await asyncio.sleep(0.01)
                session._events.put_nowait(
                    {"method": "turn/completed", "params": {"turn": {"id": "turn-1"}}}
                )

            inject_task = asyncio.create_task(_inject_turn_completed())
            # Drive the turn to completion (consume the event stream for its
            # side effects — the RPCs we assert on below).
            async for _event in session.run_turn(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                system_prompt="",
                model="gpt-5.4-mini",
                cwd=".",
                sandbox="workspace-write",
                reasoning_effort="high",
            ):
                pass
            await inject_task

            methods = [call.args[0] for call in session._request.await_args_list]
            # Settings update lands BEFORE the turn starts so the effort applies
            # to this turn, and turn/start carries no (dropped) effort field.
            self.assertEqual(methods, ["thread/start", "thread/settings/update", "turn/start"])
            settings_params = session._request.await_args_list[1].args[1]
            self.assertEqual(
                settings_params,
                {
                    "threadId": "thread-1",
                    "effort": "high",
                    "summary": "detailed",
                },
            )
            turn_params = session._request.await_args_list[2].args[1]
            self.assertNotIn("effort", turn_params)

        _run(_t())

    def test_app_server_run_turn_falls_back_when_settings_update_is_unsupported(self):
        """Older app-server builds accept effort only on ``turn/start``."""

        async def _t():
            session = _CodexAppServerSession(
                codex_path="/bin/echo",
                cwd="/tmp/workspace",
                env={},
                tool_executor=None,
            )
            session.start = AsyncMock()
            session._proc = _FakeProcess()
            session._request = AsyncMock(
                side_effect=[
                    {"result": {"thread": {"id": "thread-1"}}},
                    RuntimeError(
                        "{'code': -32600, 'message': 'Invalid request: unknown variant "
                        "`thread/settings/update`'}"
                    ),
                    {"result": {"turn": {"id": "turn-1"}}},
                ]
            )

            async def _inject_turn_completed() -> None:
                await asyncio.sleep(0.01)
                session._events.put_nowait(
                    {"method": "turn/completed", "params": {"turn": {"id": "turn-1"}}}
                )

            inject_task = asyncio.create_task(_inject_turn_completed())
            async for _event in session.run_turn(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                system_prompt="",
                model="gpt-5.4-mini",
                cwd=".",
                sandbox="workspace-write",
                reasoning_effort="low",
            ):
                pass
            await inject_task

            methods = [call.args[0] for call in session._request.await_args_list]
            self.assertEqual(methods, ["thread/start", "thread/settings/update", "turn/start"])
            turn_params = session._request.await_args_list[2].args[1]
            self.assertEqual(turn_params["effort"], "low")
            self.assertEqual(turn_params["summary"], "detailed")
            self.assertEqual(session._applied_effort, "low")

        _run(_t())

    def test_app_server_run_turn_dedupes_unchanged_effort(self):
        """An unchanged effort is not re-sent on a later turn of one thread.

        Effort persists on the thread once applied, so re-issuing
        ``thread/settings/update`` every turn would be a redundant RPC.
        """

        async def _t():
            session = _CodexAppServerSession(
                codex_path="/bin/echo",
                cwd="/tmp/workspace",
                env={},
                tool_executor=None,
            )
            session.start = AsyncMock()
            session._proc = _FakeProcess()
            # Existing thread (no thread/start) with effort already applied.
            session.thread_id = "thread-1"
            session._applied_effort = "high"
            session._request = AsyncMock(side_effect=[{"result": {"turn": {"id": "turn-2"}}}])

            async def _inject_turn_completed() -> None:
                await asyncio.sleep(0.01)
                session._events.put_nowait(
                    {"method": "turn/completed", "params": {"turn": {"id": "turn-2"}}}
                )

            inject_task = asyncio.create_task(_inject_turn_completed())
            # Drive the turn to completion (consume the event stream for its
            # side effects — the RPCs we assert on below).
            async for _event in session.run_turn(
                messages=[{"role": "user", "content": "again"}],
                tools=[],
                system_prompt="",
                model="gpt-5.4-mini",
                cwd=".",
                sandbox="workspace-write",
                reasoning_effort="high",
            ):
                pass
            await inject_task

            methods = [call.args[0] for call in session._request.await_args_list]
            self.assertEqual(methods, ["turn/start"])

        _run(_t())

    def test_app_server_dynamic_tool_complete_carries_call_id_metadata(self):
        async def _t():
            session = _CodexAppServerSession(
                codex_path="/bin/echo",
                cwd="/tmp/workspace",
                env={},
                tool_executor=lambda name, args: {"ok": True, "name": name, "args": args},
            )
            session.start = AsyncMock()
            session._proc = _FakeProcess()
            session.thread_id = "thread-1"
            session._request = AsyncMock(return_value={"result": {"turn": {"id": "turn-1"}}})

            async def _inject_events() -> None:
                await asyncio.sleep(0.01)
                session._events.put_nowait(
                    {
                        "id": 7,
                        "method": "item/tool/call",
                        "params": {
                            "turnId": "turn-1",
                            "callId": "call_abc123",
                            "tool": "sys_os_shell",
                            "arguments": {"command": "pwd"},
                        },
                    }
                )
                session._events.put_nowait(
                    {
                        "method": "turn/completed",
                        "params": {"turn": {"id": "turn-1"}},
                    }
                )

            inject_task = asyncio.create_task(_inject_events())

            events = [
                event
                async for event in session.run_turn(
                    messages=[{"role": "user", "content": "pwd"}],
                    tools=[{"name": "sys_os_shell", "description": "Shell"}],
                    system_prompt="",
                    model="gpt-5.4-mini",
                    cwd=".",
                    sandbox="workspace-write",
                )
            ]

            await inject_task

            tool_request = next(e for e in events if isinstance(e, ToolCallRequest))
            tool_complete = next(e for e in events if isinstance(e, ToolCallComplete))
            self.assertEqual(tool_request.metadata, {"call_id": "call_abc123"})
            self.assertEqual(tool_complete.metadata, {"call_id": "call_abc123"})

        _run(_t())

    def test_app_server_run_turn_can_disable_native_tools_when_requested(self):
        async def _t():
            session = _CodexAppServerSession(
                codex_path="/bin/echo",
                cwd="/tmp/workspace",
                env={},
                tool_executor=None,
                disable_native_tools=True,
            )
            session.start = AsyncMock()
            session._proc = _FakeProcess()
            session._request = AsyncMock(
                side_effect=[
                    {"result": {"thread": {"id": "thread-1"}}},
                    {"result": {"turn": {"id": "turn-1"}}},
                ]
            )

            async def _inject_turn_completed() -> None:
                await asyncio.sleep(0.01)
                session._events.put_nowait(
                    {
                        "method": "turn/completed",
                        "params": {
                            "turn": {"id": "turn-1"},
                        },
                    }
                )

            inject_task = asyncio.create_task(_inject_turn_completed())
            _ = [
                event
                async for event in session.run_turn(
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[
                        {
                            "name": "sys_os_shell",
                            "description": "Shell",
                            "parameters": {
                                "type": "object",
                                "properties": {"command": {"type": "string"}},
                            },
                        }
                    ],
                    system_prompt="Be helpful.",
                    model="gpt-5.4-mini",
                    cwd=".",
                    sandbox="workspace-write",
                )
            ]
            await inject_task

            thread_start_call = session._request.await_args_list[0]
            params = thread_start_call.args[1]
            self.assertEqual(params["config"]["features"]["shell_tool"], False)

        _run(_t())

    def test_close_uses_process_tree_termination(self):
        async def _t():
            session = _CodexAppServerSession(
                codex_path="/bin/echo",
                cwd="/tmp/workspace",
                env={},
                tool_executor=None,
            )
            session._proc = _FakeProcess()
            session._started = True

            with patch("omnigent.inner.codex_executor._terminate_process_tree") as terminate_tree:
                await session.close()

            terminate_tree.assert_called_once()

        _run(_t())

    def test_signature_change_recreates_app_session(self):
        async def _t():
            first = _FakeAppSession([[TurnComplete(response="one")]])
            second = _FakeAppSession([[TurnComplete(response="two")]])
            sessions = [first, second]

            executor = CodexExecutor(
                codex_path="/bin/echo",
                app_session_factory=lambda **kwargs: sessions.pop(0),
            )

            first_events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hi", "session_id": "s1"}],
                    [],
                    "",
                )
            ]
            second_events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hi", "session_id": "s1"}],
                    [
                        {
                            "name": "sleep",
                            "description": "Sleep",
                            "parameters": {"type": "object", "properties": {}},
                        }
                    ],
                    "",
                )
            ]

            self.assertEqual(first_events[-1].response, "one")
            self.assertEqual(second_events[-1].response, "two")
            self.assertTrue(first.closed)
            self.assertFalse(second.closed)

        _run(_t())

    def test_close_session_closes_app_session(self):
        async def _t():
            fake_session = _FakeAppSession([[TurnComplete(response="done")]])
            executor = CodexExecutor(
                codex_path="/bin/echo",
                app_session_factory=lambda **kwargs: fake_session,
            )
            _ = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hi", "session_id": "s1"}],
                    [],
                    "",
                )
            ]
            await executor.close_session("s1")
            self.assertTrue(fake_session.closed)

        _run(_t())

    def test_enqueue_session_message_steers_active_turn(self):
        async def _t():
            fake_session = _FakeAppSession([[TurnComplete(response="done")]])
            fake_session.enqueue_message = AsyncMock(return_value=True)

            executor = CodexExecutor(
                codex_path="/bin/echo",
                app_session_factory=lambda **kwargs: fake_session,
            )

            _ = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hi", "session_id": "s1"}],
                    [],
                    "",
                )
            ]

            queued = await executor.enqueue_session_message("s1", "stop")
            self.assertTrue(queued)
            fake_session.enqueue_message.assert_awaited_once_with("stop")
            self.assertTrue(executor.supports_live_message_queue())

        _run(_t())

    def test_interrupt_session_interrupts_then_drops_session(self):
        """A user interrupt halts the turn AND drops the session.

        Codex resumes the same thread on the next turn and sends only the
        latest user message, so a retained session would bypass the
        runner's ``[System: interrupted]`` marker and continue the
        abandoned request. Dropping the session (close resets thread_id)
        forces the next turn to start a fresh thread and replay full
        history. An empty ``_session_states`` is the invariant that
        prevents the canceled-instruction leak.
        """

        async def _t():
            fake_session = _FakeAppSession([[TurnComplete(response="done")]])
            executor = CodexExecutor(
                codex_path="/bin/echo",
                app_session_factory=lambda **kwargs: fake_session,
            )

            _ = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hi", "session_id": "s1"}],
                    [],
                    "",
                )
            ]

            interrupted = await executor.interrupt_session("s1")
            self.assertTrue(interrupted)
            # Interrupt is attempted first to halt the in-flight turn.
            self.assertTrue(fake_session.interrupted)
            # Then the session is closed and removed so the next turn starts a
            # fresh thread and replays full history (marker included).
            self.assertTrue(fake_session.closed)
            self.assertEqual(executor._session_states, {})

        _run(_t())

    def test_app_server_uses_workspace_cwd(self):
        async def _t():
            fake_proc = _FakeProcess()
            recorded_cwd: str | None = None

            async def _fake_create_subprocess_exec(*args, **kwargs):
                nonlocal recorded_cwd
                recorded_cwd = kwargs.get("cwd")
                return fake_proc

            with tempfile.TemporaryDirectory() as workspace:
                session = _CodexAppServerSession(
                    codex_path="/bin/echo",
                    cwd=workspace,
                    env={},
                    tool_executor=None,
                )
                session._request = AsyncMock(return_value={"result": {}})

                with patch(
                    "omnigent.inner.codex_executor._create_subprocess_exec",
                    new=_fake_create_subprocess_exec,
                ):
                    await session.start()
                    assert recorded_cwd is not None
                    self.assertEqual(recorded_cwd, workspace)
                    await session.close()

                self.assertTrue(fake_proc.terminated)

        _run(_t())

    def test_app_server_uses_isolated_codex_home_and_cleans_it_up(self):
        """Codex subprocess must receive a private CODEX_HOME, not ~/.codex/."""

        async def _t():
            fake_proc = _FakeProcess()
            recorded_env: dict | None = None

            async def _fake_create_subprocess_exec(*args, **kwargs):
                nonlocal recorded_env
                recorded_env = kwargs.get("env")
                return fake_proc

            with tempfile.TemporaryDirectory() as workspace:
                session = _CodexAppServerSession(
                    codex_path="/bin/echo",
                    cwd=workspace,
                    env={},
                    tool_executor=None,
                )
                session._request = AsyncMock(return_value={"result": {}})

                with patch(
                    "omnigent.inner.codex_executor._create_subprocess_exec",
                    new=_fake_create_subprocess_exec,
                ):
                    await session.start()
                    assert recorded_env is not None
                    self.assertIn("CODEX_HOME", recorded_env)
                    codex_home = Path(recorded_env["CODEX_HOME"])
                    self.assertTrue(codex_home.is_dir())
                    self.assertTrue(codex_home.name.startswith("omnigent-codex-home-"))
                    self.assertTrue(str(codex_home).startswith(tempfile.gettempdir()))
                    # Must not point at the user's real ~/.codex directory.
                    self.assertNotEqual(codex_home, Path.home() / ".codex")
                    await session.close()

                self.assertFalse(codex_home.exists())

        _run(_t())

    def test_app_server_enqueue_message_uses_turn_steer(self):
        async def _t():
            session = _CodexAppServerSession(
                codex_path="/bin/echo",
                cwd="/tmp/workspace",
                env={},
                tool_executor=None,
            )
            session.thread_id = "thread-1"
            session.active_turn_id = "turn-1"
            session._request = AsyncMock(return_value={"result": {"turnId": "turn-1"}})

            queued = await session.enqueue_message("stop")

            self.assertTrue(queued)
            session._request.assert_awaited_once_with(
                "turn/steer",
                {
                    "threadId": "thread-1",
                    "expectedTurnId": "turn-1",
                    "input": [{"type": "text", "text": "stop"}],
                },
            )
            self.assertEqual(session.active_turn_id, "turn-1")

        _run(_t())

    def test_app_server_interrupt_turn_uses_turn_interrupt(self):
        async def _t():
            session = _CodexAppServerSession(
                codex_path="/bin/echo",
                cwd="/tmp/workspace",
                env={},
                tool_executor=None,
            )
            session.thread_id = "thread-1"
            session.active_turn_id = "turn-1"
            session._request = AsyncMock(return_value={"result": {}})

            interrupted = await session.interrupt_turn()

            self.assertTrue(interrupted)
            session._request.assert_awaited_once_with(
                "turn/interrupt",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                },
            )

        _run(_t())

    def test_app_server_run_turn_warns_and_keeps_waiting_when_events_are_slow(self):
        async def _t():
            session = _CodexAppServerSession(
                codex_path="/bin/echo",
                cwd="/tmp/workspace",
                env={},
                tool_executor=None,
            )
            session.start = AsyncMock()
            session._proc = _FakeProcess()
            session.thread_id = "thread-1"
            session._request = AsyncMock(return_value={"result": {"turn": {"id": "turn-1"}}})

            original_warn = _TURN_EVENT_WARN_SECONDS
            import omnigent.inner.codex_executor as codex_executor_module

            codex_executor_module._TURN_EVENT_WARN_SECONDS = 0.05
            try:

                async def _inject_final_after_warning() -> None:
                    # Let the warning fire at least once, then deliver a terminal event.
                    await asyncio.sleep(0.12)
                    session._events.put_nowait(
                        {
                            "method": "item/completed",
                            "params": {
                                "turnId": "turn-1",
                                "item": {
                                    "id": "msg-1",
                                    "type": "agentMessage",
                                    "text": "done",
                                    "phase": "final_answer",
                                },
                            },
                        }
                    )

                inject_task = asyncio.create_task(_inject_final_after_warning())
                with self.assertLogs("omnigent.inner.codex_executor", level="WARNING") as cm:
                    events = [
                        event
                        async for event in session.run_turn(
                            messages=[{"role": "user", "content": "hi"}],
                            tools=[],
                            system_prompt="",
                            model="gpt-5.4-mini",
                            cwd=".",
                            sandbox="workspace-write",
                        )
                    ]
                await inject_task
            finally:
                codex_executor_module._TURN_EVENT_WARN_SECONDS = original_warn

            self.assertTrue(
                any("has been idle for" in line for line in cm.output),
                cm.output,
            )
            self.assertTrue(
                any(type(evt).__name__ == "TurnComplete" for evt in events),
                events,
            )

        _run(_t())

    def test_app_server_run_turn_finishes_on_final_answer_item_without_turn_completed(self):
        async def _t():
            session = _CodexAppServerSession(
                codex_path="/bin/echo",
                cwd="/tmp/workspace",
                env={},
                tool_executor=None,
            )
            session.start = AsyncMock()
            session._proc = _FakeProcess()
            session.thread_id = "thread-1"
            session._request = AsyncMock(return_value={"result": {"turn": {"id": "turn-1"}}})

            await session._events.put(
                {
                    "method": "item/completed",
                    "params": {
                        "turnId": "turn-1",
                        "item": {
                            "id": "msg-1",
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "done",
                        },
                    },
                }
            )

            events = [
                event
                async for event in session.run_turn(
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                    system_prompt="",
                    model="gpt-5.4-mini",
                    cwd=".",
                    sandbox="workspace-write",
                )
            ]

            self.assertEqual(len(events), 2)
            self.assertIsInstance(events[0], TextChunk)
            self.assertEqual(events[0].text, "done")
            self.assertIsInstance(events[1], TurnComplete)
            self.assertEqual(events[1].response, "done")
            self.assertIsNone(session.active_turn_id)

        _run(_t())

    def test_app_server_run_turn_attaches_token_usage_to_turn_complete(self):
        """``thread/tokenUsage/updated`` payloads populate ``TurnComplete.usage``.

        Without forwarding the codex token-usage notification, harness-backed
        codex tests produce empty token-usage artifacts even though the
        provider counted real tokens. This test asserts the inner executor
        captures the latest ``tokenUsage.last`` breakdown and attaches it
        to the next ``TurnComplete`` it yields.
        """

        async def _t():
            session = _CodexAppServerSession(
                codex_path="/bin/echo",
                cwd="/tmp/workspace",
                env={},
                tool_executor=None,
            )
            session.start = AsyncMock()
            session._proc = _FakeProcess()
            session.thread_id = "thread-1"
            session._request = AsyncMock(return_value={"result": {"turn": {"id": "turn-1"}}})

            async def _inject_events() -> None:
                await asyncio.sleep(0.01)
                session._events.put_nowait(
                    {
                        "method": "thread/tokenUsage/updated",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "tokenUsage": {
                                "last": {
                                    "inputTokens": 100,
                                    "outputTokens": 25,
                                    "totalTokens": 125,
                                    "cachedInputTokens": 0,
                                    "reasoningOutputTokens": 0,
                                },
                                "total": {
                                    "inputTokens": 100,
                                    "outputTokens": 25,
                                    "totalTokens": 125,
                                    "cachedInputTokens": 0,
                                    "reasoningOutputTokens": 0,
                                },
                            },
                        },
                    }
                )
                await asyncio.sleep(0.01)
                session._events.put_nowait(
                    {
                        "method": "turn/completed",
                        "params": {"turn": {"id": "turn-1"}},
                    }
                )

            inject_task = asyncio.create_task(_inject_events())
            events = [
                event
                async for event in session.run_turn(
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                    system_prompt="",
                    model="gpt-5.4-mini",
                    cwd=".",
                    sandbox="workspace-write",
                )
            ]
            await inject_task

            self.assertEqual(len(events), 1)
            self.assertIsInstance(events[0], TurnComplete)
            self.assertEqual(
                events[0].usage,
                {"input_tokens": 100, "output_tokens": 25, "total_tokens": 125},
            )
            # The cached usage must be cleared after consumption so the next
            # turn doesn't inherit stale numbers.
            self.assertIsNone(session._last_turn_usage)

        _run(_t())

    def test_app_server_run_turn_notifies_usage_observer(self):
        """The inner executor's ``TurnComplete`` yield site notifies the
        shared usage observer so integration tests that drive the codex
        executor directly populate the per-test token-usage artifact."""
        from omnigent.llms import _usage_observer

        async def _t():
            session = _CodexAppServerSession(
                codex_path="/bin/echo",
                cwd="/tmp/workspace",
                env={},
                tool_executor=None,
            )
            session.start = AsyncMock()
            session._proc = _FakeProcess()
            session.thread_id = "thread-1"
            session._request = AsyncMock(return_value={"result": {"turn": {"id": "turn-1"}}})

            seen: list[dict[str, object]] = []
            remove = _usage_observer.add_observer(lambda **kw: seen.append(kw))

            async def _inject_events() -> None:
                await asyncio.sleep(0.01)
                session._events.put_nowait(
                    {
                        "method": "thread/tokenUsage/updated",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "tokenUsage": {
                                "last": {
                                    "inputTokens": 100,
                                    "outputTokens": 25,
                                    "totalTokens": 125,
                                    "cachedInputTokens": 0,
                                    "reasoningOutputTokens": 0,
                                },
                                "total": {
                                    "inputTokens": 100,
                                    "outputTokens": 25,
                                    "totalTokens": 125,
                                    "cachedInputTokens": 0,
                                    "reasoningOutputTokens": 0,
                                },
                            },
                        },
                    }
                )
                await asyncio.sleep(0.01)
                session._events.put_nowait(
                    {
                        "method": "turn/completed",
                        "params": {"turn": {"id": "turn-1"}},
                    }
                )

            inject_task = asyncio.create_task(_inject_events())
            try:
                [
                    event
                    async for event in session.run_turn(
                        messages=[{"role": "user", "content": "hi"}],
                        tools=[],
                        system_prompt="",
                        model="codex-test-model",
                        cwd=".",
                        sandbox="workspace-write",
                    )
                ]
                await inject_task
            finally:
                remove()

            self.assertEqual(
                seen,
                [
                    {
                        "model": "codex-test-model",
                        "input_tokens": 100,
                        "output_tokens": 25,
                        "total_tokens": 125,
                    }
                ],
            )

        _run(_t())

    def test_app_server_run_turn_usage_none_when_no_token_notification(self):
        """Without a ``thread/tokenUsage/updated`` event, ``TurnComplete.usage`` is None."""

        async def _t():
            session = _CodexAppServerSession(
                codex_path="/bin/echo",
                cwd="/tmp/workspace",
                env={},
                tool_executor=None,
            )
            session.start = AsyncMock()
            session._proc = _FakeProcess()
            session.thread_id = "thread-1"
            session._request = AsyncMock(return_value={"result": {"turn": {"id": "turn-1"}}})

            async def _inject_completed() -> None:
                await asyncio.sleep(0.01)
                session._events.put_nowait(
                    {
                        "method": "turn/completed",
                        "params": {"turn": {"id": "turn-1"}},
                    }
                )

            inject_task = asyncio.create_task(_inject_completed())
            events = [
                event
                async for event in session.run_turn(
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                    system_prompt="",
                    model="gpt-5.4-mini",
                    cwd=".",
                    sandbox="workspace-write",
                )
            ]
            await inject_task

            self.assertEqual(len(events), 1)
            self.assertIsInstance(events[0], TurnComplete)
            self.assertIsNone(events[0].usage)

        _run(_t())

    def test_app_server_run_turn_drains_final_answer_after_turn_completed(self):
        async def _t():
            session = _CodexAppServerSession(
                codex_path="/bin/echo",
                cwd="/tmp/workspace",
                env={},
                tool_executor=None,
            )
            session.start = AsyncMock()
            session._proc = _FakeProcess()
            session.thread_id = "thread-1"
            session._request = AsyncMock(return_value={"result": {"turn": {"id": "turn-1"}}})

            async def _inject_trailing_final_answer() -> None:
                await asyncio.sleep(0.01)
                session._events.put_nowait(
                    {
                        "method": "turn/completed",
                        "params": {
                            "turn": {"id": "turn-1"},
                        },
                    }
                )
                await asyncio.sleep(0.01)
                session._events.put_nowait(
                    {
                        "method": "item/completed",
                        "params": {
                            "turnId": "turn-1",
                            "item": {
                                "id": "msg-1",
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": "done",
                            },
                        },
                    }
                )

            inject_task = asyncio.create_task(_inject_trailing_final_answer())
            events = [
                event
                async for event in session.run_turn(
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                    system_prompt="",
                    model="gpt-5.4-mini",
                    cwd=".",
                    sandbox="workspace-write",
                )
            ]
            await inject_task

            self.assertEqual(len(events), 1)
            self.assertIsInstance(events[0], TurnComplete)
            self.assertEqual(events[0].response, "done")

        _run(_t())

    def test_app_server_run_turn_uses_buffered_delta_when_completed_item_is_missing(self):
        async def _t():
            session = _CodexAppServerSession(
                codex_path="/bin/echo",
                cwd="/tmp/workspace",
                env={},
                tool_executor=None,
            )
            session.start = AsyncMock()
            session._proc = _FakeProcess()
            session.thread_id = "thread-1"
            session._request = AsyncMock(return_value={"result": {"turn": {"id": "turn-1"}}})

            await session._events.put(
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "turnId": "turn-1",
                        "itemId": "msg-1",
                        "delta": "done",
                    },
                }
            )
            await session._events.put(
                {
                    "method": "turn/completed",
                    "params": {
                        "turn": {"id": "turn-1"},
                    },
                }
            )

            events = [
                event
                async for event in session.run_turn(
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[],
                    system_prompt="",
                    model="gpt-5.4-mini",
                    cwd=".",
                    sandbox="workspace-write",
                )
            ]

            self.assertEqual(len(events), 1)
            self.assertIsInstance(events[0], TurnComplete)
            self.assertEqual(events[0].response, "done")

        _run(_t())

    def test_app_server_run_turn_reasoning_deltas_yield_reasoning_chunks(self):
        """item/reasoning/textDelta and item/reasoning/summaryTextDelta events
        yield ReasoningChunk events so the idle watchdog resets during long
        think phases (regression guard for omnigent-ai/omnigent#738)."""

        async def _t():
            session = _CodexAppServerSession(
                codex_path="/bin/echo",
                cwd="/tmp/workspace",
                env={},
                tool_executor=None,
            )
            session.start = AsyncMock()
            session._proc = _FakeProcess()
            session.thread_id = "thread-1"
            session._request = AsyncMock(return_value={"result": {"turn": {"id": "turn-1"}}})

            async def _inject() -> None:
                await asyncio.sleep(0.01)
                session._events.put_nowait(
                    {
                        "method": "item/reasoning/textDelta",
                        "params": {"turnId": "turn-1", "delta": "thinking hard..."},
                    }
                )
                session._events.put_nowait(
                    {
                        "method": "item/reasoning/summaryTextDelta",
                        "params": {"turnId": "turn-1", "delta": "summary of thoughts"},
                    }
                )
                session._events.put_nowait(
                    {
                        "method": "item/completed",
                        "params": {
                            "turnId": "turn-1",
                            "item": {
                                "id": "msg-1",
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": "Here is my answer.",
                            },
                        },
                    }
                )

            inject_task = asyncio.create_task(_inject())
            events = [
                event
                async for event in session.run_turn(
                    messages=[{"role": "user", "content": "complex question"}],
                    tools=[],
                    system_prompt="",
                    model="gpt-5.4-mini",
                    cwd=".",
                    sandbox="workspace-write",
                )
            ]
            await inject_task

            reasoning_events = [e for e in events if isinstance(e, ReasoningChunk)]
            self.assertEqual(len(reasoning_events), 2)
            self.assertEqual(reasoning_events[0].delta, "thinking hard...")
            self.assertEqual(reasoning_events[0].event_type, "reasoning_text")
            self.assertEqual(reasoning_events[1].delta, "summary of thoughts")
            self.assertEqual(reasoning_events[1].event_type, "reasoning_text")

            turn_complete = events[-1]
            self.assertIsInstance(turn_complete, TurnComplete)
            self.assertEqual(turn_complete.response, "Here is my answer.")

        _run(_t())

    def test_stderr_loop_handles_oversized_lines(self):
        async def _t():
            session = _CodexAppServerSession(
                codex_path="/bin/echo",
                cwd="/tmp/workspace",
                env={},
                tool_executor=None,
            )
            proc = _FakeProcess()
            proc.stderr = _OverflowingPipe()
            session._proc = proc

            await session._stderr_loop()

            self.assertEqual(len(session._recent_stderr), 1)
            self.assertTrue(session._recent_stderr[0].endswith("...[truncated]"))

        _run(_t())

    def test_reader_loop_handles_large_json_message_split_across_chunks(self):
        async def _t():
            session = _CodexAppServerSession(
                codex_path="/bin/echo",
                cwd="/tmp/workspace",
                env={},
                tool_executor=None,
            )
            proc = _FakeProcess()
            payload = {
                "method": "item/completed",
                "params": {
                    "turnId": "turn-1",
                    "item": {
                        "id": "msg-1",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "x" * 70000,
                    },
                },
            }
            encoded = (json.dumps(payload) + "\n").encode("utf-8")
            proc.stdout = _ChunkedPipe([encoded[:100], encoded[100:50000], encoded[50000:]])
            session._proc = proc

            reader_task = asyncio.create_task(session._reader_loop())
            await reader_task

            message = await asyncio.wait_for(session._events.get(), timeout=0.1)
            self.assertEqual(message["method"], "item/completed")
            self.assertEqual(message["params"]["item"]["phase"], "final_answer")
            self.assertEqual(len(message["params"]["item"]["text"]), 70000)

        _run(_t())


# ── Retryable ExecutorError emission ──────────────────────────
#
# Function-based pytest tests for the retryable-flag behavior on the
# two codex app-server failure paths. Kept outside the unittest class
# above to comply with the project-wide function-based test rule —
# the class-style tests above pre-date that rule.


async def test_run_turn_turn_failed_emits_retryable_executor_error() -> None:
    """
    When the codex app server reports ``method == "turn/failed"`` for
    the active turn, ``_CodexAppServerSession.run_turn`` must emit
    exactly one ``ExecutorError`` with ``retryable=True``. Without the
    retryable flag the workflow raises ``PermanentLLMError`` and the
    surrounding retry policy never gets a chance to reissue a turn
    that failed for a transient reason (provider hiccup, tool exit
    code, etc.).

    If this breaks (e.g. someone drops ``retryable=True``), the user
    sees transient codex failures as terminal task errors instead of
    a transparent retry.
    """
    session = _CodexAppServerSession(
        codex_path="/bin/echo",
        cwd="/tmp/workspace",
        env={},
        tool_executor=None,
    )
    session.start = AsyncMock()
    session._proc = _FakeProcess()
    session.thread_id = "thread-1"
    session._request = AsyncMock(return_value={"result": {"turn": {"id": "turn-1"}}})

    # ``turnId`` must be set in ``params`` so the startup drain at
    # codex_executor.py:550 preserves the event for the main loop —
    # real codex frames carry it alongside the nested ``turn.id``.
    await session._events.put(
        {
            "method": "turn/failed",
            "params": {
                "turnId": "turn-1",
                "turn": {"id": "turn-1", "error": "bash exit 127: printf: illegal usage"},
                "message": "bash exit 127: printf: illegal usage",
            },
        }
    )

    events = [
        event
        async for event in session.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
            model="gpt-5.4-mini",
            cwd=".",
            sandbox="workspace-write",
        )
    ]

    # Exactly one ExecutorError — anything else means the terminal
    # event stream leaked extra events past the failure.
    error_events = [e for e in events if isinstance(e, ExecutorError)]
    assert len(error_events) == 1, (
        f"Expected exactly 1 ExecutorError on turn/failed, got {len(error_events)}: {events}"
    )
    assert error_events[0].retryable is True
    assert "bash exit 127" in error_events[0].message


async def test_run_turn_method_error_emits_retryable_executor_error() -> None:
    """
    When the codex app server emits a top-level ``method == "error"``
    JSON-RPC frame (typical for runtime / tool-execution failures),
    ``run_turn`` must emit ``ExecutorError(retryable=True)`` so the
    workflow's retry policy can reissue. Matches the behavior for
    ``turn/failed`` — both paths carry transient provider-side errors.
    """
    session = _CodexAppServerSession(
        codex_path="/bin/echo",
        cwd="/tmp/workspace",
        env={},
        tool_executor=None,
    )
    session.start = AsyncMock()
    session._proc = _FakeProcess()
    session.thread_id = "thread-1"
    session._request = AsyncMock(return_value={"result": {"turn": {"id": "turn-1"}}})

    # ``turnId`` is included only so the startup drain at
    # codex_executor.py:550 preserves the event for the main loop;
    # the ``method == error`` handler itself ignores ``turnId``.
    await session._events.put(
        {
            "method": "error",
            "params": {"turnId": "turn-1", "message": "shell command crashed"},
        }
    )

    events = [
        event
        async for event in session.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
            model="gpt-5.4-mini",
            cwd=".",
            sandbox="workspace-write",
        )
    ]

    error_events = [e for e in events if isinstance(e, ExecutorError)]
    assert len(error_events) == 1, (
        f"Expected exactly 1 ExecutorError on method=error, got {len(error_events)}: {events}"
    )
    assert error_events[0].retryable is True
    assert "shell command crashed" in error_events[0].message


async def test_run_turn_refuses_to_adopt_stale_final_answer_event() -> None:
    """Adopt fallback must drop a stale final-answer item rather
    than adopt it as the new turn's response.
    """
    session = _CodexAppServerSession(
        codex_path="/bin/echo",
        cwd="/tmp/workspace",
        env={},
        tool_executor=None,
    )
    session.start = AsyncMock()
    session._proc = _FakeProcess()
    session.thread_id = "thread-1"
    # ``turn/start`` reports the new turn id (turn-2). The first event
    # the executor sees is a stale final-answer item carrying turn-1's
    # id (the prior turn's tail), followed immediately by the real
    # turn-2 final-answer.
    session._request = AsyncMock(return_value={"result": {"turn": {"id": "turn-2"}}})

    async def _inject_stale_then_real() -> None:
        await asyncio.sleep(0.01)
        # Stale tail: prior turn's final answer arrives during turn-2.
        session._events.put_nowait(
            {
                "method": "item/completed",
                "params": {
                    "turnId": "turn-1",
                    "item": {
                        "type": "agentMessage",
                        "id": "msg-stale",
                        "text": "STALE",
                        "phase": "final_answer",
                    },
                },
            }
        )
        # The real turn-2 final answer.
        session._events.put_nowait(
            {
                "method": "item/completed",
                "params": {
                    "turnId": "turn-2",
                    "item": {
                        "type": "agentMessage",
                        "id": "msg-real",
                        "text": "REAL",
                        "phase": "final_answer",
                    },
                },
            }
        )

    inject_task = asyncio.create_task(_inject_stale_then_real())
    events = [
        event
        async for event in session.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
            model="gpt-5.4-mini",
            cwd=".",
            sandbox="workspace-write",
        )
    ]
    await inject_task

    turn_completes = [e for e in events if isinstance(e, TurnComplete)]
    assert len(turn_completes) == 1, (
        f"Expected exactly one TurnComplete, got {len(turn_completes)}: {events}"
    )
    # ``STALE`` here means the adopt fallback fired on the stale
    # final-answer; the fix is meant to drop it instead.
    assert turn_completes[0].response == "REAL", (
        f"Expected 'REAL' (real turn-2 response); got "
        f"{turn_completes[0].response!r}. If 'STALE', the adopt "
        f"fallback adopted a stale final-answer event again."
    )
    # The instance attribute keeps the announced turn id; the adopted
    # path is only the local rebinding inside ``_event_turn_matches``.
    assert session.active_turn_id is None


async def test_run_turn_still_adopts_non_terminal_first_event() -> None:
    """Non-terminal first events (deltas, tool calls) must still be
    adopted; the narrowing only refuses terminal final-answer items.
    """
    session = _CodexAppServerSession(
        codex_path="/bin/echo",
        cwd="/tmp/workspace",
        env={},
        tool_executor=None,
    )
    session.start = AsyncMock()
    session._proc = _FakeProcess()
    session.thread_id = "thread-1"
    session._request = AsyncMock(return_value={"result": {"turn": {"id": "turn-2"}}})

    async def _inject_events() -> None:
        await asyncio.sleep(0.01)
        # First event: a non-terminal delta carrying a different id.
        # Adopt should kick in (the original Codex quirk).
        session._events.put_nowait(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "turnId": "turn-2-rewritten",
                    "itemId": "msg-1",
                    "delta": "Hello",
                },
            }
        )
        # Final answer with the adopted id, terminating the turn.
        session._events.put_nowait(
            {
                "method": "item/completed",
                "params": {
                    "turnId": "turn-2-rewritten",
                    "item": {
                        "type": "agentMessage",
                        "id": "msg-1",
                        "text": "Hello",
                        "phase": "final_answer",
                    },
                },
            }
        )

    inject_task = asyncio.create_task(_inject_events())
    events = [
        event
        async for event in session.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
            model="gpt-5.4-mini",
            cwd=".",
            sandbox="workspace-write",
        )
    ]
    await inject_task

    turn_completes = [e for e in events if isinstance(e, TurnComplete)]
    assert len(turn_completes) == 1
    # The adopted id's deltas + final answer flow through normally.
    assert turn_completes[0].response == "Hello"


def test_format_codex_error_params_extracts_provider_error_from_nested_error() -> None:
    """
    Codex App Server's ``method == "error"`` frames carry the
    actual provider failure inside ``params["error"]["message"]``,
    typically as a stringified JSON blob like
    ``'{"error_code":"BAD_REQUEST","message":"..."}'``.
    The formatter must unwrap two layers (the codex envelope
    and the provider's JSON) so the user sees the human-readable
    line, not the raw nested structure.

    What breaks if this fails: users hit a config mismatch (Claude
    model on a codex harness, bad profile, unsupported model on the
    Databricks Responses passthrough) and see only the bare fallback
    "Codex App Server error" — no clue why. The 2026-04-28 user
    report was exactly this: codex+claude-opus-4-6 on Databricks
    surfaced as "Codex App Server error" with zero diagnostic info.
    """
    from omnigent.inner.codex_executor import _format_codex_error_params

    params = {
        "error": {
            "message": (
                '{"error_code":"BAD_REQUEST","message":'
                '"Responses API passthrough is not supported for '
                'model databricks-claude-opus-4-6."}'
            ),
            "codexErrorInfo": "other",
            "additionalDetails": None,
        },
        "willRetry": False,
        "threadId": "abc",
        "turnId": "def",
    }
    result = _format_codex_error_params(params)
    # Provider's user-facing message must appear, with the error_code
    # alongside it.
    assert "Responses API passthrough is not supported" in result
    assert "BAD_REQUEST" in result
    # The bare fallback string must NOT appear — that's the "you
    # silently ate my diagnostic" symptom this fix prevents.
    assert "no params" not in result
    # The codex-envelope's ``codexErrorInfo: "other"`` is generic
    # and should be dropped from the output (we filter it
    # explicitly to keep the message tight).
    assert "codexErrorInfo" not in result


def test_format_codex_error_params_falls_back_to_raw_when_no_known_fields() -> None:
    """
    A truly opaque error frame (no message / code / data / nested
    error.message) must still surface SOMETHING — dump the raw
    params dict. We never want the user to see a bare
    "Codex App Server error" with no hint about what went wrong.
    """
    from omnigent.inner.codex_executor import _format_codex_error_params

    params = {"someField": "someValue", "willRetry": False}
    result = _format_codex_error_params(params)
    assert "raw_params" in result
    assert "someField" in result


def test_format_codex_error_params_handles_missing_params() -> None:
    """
    None / empty / non-dict params must produce a stable fallback
    string — never crash, never empty.
    """
    from omnigent.inner.codex_executor import _format_codex_error_params

    assert "no params" in _format_codex_error_params(None)
    assert "no params" in _format_codex_error_params({})
    assert "no params" in _format_codex_error_params("not a dict")


def test_extract_codex_last_turn_usage_splits_cached_out_of_input() -> None:
    """``tokenUsage.last`` maps onto TurnComplete.usage, splitting cached tokens.

    Codex's ``inputTokens`` is inclusive of cached tokens, so the cached
    portion is moved into ``cache_read_input_tokens`` and ``input_tokens``
    keeps only the non-cached remainder — otherwise the server prices cached
    tokens at the full input rate. If ``input_tokens`` came back as 7 (not 6)
    and there were no ``cache_read_input_tokens`` key, the split regressed.
    """
    from omnigent.inner.codex_executor import _extract_codex_last_turn_usage

    params = {
        "threadId": "t1",
        "turnId": "turn-1",
        "tokenUsage": {
            "last": {
                "inputTokens": 7,
                "outputTokens": 3,
                "totalTokens": 10,
                "cachedInputTokens": 1,
                "reasoningOutputTokens": 0,
            },
            "total": {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150},
        },
    }
    assert _extract_codex_last_turn_usage(params) == {
        "input_tokens": 6,  # 7 total input - 1 cached
        "output_tokens": 3,
        "total_tokens": 10,
        "cache_read_input_tokens": 1,
    }


def test_extract_codex_last_turn_usage_no_cache_key_when_uncached() -> None:
    """No ``cachedInputTokens`` ⇒ input unchanged and no cache_read key added.

    Guards against synthesizing a phantom cache bucket (which would shrink
    the non-cached input the server bills at the full rate).
    """
    from omnigent.inner.codex_executor import _extract_codex_last_turn_usage

    params = {"tokenUsage": {"last": {"inputTokens": 7, "outputTokens": 3, "totalTokens": 10}}}
    assert _extract_codex_last_turn_usage(params) == {
        "input_tokens": 7,
        "output_tokens": 3,
        "total_tokens": 10,
    }


def test_extract_codex_last_turn_usage_handles_missing_or_malformed() -> None:
    """Missing or non-dict shapes return None rather than raising."""
    from omnigent.inner.codex_executor import _extract_codex_last_turn_usage

    assert _extract_codex_last_turn_usage(None) is None
    assert _extract_codex_last_turn_usage("not a dict") is None
    assert _extract_codex_last_turn_usage({}) is None
    assert _extract_codex_last_turn_usage({"tokenUsage": None}) is None
    assert _extract_codex_last_turn_usage({"tokenUsage": {"total": {}}}) is None


def _make_skill_dir(root: Path, name: str) -> Path:
    """Create a minimal valid skill directory for the populator tests."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\n# {name}\n")
    return skill_dir


def test_populate_codex_skills_all(tmp_path: Path) -> None:
    """``skills_filter='all'`` symlinks every available skill from
    every source.

    Mirrors the SDK semantics where ``skills='all'`` exposes
    every host-discovered skill. The populator is the codex
    equivalent — it materializes the union of all sources into
    the per-conversation ``$CODEX_HOME/skills/`` so codex's
    auto-discovery surfaces them all.
    """
    from omnigent.inner.codex_executor import _populate_codex_skills

    host_skills = tmp_path / "host"
    bundle_skills = tmp_path / "bundle"
    target = tmp_path / "codex_home_skills"

    _make_skill_dir(host_skills, "alpha")
    _make_skill_dir(host_skills, "beta")
    _make_skill_dir(bundle_skills, "gamma")

    _populate_codex_skills(target, "all", [bundle_skills, host_skills])

    # Every skill from both sources is present as a symlink. If
    # the populator only scanned one source, this would miss
    # either ``gamma`` (bundle-only) or ``alpha``/``beta``
    # (host-only) — the failure message would name the missing
    # entry.
    assert sorted(p.name for p in target.iterdir()) == ["alpha", "beta", "gamma"]
    assert all((target / name).is_symlink() for name in ["alpha", "beta", "gamma"])


def test_populate_codex_skills_none(tmp_path: Path) -> None:
    """``skills_filter='none'`` leaves the target dir absent
    entirely.

    Codex's discovery walks ``$CODEX_HOME/skills/`` — if the
    directory doesn't exist, no skills load. This is the
    hermetic-agent regression-pin: Omnigent must produce no skill
    surface for ``skills: none`` even when host
    ``~/.codex/skills/`` is populated.
    """
    from omnigent.inner.codex_executor import _populate_codex_skills

    host_skills = tmp_path / "host"
    target = tmp_path / "codex_home_skills"
    _make_skill_dir(host_skills, "alpha")

    _populate_codex_skills(target, "none", [host_skills])

    # Target dir must not exist (codex's scan no-ops cleanly when
    # the path is absent). A directory with zero entries would
    # still pass codex's existence check but is wasteful — the
    # absent-dir contract is tighter.
    assert not target.exists()


def test_populate_codex_skills_named_subset(tmp_path: Path) -> None:
    """``skills_filter=[name, ...]`` exposes only the named
    skills.

    Names not present in any source are silently skipped (the
    SDK semantic — missing skill is no-op, not an error).
    Names present in multiple sources resolve to the
    first-listed source (so callers can express priority by
    ordering ``sources``).
    """
    from omnigent.inner.codex_executor import _populate_codex_skills

    host_skills = tmp_path / "host"
    bundle_skills = tmp_path / "bundle"
    target = tmp_path / "codex_home_skills"

    _make_skill_dir(host_skills, "alpha")
    _make_skill_dir(host_skills, "beta")
    _make_skill_dir(bundle_skills, "alpha")  # name collision
    _make_skill_dir(bundle_skills, "gamma")

    _populate_codex_skills(
        target,
        ["alpha", "gamma", "missing_skill"],
        [bundle_skills, host_skills],  # bundle wins
    )

    # ``alpha`` resolves to the bundle copy (first source listed).
    # ``gamma`` is bundle-only. ``beta`` is host-only and not
    # named — must NOT appear. ``missing_skill`` doesn't exist
    # anywhere — must be silently dropped, not raised.
    assert sorted(p.name for p in target.iterdir()) == ["alpha", "gamma"]
    alpha_target = (target / "alpha").resolve()
    assert alpha_target == (bundle_skills / "alpha").resolve(), (
        f"name collision should resolve to first source (bundle), but resolved to {alpha_target}"
    )


def test_populate_codex_skills_from_bundle_links_bundle_skills(tmp_path: Path) -> None:
    """
    ``populate_codex_skills_from_bundle`` links a bundle's ``skills/`` into
    ``<codex_home>/skills/``.

    This is the shared helper both the wrapped codex executor and the
    codex-native launch path use, so a deployed agent's bundled skills
    reach the native Codex CLI (the gap before this change: codex-native
    populated no skills). Fails if the bundle source isn't scanned or the
    target isn't created under ``<codex_home>/skills``.
    """
    from omnigent.inner.codex_executor import populate_codex_skills_from_bundle

    bundle = tmp_path / "bundle"
    _make_skill_dir(bundle / "skills", "authoring")
    codex_home = tmp_path / "codex_home"

    populate_codex_skills_from_bundle(codex_home, bundle, "all")

    linked = codex_home / "skills" / "authoring"
    assert linked.is_symlink() or linked.is_dir()
    assert (linked / "SKILL.md").is_file()


def test_populate_codex_skills_from_bundle_none_leaves_no_dir(tmp_path: Path) -> None:
    """
    ``skills_filter="none"`` produces no ``skills/`` dir even when the
    bundle ships skills — the codex-native parity for a hermetic agent.
    """
    from omnigent.inner.codex_executor import populate_codex_skills_from_bundle

    bundle = tmp_path / "bundle"
    _make_skill_dir(bundle / "skills", "authoring")
    codex_home = tmp_path / "codex_home"

    populate_codex_skills_from_bundle(codex_home, bundle, "none")

    assert not (codex_home / "skills").exists()


# ---------------------------------------------------------------------------
# _populate_codex_home_config tests
# ---------------------------------------------------------------------------


def test_populate_codex_home_config_symlinks_auth_and_config(tmp_path: Path) -> None:
    """``auth.json`` is symlinked; ``config.toml`` is copied (not symlinked).

    ``auth.json`` is symlinked so OAuth token refreshes written to the real
    home propagate to running sessions. ``config.toml`` is copied so an
    in-TUI ``/model`` command writes only to the session's private copy and
    never mutates the shared ``~/.codex/config.toml``.
    """
    from omnigent.inner.codex_executor import _populate_codex_home_config

    source = tmp_path / "real_codex_home"
    source.mkdir()
    (source / "auth.json").write_text('{"auth_mode": "chatgpt"}')
    (source / "config.toml").write_text('[default]\nmodel = "gpt-5.4"')
    (source / "AGENTS.md").write_text("global guidance")
    (source / "AGENTS.override.md").write_text("global override")
    target = tmp_path / "temp_codex_home"
    target.mkdir()

    _populate_codex_home_config(target, source)

    # auth.json: symlink so live credential refreshes propagate.
    assert (target / "auth.json").is_symlink()
    assert (target / "auth.json").read_text() == '{"auth_mode": "chatgpt"}'
    assert (target / "AGENTS.md").is_symlink()
    assert (target / "AGENTS.md").read_text() == "global guidance"
    assert (target / "AGENTS.override.md").is_symlink()
    assert (target / "AGENTS.override.md").read_text() == "global override"
    # config.toml: independent copy so /model writes stay session-local.
    assert not (target / "config.toml").is_symlink()
    assert (target / "config.toml").is_file()
    assert (target / "config.toml").read_text() == '[default]\nmodel = "gpt-5.4"'


def test_populate_codex_home_config_minimal_mode_keeps_only_provider_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Title sidecars retain auth/provider config without loading extensions."""
    from omnigent.inner.codex_executor import _populate_codex_home_config

    source = tmp_path / "real_codex_home"
    source.mkdir()
    (source / "auth.json").write_text('{"auth_mode": "chatgpt"}')
    (source / "AGENTS.md").write_text("global guidance")
    (source / "config.toml").write_text(
        'model_provider = "Databricks"\n'
        '[model_providers.Databricks]\nname = "Databricks"\nbase_url = "https://example"\n'
        "[plugins.example]\nenabled = true\n"
        '[mcp_servers.github]\nenabled = true\ncommand = "github-mcp"\n'
        '[marketplaces.example]\nsource = "https://example"\n'
    )
    target = tmp_path / "temp_codex_home"
    target.mkdir()
    monkeypatch.setenv("HARNESS_CODEX_MINIMAL_CONFIG", "1")

    _populate_codex_home_config(target, source)

    assert (target / "auth.json").is_symlink()
    assert not (target / "AGENTS.md").exists()
    config_text = (target / "config.toml").read_text()
    assert 'model_provider = "Databricks"' in config_text
    assert "[model_providers.Databricks]" in config_text
    assert "plugins" not in config_text
    assert "mcp_servers" not in config_text
    assert "marketplaces" not in config_text


def test_populate_codex_home_config_config_toml_copy_is_isolated(tmp_path: Path) -> None:
    """Writing to the session's ``config.toml`` copy does not affect the source.

    Proves that ``/model`` inside a session cannot mutate the shared
    ``~/.codex/config.toml`` and silently change another session's model or
    cost-policy enforcement.
    """
    from omnigent.inner.codex_executor import _populate_codex_home_config

    source = tmp_path / "real_codex_home"
    source.mkdir()
    (source / "config.toml").write_text('[default]\nmodel = "gpt-5.4"')
    target = tmp_path / "temp_codex_home"
    target.mkdir()

    _populate_codex_home_config(target, source)

    # Simulate a /model write inside the session.
    (target / "config.toml").write_text('[default]\nmodel = "gpt-4o-mini"')

    # Source must be untouched.
    assert (source / "config.toml").read_text() == '[default]\nmodel = "gpt-5.4"'


def test_populate_codex_home_config_normalizes_deprecated_effort(tmp_path: Path) -> None:
    """A ChatGPT-app ``model_reasoning_effort = "ultra"`` becomes ``xhigh``.

    The ChatGPT desktop app writes ``ultra`` into ``~/.codex/config.toml``;
    the codex CLI maps it to the retired ``max`` wire value, which the
    OpenAI Responses API rejects (``invalid_value: 'max'``) — failing every
    codex turn on such machines. The session copy must be normalized while
    (a) the user's real config stays untouched and (b) keys inside tables
    are never rewritten.
    """
    from omnigent.inner.codex_executor import _populate_codex_home_config

    source = tmp_path / "real_codex_home"
    source.mkdir()
    original = (
        'model = "gpt-5.5"\n'
        'model_reasoning_effort = "ultra"\n'
        "[profiles.other]\n"
        'model_reasoning_effort = "ultra"\n'
    )
    (source / "config.toml").write_text(original)
    target = tmp_path / "temp_codex_home"
    target.mkdir()

    _populate_codex_home_config(target, source)

    copied = (target / "config.toml").read_text()
    # Top-level deprecated value is normalized in the session copy.
    assert 'model_reasoning_effort = "xhigh"' in copied.splitlines()[1]
    # Keys inside tables are left alone — they may target other ladders.
    assert copied.splitlines()[3] == 'model_reasoning_effort = "ultra"'
    # The user's real config is never mutated.
    assert (source / "config.toml").read_text() == original


def test_populate_codex_home_config_keeps_valid_effort(tmp_path: Path) -> None:
    """A supported top-level effort value is copied verbatim."""
    from omnigent.inner.codex_executor import _populate_codex_home_config

    source = tmp_path / "real_codex_home"
    source.mkdir()
    original = 'model = "gpt-5.5"\nmodel_reasoning_effort = "high"\n'
    (source / "config.toml").write_text(original)
    target = tmp_path / "temp_codex_home"
    target.mkdir()

    _populate_codex_home_config(target, source)

    assert (target / "config.toml").read_text() == original


def test_populate_codex_home_config_normalizes_effort_after_multiline_array(
    tmp_path: Path,
) -> None:
    """A top-level effort key AFTER a multiline array is still normalized.

    A top-level array's continuation lines can start with ``[`` (nested
    arrays), which must not be mistaken for a table header -- otherwise the
    still-top-level ``model_reasoning_effort`` past the array is skipped.
    """
    from omnigent.inner.codex_executor import _populate_codex_home_config

    source = tmp_path / "real_codex_home"
    source.mkdir()
    original = (
        "value = [\n"
        '  ["nested"],\n'
        '  ["deep"],\n'
        "]\n"
        'model_reasoning_effort = "ultra"\n'
        "[profiles.other]\n"
        'model_reasoning_effort = "ultra"\n'
    )
    (source / "config.toml").write_text(original)
    target = tmp_path / "temp_codex_home"
    target.mkdir()

    _populate_codex_home_config(target, source)

    copied = (target / "config.toml").read_text().splitlines()
    assert copied[0] == "value = ["
    assert copied[1] == '  ["nested"],'
    assert copied[4] == 'model_reasoning_effort = "xhigh"'
    assert copied[5] == "[profiles.other]"
    assert copied[6] == 'model_reasoning_effort = "ultra"'
    assert (source / "config.toml").read_text() == original


def test_populate_codex_home_config_missing_source_dir(tmp_path: Path) -> None:
    """When the source ``CODEX_HOME`` dir doesn't exist (fresh install),
    nothing is created in the target.

    Handles the case where codex has never been run locally — the
    populator must no-op cleanly rather than raising on a missing path.
    """
    from omnigent.inner.codex_executor import _populate_codex_home_config

    source = tmp_path / "nonexistent"
    target = tmp_path / "temp_codex_home"
    target.mkdir()

    _populate_codex_home_config(target, source)

    assert list(target.iterdir()) == []


def test_populate_codex_home_config_partial_files(tmp_path: Path) -> None:
    """When only some config files exist, only those are symlinked.

    API-key users may not have ``auth.json`` (subscription auth is
    opt-in via ``codex auth``); ``config.toml`` is optional too. The
    populator must skip missing files silently.
    """
    from omnigent.inner.codex_executor import _populate_codex_home_config

    source = tmp_path / "real_codex_home"
    source.mkdir()
    (source / "auth.json").write_text('{"auth_mode": "chatgpt"}')
    # config.toml intentionally absent
    target = tmp_path / "temp_codex_home"
    target.mkdir()

    _populate_codex_home_config(target, source)

    assert (target / "auth.json").is_symlink()
    assert not (target / "config.toml").exists()


def test_app_server_start_uses_real_home_for_private_inherited_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Empty inherited ``CODEX_HOME`` does not hide the real user login at startup.

    Omnigent itself can be launched from a Codex-managed environment
    where ``CODEX_HOME`` points at an isolated private home. If that
    inherited home lacks Codex auth/config files, app-server startup must
    bridge from the user's real ``~/.codex`` equivalent so Codex sessions
    do not prompt to log in despite the regular CLI already being authenticated.

    :param tmp_path: Temporary directory for isolated Codex homes.
    :param monkeypatch: Pytest fixture used to isolate ``HOME`` and
        inherited ``CODEX_HOME``.
    :returns: None.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    inherited = home / ".omnigent" / "codex-native" / "abc123" / "codex-home"
    inherited.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(inherited))
    real_codex_home = home / ".codex"
    real_codex_home.mkdir()
    (real_codex_home / "auth.json").write_text('{"auth_mode": "api_key"}')
    (real_codex_home / "config.toml").write_text('model_provider = "openai"')

    async def _t() -> None:
        """
        Drive ``_CodexAppServerSession.start`` with the subprocess boundary stubbed.

        :returns: None.
        """
        fake_proc = _FakeProcess()
        recorded_env: dict[str, str] | None = None
        target_at_spawn: Path | None = None

        async def _fake_create_subprocess_exec(*args, **kwargs):
            """
            Capture the app-server subprocess environment.

            :param args: Positional subprocess argv values.
            :param kwargs: Keyword subprocess options, including ``env``.
            :returns: Fake subprocess handle.
            """
            nonlocal recorded_env, target_at_spawn
            recorded_env = kwargs.get("env")
            assert recorded_env is not None
            target_at_spawn = Path(recorded_env["CODEX_HOME"])
            assert target_at_spawn != inherited
            assert (target_at_spawn / "auth.json").is_symlink()
            assert not (target_at_spawn / "config.toml").is_symlink()
            assert (target_at_spawn / "config.toml").is_file()
            assert (target_at_spawn / "auth.json").read_text() == '{"auth_mode": "api_key"}'
            assert (target_at_spawn / "config.toml").read_text() == 'model_provider = "openai"'
            return fake_proc

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        session = _CodexAppServerSession(
            codex_path="/bin/echo",
            cwd=str(workspace),
            env={},
            tool_executor=None,
        )
        session._request = AsyncMock(return_value={"result": {}})

        with patch(
            "omnigent.inner.codex_executor._create_subprocess_exec",
            new=_fake_create_subprocess_exec,
        ):
            await session.start()
            assert recorded_env is not None
            assert target_at_spawn is not None
            await session.close()

        assert fake_proc.terminated

    _run(_t())


def test_app_server_start_preserves_custom_home_from_inherited_private_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Nested startup preserves a parent's custom Codex home source.

    A top-level launch may bridge auth/config from an explicit custom
    ``CODEX_HOME`` into an Omnigent private home. A nested launch inherits
    only that private path, so it must infer the original custom source from
    the existing symlink targets instead of falling back to ``~/.codex``.

    :param tmp_path: Temporary directory for isolated Codex homes.
    :param monkeypatch: Pytest fixture used to isolate ``HOME`` and
        inherited ``CODEX_HOME``.
    :returns: None.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    custom_home = tmp_path / "custom-codex-home"
    custom_home.mkdir()
    (custom_home / "auth.json").write_text('{"auth_mode": "custom"}')
    (custom_home / "config.toml").write_text('model_provider = "custom"')
    default_home = home / ".codex"
    default_home.mkdir(parents=True)
    (default_home / "auth.json").write_text('{"auth_mode": "default"}')
    (default_home / "config.toml").write_text('model_provider = "default"')
    inherited = home / ".omnigent" / "codex-native" / "abc123" / "codex-home"
    inherited.mkdir(parents=True)
    (inherited / "auth.json").symlink_to(custom_home / "auth.json")
    (inherited / "config.toml").symlink_to(custom_home / "config.toml")
    monkeypatch.setenv("CODEX_HOME", str(inherited))

    async def _t() -> None:
        """
        Drive ``_CodexAppServerSession.start`` with a stubbed subprocess.

        :returns: None.
        """
        fake_proc = _FakeProcess()

        async def _fake_create_subprocess_exec(*args, **kwargs):
            """
            Assert the bridged files come from the custom home at spawn time.

            :param args: Positional subprocess argv values.
            :param kwargs: Keyword subprocess options, including ``env``.
            :returns: Fake subprocess handle.
            """
            target = Path(kwargs["env"]["CODEX_HOME"])
            assert (target / "auth.json").read_text() == '{"auth_mode": "custom"}'
            assert (target / "config.toml").read_text() == 'model_provider = "custom"'
            return fake_proc

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        session = _CodexAppServerSession(
            codex_path="/bin/echo",
            cwd=str(workspace),
            env={},
            tool_executor=None,
        )
        session._request = AsyncMock(return_value={"result": {}})

        with patch(
            "omnigent.inner.codex_executor._create_subprocess_exec",
            new=_fake_create_subprocess_exec,
        ):
            await session.start()
            await session.close()

    _run(_t())


def test_populate_codex_home_config_does_not_overwrite_existing(tmp_path: Path) -> None:
    """If a config file already exists in the target (e.g. from a
    previous partial start), it is not replaced.

    Guards against double-start races or manual overrides placed
    in the temp dir before the populator runs.
    """
    from omnigent.inner.codex_executor import _populate_codex_home_config

    source = tmp_path / "real_codex_home"
    source.mkdir()
    (source / "auth.json").write_text('{"new": true}')
    target = tmp_path / "temp_codex_home"
    target.mkdir()
    (target / "auth.json").write_text('{"old": true}')

    _populate_codex_home_config(target, source)

    assert (target / "auth.json").read_text() == '{"old": true}'
    assert not (target / "auth.json").is_symlink()


# ---------------------------------------------------------------------------
# _clean_codex_env tests
# ---------------------------------------------------------------------------


def test_clean_codex_env_excludes_openai_api_key(monkeypatch) -> None:
    """``_clean_codex_env`` must strip ``OPENAI_API_KEY`` even though
    the ``OPENAI_`` prefix is in the allowlist.

    The codex harness routes through the CLI's subscription auth
    (``auth.json``), not a developer API key. Leaking
    ``OPENAI_API_KEY`` into the subprocess would charge the user's
    developer account instead of their subscription plan.
    """
    from omnigent.inner.codex_executor import _clean_codex_env

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    monkeypatch.setenv("OPENAI_MAX_RETRIES", "3")
    monkeypatch.setenv("OPENAI_TIMEOUT", "60")

    env = _clean_codex_env()

    assert "OPENAI_API_KEY" not in env, (
        "OPENAI_API_KEY must be excluded — subscription auth, not API key"
    )
    # Other OPENAI_* vars (retry/timeout knobs) must still pass through.
    assert env.get("OPENAI_MAX_RETRIES") == "3"
    assert env.get("OPENAI_TIMEOUT") == "60"


def test_clean_codex_env_includes_databricks_bearer(monkeypatch) -> None:
    """``_clean_codex_env`` preserves CI's explicit Databricks bearer.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.inner.codex_executor import _clean_codex_env

    monkeypatch.setenv("DATABRICKS_BEARER", "ci-bearer")
    monkeypatch.setenv("DATABRICKS_TOKEN", "stale-token")

    env = _clean_codex_env()

    assert env.get("DATABRICKS_BEARER") == "ci-bearer"
    assert "DATABRICKS_TOKEN" not in env


def test_clean_codex_env_includes_omnigent_session_marker(monkeypatch) -> None:
    """The ``OMNIGENT`` session marker survives the codex env scrub.

    The marker (set once on the runner) must reach the codex CLI so the
    shell commands codex runs can detect they are inside an Omnigent
    session, like ``CLAUDE_CODE`` / ``CODEX``.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.inner.codex_executor import _clean_codex_env
    from omnigent.runner.identity import (
        OMNIGENT_SESSION_ENV_VALUE,
        OMNIGENT_SESSION_ENV_VAR,
    )

    monkeypatch.setenv(OMNIGENT_SESSION_ENV_VAR, OMNIGENT_SESSION_ENV_VALUE)

    env = _clean_codex_env()

    assert env.get(OMNIGENT_SESSION_ENV_VAR) == OMNIGENT_SESSION_ENV_VALUE


# ---------------------------------------------------------------------------
# Tests for _to_codex_input_items — input_file → inline text conversion
# ---------------------------------------------------------------------------


def test_to_codex_input_items_text_block_passes_through() -> None:
    """Plain text blocks are mapped to Codex ``{"type": "text"}`` items."""
    result = _to_codex_input_items([{"type": "input_text", "text": "hello"}])

    # One item produced, with the same text — nothing stripped or altered.
    assert result == [{"type": "text", "text": "hello"}]


def test_to_codex_input_items_image_block_passes_through() -> None:
    """Image blocks are mapped to Codex ``{"type": "image"}`` items."""
    result = _to_codex_input_items(
        [{"type": "input_image", "image_url": "https://example.com/img.png"}]
    )

    assert result == [{"type": "image", "url": "https://example.com/img.png"}]


def test_to_codex_input_items_input_file_data_uri_decoded_to_text() -> None:
    """``input_file`` with a ``data:`` URI is decoded and emitted as text.

    This is the primary new behaviour: the Codex CLI does not support
    ``input_file`` blocks, so the base64 payload must be decoded inline.
    """
    content = "# Hello\nThis is a markdown file."
    b64 = base64.b64encode(content.encode()).decode()
    block = {"type": "input_file", "file_data": f"data:text/plain;base64,{b64}"}

    result = _to_codex_input_items([block])

    # Decoded text emitted as a plain ``text`` item.
    assert result == [{"type": "text", "text": content}], (
        f"Expected decoded file content as text item, got: {result!r}"
    )


def test_to_codex_input_items_input_file_plain_string_used_as_text() -> None:
    """``input_file`` whose ``file_data`` is NOT a data URI is used as-is."""
    block = {"type": "input_file", "file_data": "plain text content"}

    result = _to_codex_input_items([block])

    # When file_data is already plain text (no "data:" prefix), use it directly.
    assert result == [{"type": "text", "text": "plain text content"}]


def test_to_codex_input_items_input_file_empty_file_data_dropped() -> None:
    """``input_file`` with empty ``file_data`` produces no output item.

    An empty block would add noise without content; dropping it is correct.
    """
    result = _to_codex_input_items([{"type": "input_file", "file_data": ""}])

    # No item emitted — empty content is silently dropped.
    assert result == [], f"Expected empty list, got: {result!r}"


def test_to_codex_input_items_input_file_malformed_base64_dropped() -> None:
    """``input_file`` with an invalid base64 payload produces no output item.

    The decode path catches all exceptions and falls back to empty string,
    which is then dropped.  The caller should not see an error.
    """
    block = {"type": "input_file", "file_data": "data:text/plain;base64,!!!not-valid-base64!!!"}

    result = _to_codex_input_items([block])

    # Malformed base64 → decode fails → text="" → dropped.
    assert result == [], f"Expected empty list, got: {result!r}"


def test_to_codex_input_items_binary_file_dropped() -> None:
    """``input_file`` with a binary (non-text) MIME type is silently dropped.

    Decoding a PDF as UTF-8 produces garbled replacement characters that waste
    tokens and provide no useful information to the model.
    """
    b64 = base64.b64encode(b"%PDF-1.4 binary content").decode()
    block = {"type": "input_file", "file_data": f"data:application/pdf;base64,{b64}"}

    result = _to_codex_input_items([block])

    # Binary MIME type → dropped, not inlined as garbage text.
    assert result == [], f"Expected binary block to be dropped, got: {result!r}"


def test_to_codex_input_items_mixed_blocks() -> None:
    """Text + image + input_file blocks are all handled in one pass."""
    file_content = "file body"
    b64 = base64.b64encode(file_content.encode()).decode()
    blocks = [
        {"type": "input_text", "text": "question"},
        {"type": "input_file", "file_data": f"data:text/plain;base64,{b64}"},
        {"type": "input_image", "image_url": "https://example.com/pic.jpg"},
    ]

    result = _to_codex_input_items(blocks)

    assert result == [
        {"type": "text", "text": "question"},
        {"type": "text", "text": "file body"},
        {"type": "image", "url": "https://example.com/pic.jpg"},
    ], f"Unexpected result: {result!r}"


@dataclass
class _FakeVersionProcess:
    """
    Minimal subprocess stub for ``codex --version`` parsing tests.

    :param stdout: Bytes the stubbed process emits on stdout, e.g.
        ``b"codex-cli 0.129.0\\n"``.
    """

    stdout: bytes

    async def communicate(self) -> tuple[bytes, bytes]:
        """
        Return the canned ``(stdout, stderr)`` pair.

        :returns: ``(self.stdout, b"")`` — the version probe ignores
            stderr.
        """
        return (self.stdout, b"")


@pytest.mark.parametrize(
    "output,expected",
    [
        (b"codex-cli 0.129.0\n", (0, 129, 0)),
        (b"codex-cli 0.136.0\n", (0, 136, 0)),
        # Pre-release suffix: only the X.Y.Z core is parsed, so an alpha
        # of a supported release compares as that release.
        (b"codex-cli 0.132.0-alpha.1\n", (0, 132, 0)),
        # No X.Y.Z token → unknown (caller treats None as "supported").
        (b"codex-cli (unknown build)\n", None),
    ],
)
async def test_codex_cli_version_parses_output(
    monkeypatch: pytest.MonkeyPatch,
    output: bytes,
    expected: tuple[int, int, int] | None,
) -> None:
    """
    ``_codex_cli_version`` parses the numeric core of ``codex --version``.

    Guards the version gate that decides whether the native policy hook
    can be registered (>= 0.129). A parsing regression here would either
    disable enforcement on a supported codex or register an un-trustable
    hook on an old one.
    """

    async def _fake_exec(*_args: Any, **_kwargs: Any) -> _FakeVersionProcess:
        return _FakeVersionProcess(stdout=output)

    monkeypatch.setattr("omnigent.inner.codex_executor._create_subprocess_exec", _fake_exec)
    assert await _codex_cli_version("/usr/local/bin/codex") == expected


async def test_codex_cli_version_returns_none_on_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A codex binary that cannot be executed yields ``None``, not a crash.

    ``None`` means "version unknown" and the caller proceeds (treats it
    as supported). Fails if an OSError from a missing/broken binary
    propagates out of the probe.
    """

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("no such binary")

    monkeypatch.setattr("omnigent.inner.codex_executor._create_subprocess_exec", _boom)
    assert await _codex_cli_version("/usr/local/bin/codex") is None


async def test_codex_cli_version_times_out_and_kills_proc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A hung ``codex --version`` is killed and reported as unknown.

    Guards session startup: without the probe timeout, a codex build that
    blocks on ``--version`` would stall ``start()`` indefinitely. The probe
    must return ``None`` (treated as "supported, proceed") and kill the
    stuck process rather than leak it.
    """

    class _HangingProcess:
        """Stub process whose ``communicate`` blocks until killed."""

        def __init__(self) -> None:
            self.killed = False

        async def communicate(self) -> tuple[bytes, bytes]:
            # Block far longer than the probe timeout so wait_for fires.
            await asyncio.sleep(60)
            return (b"", b"")

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            return -9

    proc = _HangingProcess()

    async def _fake_exec(*_args: Any, **_kwargs: Any) -> _HangingProcess:
        return proc

    # Shrink the probe budget so the test does not actually wait the full 5s.
    monkeypatch.setattr("omnigent.inner.codex_executor._CODEX_VERSION_PROBE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr("omnigent.inner.codex_executor._create_subprocess_exec", _fake_exec)

    assert await _codex_cli_version("/usr/local/bin/codex") is None
    # The stuck process was killed, not leaked.
    assert proc.killed is True


# ── model_provider_override (cli-config / subscription pinning) ─────────────
# Function-based (the project standard); the TestCase class above predates it.


def test_model_provider_override_appends_pin() -> None:
    """The override becomes exactly one ``-c model_provider=...`` fragment.

    json.dumps quoting yields a valid TOML basic string. Failure means an
    adopted cli-config provider (or the subscription's openai pin) never
    reaches the codex CLI, so the bridged config.toml's default provider
    silently routes the session instead.
    """
    with patch("omnigent.inner.codex_executor._find_codex_cli", return_value="/usr/bin/codex"):
        executor = CodexExecutor(model_provider_override="Databricks")
    assert executor._codex_config_overrides == ['model_provider="Databricks"']


def test_model_provider_override_with_gateway_raises() -> None:
    """gateway=True and model_provider_override are mutually exclusive.

    Both write ``model_provider`` into the -c overrides; last-one-wins
    would silently drop the gateway transport. Failure (no exception)
    means a misconfigured AP producer ships an ambiguous launch.
    """
    with (
        patch("omnigent.inner.codex_executor._find_codex_cli", return_value="/usr/bin/codex"),
        pytest.raises(OSError, match="mutually exclusive"),
    ):
        CodexExecutor(
            gateway=True,
            gateway_host="https://gw.example.com",
            base_url_override="https://gw.example.com/codex/v1",
            gateway_auth_command="printf %s tok",
            model="some-model",
            model_provider_override="Databricks",
        )


def _mk_codex_skill(skills_dir: Path, name: str) -> None:
    """Create a ``<skills_dir>/<name>/SKILL.md`` skill directory."""
    d = skills_dir / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nbody\n")


def test_select_codex_skill_dirs_all_first_source_wins(tmp_path: Path) -> None:
    from omnigent.inner.codex_executor import select_codex_skill_dirs

    a, b = tmp_path / "a", tmp_path / "b"
    _mk_codex_skill(a, "shared")
    _mk_codex_skill(b, "shared")
    _mk_codex_skill(b, "only-b")
    out = select_codex_skill_dirs("all", [a, b])
    assert out["shared"] == a / "shared"
    assert out["only-b"] == b / "only-b"


def test_select_codex_skill_dirs_none_and_list(tmp_path: Path) -> None:
    from omnigent.inner.codex_executor import select_codex_skill_dirs

    a = tmp_path / "a"
    _mk_codex_skill(a, "x")
    _mk_codex_skill(a, "y")
    assert select_codex_skill_dirs("none", [a]) == {}
    assert set(select_codex_skill_dirs(["x"], [a])) == {"x"}


def test_codex_skill_sources_order_bundle_then_host(tmp_path: Path) -> None:
    """codex_skill_sources lists <bundle>/skills before <home>/.codex/skills."""
    from omnigent.inner.codex_executor import codex_skill_sources

    bundle = tmp_path / "bundle"
    (bundle / "skills").mkdir(parents=True)
    home = tmp_path / "home"
    (home / ".codex" / "skills").mkdir(parents=True)
    assert codex_skill_sources(bundle, home) == [
        bundle / "skills",
        home / ".codex" / "skills",
    ]


def test_codex_skill_sources_omits_absent_dirs(tmp_path: Path) -> None:
    """Only existing dirs are returned (bundle absent → host only)."""
    from omnigent.inner.codex_executor import codex_skill_sources

    home = tmp_path / "home"
    (home / ".codex" / "skills").mkdir(parents=True)
    assert codex_skill_sources(None, home) == [home / ".codex" / "skills"]
    assert codex_skill_sources(tmp_path / "no-bundle", home) == [home / ".codex" / "skills"]


def test_clean_codex_env_honors_extra_allow(monkeypatch):
    from omnigent.inner.codex_executor import _clean_codex_env

    monkeypatch.setenv("CRAWL4AI_API_TOKEN", "secret-tok")
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "ch-key")
    # undeclared → stripped by the hardcoded allowlist
    assert "CRAWL4AI_API_TOKEN" not in _clean_codex_env()
    # declared via env_passthrough → admitted
    env = _clean_codex_env(["CRAWL4AI_API_TOKEN", "COMPANIES_HOUSE_API_KEY"])
    assert env["CRAWL4AI_API_TOKEN"] == "secret-tok"
    assert env["COMPANIES_HOUSE_API_KEY"] == "ch-key"


def test_clean_codex_env_deny_wins_over_extra_allow(monkeypatch):
    from omnigent.inner.codex_executor import _clean_codex_env

    monkeypatch.setenv("OPENAI_API_KEY", "sk-stripped")
    # the deny rule (subscription auth) wins even if a caller declares it
    assert "OPENAI_API_KEY" not in _clean_codex_env(["OPENAI_API_KEY"])


def test_declared_passthrough_reads_sandbox_env_passthrough():
    from omnigent.inner.codex_executor import _declared_passthrough
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    # env_passthrough lives on os_env.sandbox, not os_env directly
    spec = OSEnvSpec(
        sandbox=OSEnvSandboxSpec(
            type="none", env_passthrough=["CRAWL4AI_API_TOKEN", "COMPANIES_HOUSE_API_KEY"]
        )
    )
    assert _declared_passthrough(spec) == ("CRAWL4AI_API_TOKEN", "COMPANIES_HOUSE_API_KEY")
    # guards: None os_env / None sandbox / unset list all yield ()
    assert _declared_passthrough(None) == ()
    assert _declared_passthrough(OSEnvSpec(sandbox=None)) == ()
    assert _declared_passthrough(OSEnvSpec(sandbox=OSEnvSandboxSpec(type="none"))) == ()


def test_find_codex_cli_delegates_to_shared_resolver(monkeypatch):
    """``_find_codex_cli`` resolves codex via the shared resolver with the
    OMNIGENT_CODEX_PATH override. (The resolver's own PATH/override/fallback
    behavior is covered in tests/inner/test_proc_and_platform.py.)"""
    from omnigent.inner import codex_executor as ce

    captured = {}

    def fake_resolve(name, *, env_var=None):
        captured["name"] = name
        captured["env_var"] = env_var
        return "/opt/homebrew/bin/codex"

    monkeypatch.setattr(ce, "resolve_cli_binary", fake_resolve)
    assert ce._find_codex_cli() == "/opt/homebrew/bin/codex"
    assert captured == {"name": "codex", "env_var": "OMNIGENT_CODEX_PATH"}


class TestCodexAppServerSessionReadOnlyCwd(unittest.TestCase):
    """Regression tests for .codex-tmp fallback on read-only cwd."""

    def _run_start_and_capture_mkdtemp_dir(self, cwd: str) -> str:
        """Run ``_CodexAppServerSession.start()`` with *cwd* and return
        the ``dir=`` keyword passed to ``tempfile.mkdtemp`` for the
        codex-home directory.

        ``start()`` is stopped just after ``mkdtemp`` by forcing a
        ``RuntimeError`` from the subprocess launch; the session's
        ``close()`` cleans up the temp dir before we can inspect it,
        so we capture the argument instead.
        """
        import tempfile as _tempfile

        mkdtemp_dirs: list[str] = []
        original_mkdtemp = _tempfile.mkdtemp

        def _capture(**kwargs):
            mkdtemp_dirs.append(kwargs.get("dir", ""))
            return original_mkdtemp(**kwargs)

        async def _t():
            session = _CodexAppServerSession(
                codex_path="/bin/echo",
                cwd=cwd,
                env={},
                tool_executor=None,
            )
            with (
                patch("omnigent.inner.codex_executor.populate_codex_skills_from_bundle"),
                patch("omnigent.inner.codex_executor._populate_codex_home_config"),
                patch(
                    "omnigent.inner.codex_executor._codex_home_config_source_from_env",
                    return_value=None,
                ),
                patch(
                    "omnigent.inner.codex_executor._create_subprocess_exec",
                    new_callable=AsyncMock,
                    side_effect=RuntimeError("stop"),
                ),
                patch("tempfile.mkdtemp", side_effect=_capture),
            ):
                with contextlib.suppress(RuntimeError):
                    await session.start()

            self.assertTrue(mkdtemp_dirs, "mkdtemp was never called")
            return mkdtemp_dirs[0]

        return _run(_t())

    def test_start_falls_back_to_tempdir_when_cwd_is_readonly(self):
        """When cwd is ``/`` (read-only on macOS SSV), the codex home
        must be placed under the system temp directory, not under
        ``/.codex-tmp``.
        """
        import tempfile as _tempfile

        dir_used = self._run_start_and_capture_mkdtemp_dir("/")
        self.assertEqual(dir_used, _tempfile.gettempdir())

    def test_start_uses_cwd_when_writable(self):
        """When cwd is writable, .codex-tmp is placed there as before."""
        import tempfile as _tempfile

        with _tempfile.TemporaryDirectory() as writable_dir:
            dir_used = self._run_start_and_capture_mkdtemp_dir(writable_dir)
            expected = str(Path(writable_dir) / ".codex-tmp")
            self.assertEqual(dir_used, expected)
