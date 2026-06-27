"""Tests for ClaudeSDKExecutor."""

import asyncio
import base64
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omnigent.inner.claude_sdk_executor import _to_anthropic_content_blocks
from omnigent.inner.executor import (
    ExecutorError,
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
        loop.close()


# ---------------------------------------------------------------------------
# Tests: Prompt extraction
# ---------------------------------------------------------------------------


class TestPromptExtraction(unittest.TestCase):
    def _make_executor(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        return ClaudeSDKExecutor()

    def test_resumed_session_uses_last_user_message(self):
        executor = self._make_executor()
        messages = [
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Second question"},
        ]
        self.assertEqual(
            executor._build_prompt(messages, resume_session=True),
            "Second question",
        )

    def test_empty_messages(self):
        executor = self._make_executor()
        self.assertEqual(executor._build_prompt([], resume_session=False), "")

    def test_no_user_messages(self):
        executor = self._make_executor()
        messages = [{"role": "assistant", "content": "Hello"}]
        self.assertEqual(executor._build_prompt(messages, resume_session=False), "")

    def test_dict_content_converted(self):
        executor = self._make_executor()
        messages = [{"role": "user", "content": {"key": "value"}}]
        self.assertIn("key", executor._build_prompt(messages, resume_session=False))

    def test_fresh_session_with_history_serializes_context(self):
        executor = self._make_executor()
        messages = [
            {"role": "user", "content": "The secret codeword is ZEBRA-99."},
            {"role": "assistant", "content": "Understood."},
            {"role": "user", "content": "Summarize our conversation."},
        ]
        prompt = executor._build_prompt(messages, resume_session=False)
        self.assertIn("Conversation so far:", prompt)
        self.assertIn("ZEBRA-99", prompt)
        self.assertIn("Summarize our conversation.", prompt)


# ---------------------------------------------------------------------------
# Tests: Constructor and properties
# ---------------------------------------------------------------------------


class TestConstructor(unittest.TestCase):
    def test_default_values(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
        from omnigent.spec.types import RetryPolicy

        executor = ClaudeSDKExecutor()
        self.assertFalse(executor._os_env)
        self.assertIsNone(executor._os_env_spec)
        self.assertIsNone(executor._cwd)
        self.assertIsNone(executor._model_override)
        self.assertEqual(executor._permission_mode, "auto")
        self.assertIsNone(executor._tool_executor)
        self.assertEqual(executor._clients, {})
        self.assertEqual(executor._crashed_sessions, {})
        self.assertFalse(executor._gateway)
        # _extra_env carries the default RetryPolicy's CLI env vars
        # (ANTHROPIC_MAX_RETRIES + ANTHROPIC_REQUEST_TIMEOUT_SECONDS).
        self.assertEqual(executor._extra_env, RetryPolicy().claude_cli.env())

    def test_os_env_spec_with_no_sandbox_keeps_native_tools_enabled(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
        from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

        executor = ClaudeSDKExecutor(
            os_env=OSEnvSpec(
                type="caller_process",
                sandbox=OSEnvSandboxSpec(type="none"),
            )
        )
        self.assertTrue(executor._os_env)
        self.assertIsNotNone(executor._os_env_spec)

    def test_os_env_spec_wraps_cli_and_enables_native_tools(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor, PreparedClaudeCli
        from omnigent.inner.datamodel import OSEnvSpec

        spec = OSEnvSpec(type="caller_process", cwd="/tmp/work")
        with (
            patch(
                "omnigent.inner.claude_sdk_executor._find_system_claude",
                return_value="/usr/bin/claude",
            ),
            patch(
                "omnigent.inner.claude_sdk_executor.prepare_claude_cli_path",
                return_value=PreparedClaudeCli(
                    cli_path="/tmp/omnigent-claude-wrapper",
                    enable_native_tools=True,
                ),
            ),
        ):
            executor = ClaudeSDKExecutor(os_env=spec)

        self.assertTrue(executor._os_env)
        self.assertEqual(executor._os_env_spec, spec)
        self.assertEqual(executor._cli_path, "/tmp/omnigent-claude-wrapper")
        self.assertEqual(executor._cwd, "/tmp/work")

    def test_prepare_claude_cli_path_adds_internal_roots_to_read_allowlist(self):
        from omnigent.inner.claude_sdk_executor import prepare_claude_cli_path
        from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
        from omnigent.inner.sandbox import SandboxPolicy

        captured: dict[str, SandboxPolicy] = {}

        def _capture_launcher(_path: str, sandbox: SandboxPolicy) -> str:
            captured["sandbox"] = sandbox
            return "/tmp/launcher"

        spec = OSEnvSpec(
            type="caller_process",
            cwd="/tmp/work",
            sandbox=OSEnvSandboxSpec(
                type="linux_bwrap",
                read_paths=["."],
                write_paths=["."],
                allow_network=True,
            ),
        )

        with (
            patch(
                "omnigent.inner.claude_sdk_executor.resolve_sandbox",
                return_value=SandboxPolicy(
                    backend_type="linux_bwrap",
                    active=True,
                    read_roots=[Path("/tmp/work")],
                    write_roots=[Path("/tmp/work")],
                    write_files=[Path("/dev/null")],
                    allow_network=True,
                ),
            ),
            patch(
                "omnigent.inner.claude_sdk_executor._claude_internal_write_roots",
                return_value=[Path("/home/test/.claude/sessions")],
            ),
            patch(
                "omnigent.inner.claude_sdk_executor._claude_internal_write_files",
                return_value=[],
            ),
            patch(
                "omnigent.inner.claude_sdk_executor.create_exec_launcher",
                side_effect=_capture_launcher,
            ),
        ):
            prepared = prepare_claude_cli_path("/usr/bin/claude", spec)

        self.assertEqual(prepared.cli_path, "/tmp/launcher")
        self.assertTrue(prepared.enable_native_tools)
        # Sandbox helpers call ``.resolve(strict=False)``, which on
        # macOS rewrites ``/home`` → ``/System/Volumes/Data/home``.
        # Compare against the resolved form so the assertion is stable
        # across platforms.
        expected = Path("/home/test/.claude/sessions").resolve(strict=False)
        self.assertIn(expected, captured["sandbox"].read_roots)

    def test_default_process_sandbox_wraps_cli_without_enabling_native_tools(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        with (
            patch(
                "omnigent.inner.claude_sdk_executor._find_system_claude",
                return_value="/usr/bin/claude",
            ),
            patch(
                "omnigent.inner.claude_sdk_executor.prepare_tight_cli_process_path",
                return_value="/tmp/omnigent-claude-tight-wrapper",
            ),
        ):
            executor = ClaudeSDKExecutor()

        self.assertFalse(executor._os_env)
        self.assertIsNone(executor._os_env_spec)
        self.assertEqual(
            executor._cli_path,
            "/tmp/omnigent-claude-tight-wrapper",
        )

    def test_os_env_spec_without_supported_native_sandbox_disables_native_tools(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor, PreparedClaudeCli
        from omnigent.inner.datamodel import OSEnvSpec

        spec = OSEnvSpec(type="caller_process", cwd="/tmp/work")
        with (
            patch(
                "omnigent.inner.claude_sdk_executor._find_system_claude",
                return_value="/usr/bin/claude",
            ),
            patch(
                "omnigent.inner.claude_sdk_executor.prepare_claude_cli_path",
                return_value=PreparedClaudeCli(
                    cli_path="/usr/bin/claude",
                    enable_native_tools=False,
                ),
            ),
        ):
            executor = ClaudeSDKExecutor(os_env=spec)

        self.assertFalse(executor._os_env)
        self.assertEqual(executor._os_env_spec, spec)
        self.assertEqual(executor._cli_path, "/usr/bin/claude")

    def test_model_override(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        executor = ClaudeSDKExecutor(model="claude-haiku-4-5-20251001")
        self.assertEqual(executor._model_override, "claude-haiku-4-5-20251001")

    def test_supports_streaming(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        self.assertTrue(ClaudeSDKExecutor().supports_streaming())

    def test_supports_tool_calling(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        self.assertTrue(ClaudeSDKExecutor().supports_tool_calling())

    def test_databricks_flag_with_profile(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
        from omnigent.inner.databricks_executor import DatabricksCredentials

        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "omnigent.inner.databricks_executor._read_databrickscfg",
                return_value=DatabricksCredentials(
                    host="https://example.cloud.databricks.com",
                    token="dapi_test_token",
                ),
            ),
        ):
            executor = ClaudeSDKExecutor(gateway=True)
            self.assertTrue(executor._gateway)
            self.assertEqual(
                executor._extra_env["ANTHROPIC_BASE_URL"],
                "https://example.cloud.databricks.com/ai-gateway/anthropic",
            )
            self.assertEqual(executor._extra_env["CLAUDE_CODE_API_KEY_HELPER_TTL_MS"], "900000")
            self.assertIn(
                'databricks auth token --host "https://example.cloud.databricks.com"',
                executor._extra_env["OMNIGENT_CLAUDE_API_KEY_HELPER"],
            )
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", executor._extra_env)

    def test_databricks_explicit_profile_selects_by_profile(self):
        """An explicit ``databricks_profile`` makes the token helper select
        the bearer by ``--profile`` (unambiguous), not ``--host``.

        Two ``~/.databrickscfg`` profiles can share one host, which makes
        ``databricks auth token --host`` fail ("Use --profile to specify
        which profile") → empty token → a silent ``status=401``. Selecting
        by ``--profile`` avoids that.
        """
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
        from omnigent.inner.databricks_executor import DatabricksCredentials

        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "omnigent.inner.databricks_executor._read_databrickscfg",
                return_value=DatabricksCredentials(
                    host="https://example.cloud.databricks.com",
                    token="dapi_test_token",
                ),
            ),
        ):
            executor = ClaudeSDKExecutor(gateway=True, databricks_profile="oss")
        helper = executor._extra_env["OMNIGENT_CLAUDE_API_KEY_HELPER"]
        # Proves the selector is --profile, not --host. A regression to --host
        # makes a two-profiles-one-host workspace yield an empty token → 401.
        self.assertIn('databricks auth token --profile "oss"', helper)
        self.assertNotIn("--host", helper)
        # `--force-refresh` only exists in Databricks CLI >= v0.296.0, so it
        # must be applied via a `--help` capability probe ($force), never
        # passed unconditionally — an older CLI rejects the unknown flag and
        # yields an empty token → silent 401.
        self.assertIn("databricks auth token --help", helper)
        self.assertIn("force=--force-refresh", helper)
        self.assertNotIn('oss" --force-refresh', helper)

    def test_databricks_flag_no_creds_raises(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("omnigent.inner.claude_sdk_executor._resolve_gateway_env", return_value={}),
        ):
            with self.assertRaises(EnvironmentError):
                ClaudeSDKExecutor(gateway=True)

    def test_databricks_flag_with_host_override(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("omnigent.inner.databricks_executor._read_databrickscfg") as read_cfg,
        ):
            executor = ClaudeSDKExecutor(
                gateway=True,
                databricks_profile="missing-profile",
                gateway_host="https://example.databricks.com/",
                base_url_override="https://example.databricks.com/ai-gateway/anthropic",
                gateway_auth_command="printf token",
            )

        read_cfg.assert_not_called()
        self.assertEqual(
            executor._extra_env["ANTHROPIC_BASE_URL"],
            "https://example.databricks.com/ai-gateway/anthropic",
        )
        self.assertEqual(
            executor._extra_env["OMNIGENT_CLAUDE_API_KEY_HELPER"],
            "printf token",
        )

    def test_databricks_flag_with_host_override_requires_base_url(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        with (
            patch.dict("os.environ", {}, clear=True),
            self.assertRaisesRegex(OSError, "GATEWAY_BASE_URL"),
        ):
            ClaudeSDKExecutor(
                gateway=True,
                gateway_host="https://example.databricks.com/",
                gateway_auth_command="printf token",
            )

    def test_databricks_flag_with_host_override_requires_auth_command(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        with (
            patch.dict("os.environ", {}, clear=True),
            self.assertRaisesRegex(OSError, "GATEWAY_AUTH_COMMAND"),
        ):
            ClaudeSDKExecutor(
                gateway=True,
                gateway_host="https://example.databricks.com/",
                base_url_override="https://example.databricks.com/ai-gateway/anthropic",
            )

    def test_databricks_false_no_extra_env(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
        from omnigent.spec.types import RetryPolicy

        executor = ClaudeSDKExecutor(gateway=False)
        # gateway=False → no Databricks env, but RetryPolicy CLI env
        # is always merged in. Verify the only entries are the retry env.
        self.assertEqual(executor._extra_env, RetryPolicy().claude_cli.env())

    def test_databricks_profile_default_model_used_when_unset(self):
        """gateway=True (profile-derived) + no model → Databricks default.

        On the Databricks-profile gateway path (transport derived from
        ~/.databrickscfg, no gateway base URL supplied directly), a missing
        model falls back to the Databricks default. The neutral
        generic-provider gateway path never does this (see
        ``test_neutral_gateway_no_model_does_not_inject_databricks_default``).
        """
        from omnigent.inner.claude_sdk_executor import (
            _DATABRICKS_CLAUDE_DEFAULT_MODEL,
            ClaudeSDKExecutor,
        )
        from omnigent.inner.databricks_executor import DatabricksCredentials

        async def _t():
            with patch(
                "omnigent.inner.databricks_executor._read_databrickscfg",
                return_value=DatabricksCredentials(
                    host="https://example.cloud.databricks.com",
                    token="dapi_test_token",
                ),
            ):
                executor = ClaudeSDKExecutor(gateway=True)

            captured: dict[str, str | None] = {}

            async def fake_get_or_create_client(sdk, *, session_key, options, model):
                captured["model"] = model
                raise RuntimeError("stop after model resolution")

            with patch.object(
                executor,
                "_get_or_create_client",
                side_effect=fake_get_or_create_client,
            ):
                with self.assertRaises(RuntimeError):
                    async for _ in executor.run_turn([{"role": "user", "content": "hi"}], [], ""):
                        pass

            self.assertEqual(captured["model"], _DATABRICKS_CLAUDE_DEFAULT_MODEL)

        _run(_t())

    def test_neutral_gateway_no_model_does_not_inject_databricks_default(self):
        """Neutral gateway (base URL supplied directly) + no model → ``None``.

        The neutral generic-provider gateway transport never falls back to a
        ``databricks-*`` model: the Omnigent producer resolves a concrete model
        before spawning, so the executor passes ``None`` through to the SDK.
        """
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        async def _t():
            executor = ClaudeSDKExecutor(
                gateway=True,
                gateway_host="https://gateway.example.com",
                base_url_override="https://gateway.example.com/v1",
                gateway_auth_command="printf token",
            )

            captured: dict[str, str | None] = {}

            async def fake_get_or_create_client(sdk, *, session_key, options, model):
                captured["model"] = model
                raise RuntimeError("stop after model resolution")

            with patch.object(
                executor,
                "_get_or_create_client",
                side_effect=fake_get_or_create_client,
            ):
                with self.assertRaises(RuntimeError):
                    async for _ in executor.run_turn([{"role": "user", "content": "hi"}], [], ""):
                        pass

            self.assertIsNone(captured["model"])

        _run(_t())

    def test_gateway_model_passes_through(self):
        """Explicit model on the gateway path passes through unchanged."""
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
        from omnigent.inner.databricks_executor import DatabricksCredentials

        async def _t():
            with patch(
                "omnigent.inner.databricks_executor._read_databrickscfg",
                return_value=DatabricksCredentials(
                    host="https://example.cloud.databricks.com",
                    token="dapi_test_token",
                ),
            ):
                executor = ClaudeSDKExecutor(gateway=True, model="databricks-claude-sonnet-4-6")

            captured: dict[str, str | None] = {}

            async def fake_get_or_create_client(sdk, *, session_key, options, model):
                captured["model"] = model
                raise RuntimeError("stop after model resolution")

            with patch.object(
                executor,
                "_get_or_create_client",
                side_effect=fake_get_or_create_client,
            ):
                with self.assertRaises(RuntimeError):
                    async for _ in executor.run_turn([{"role": "user", "content": "hi"}], [], ""):
                        pass

            self.assertEqual(captured["model"], "databricks-claude-sonnet-4-6")

        _run(_t())

    def test_no_databricks_default_when_databricks_off(self):
        """gateway=False keeps prior behavior: None falls through to the SDK."""
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        async def _t():
            executor = ClaudeSDKExecutor(gateway=False)
            captured: dict[str, str | None] = {}

            async def fake_get_or_create_client(sdk, *, session_key, options, model):
                captured["model"] = model
                raise RuntimeError("stop after model resolution")

            with patch.object(
                executor,
                "_get_or_create_client",
                side_effect=fake_get_or_create_client,
            ):
                with self.assertRaises(RuntimeError):
                    async for _ in executor.run_turn([{"role": "user", "content": "hi"}], [], ""):
                        pass

            self.assertIsNone(captured["model"])

        _run(_t())

    def test_databricks_opus_pins_thinking_to_adaptive(self):
        """gateway=True + opus sets ``thinking={"type": "adaptive", "display": "summarized"}``."""
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
        from omnigent.inner.databricks_executor import DatabricksCredentials

        async def _t():
            with patch(
                "omnigent.inner.databricks_executor._read_databrickscfg",
                return_value=DatabricksCredentials(
                    host="https://example.cloud.databricks.com",
                    token="dapi_test_token",
                ),
            ):
                executor = ClaudeSDKExecutor(gateway=True, model="databricks-claude-opus-4-7")

            captured: dict[str, object] = {}

            async def fake_get_or_create_client(sdk, *, session_key, options, model):
                captured["thinking"] = getattr(options, "thinking", None)
                raise RuntimeError("stop after options built")

            with patch.object(
                executor,
                "_get_or_create_client",
                side_effect=fake_get_or_create_client,
            ):
                with self.assertRaises(RuntimeError):
                    async for _ in executor.run_turn([{"role": "user", "content": "hi"}], [], ""):
                        pass

            self.assertEqual(captured["thinking"], {"type": "adaptive", "display": "summarized"})

        _run(_t())

    def test_databricks_fable_pins_thinking_to_adaptive(self):
        """gateway=True + fable sets ``thinking={"type": "adaptive", "display": "summarized"}``.

        Fable (claude-fable-5) shares Opus 4.7/4.8's adaptive-only thinking
        surface, so the Databricks gateway rejects the CLI's default
        thinking=enabled for it too. If this stays unset, a fable session
        through the gateway 400s on the first request.
        """
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
        from omnigent.inner.databricks_executor import DatabricksCredentials

        async def _t():
            with patch(
                "omnigent.inner.databricks_executor._read_databrickscfg",
                return_value=DatabricksCredentials(
                    host="https://example.databricks.com",
                    token="dapi_test_token",
                ),
            ):
                executor = ClaudeSDKExecutor(gateway=True, model="databricks-claude-fable-5")

            captured: dict[str, object] = {}

            async def fake_get_or_create_client(sdk, *, session_key, options, model):
                captured["thinking"] = getattr(options, "thinking", None)
                raise RuntimeError("stop after options built")

            with patch.object(
                executor,
                "_get_or_create_client",
                side_effect=fake_get_or_create_client,
            ):
                with self.assertRaises(RuntimeError):
                    async for _ in executor.run_turn([{"role": "user", "content": "hi"}], [], ""):
                        pass

            self.assertEqual(captured["thinking"], {"type": "adaptive", "display": "summarized"})

        _run(_t())

    def test_databricks_sonnet_leaves_thinking_unset(self):
        """gateway=True + non-adaptive-tier model preserves CLI default thinking."""
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
        from omnigent.inner.databricks_executor import DatabricksCredentials

        async def _t():
            with patch(
                "omnigent.inner.databricks_executor._read_databrickscfg",
                return_value=DatabricksCredentials(
                    host="https://example.cloud.databricks.com",
                    token="dapi_test_token",
                ),
            ):
                executor = ClaudeSDKExecutor(gateway=True, model="databricks-claude-sonnet-4-6")

            captured: dict[str, object] = {}

            async def fake_get_or_create_client(sdk, *, session_key, options, model):
                captured["thinking"] = getattr(options, "thinking", None)
                raise RuntimeError("stop after options built")

            with patch.object(
                executor,
                "_get_or_create_client",
                side_effect=fake_get_or_create_client,
            ):
                with self.assertRaises(RuntimeError):
                    async for _ in executor.run_turn([{"role": "user", "content": "hi"}], [], ""):
                        pass

            self.assertIsNone(captured["thinking"])

        _run(_t())

    def test_no_databricks_leaves_thinking_unset(self):
        """gateway=False does not touch ``thinking``; preserves CLI default."""
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        async def _t():
            executor = ClaudeSDKExecutor(gateway=False, model="claude-opus-4-7")
            captured: dict[str, object] = {}

            async def fake_get_or_create_client(sdk, *, session_key, options, model):
                captured["thinking"] = getattr(options, "thinking", None)
                raise RuntimeError("stop after options built")

            with patch.object(
                executor,
                "_get_or_create_client",
                side_effect=fake_get_or_create_client,
            ):
                with self.assertRaises(RuntimeError):
                    async for _ in executor.run_turn([{"role": "user", "content": "hi"}], [], ""):
                        pass

            self.assertIsNone(captured["thinking"])

        _run(_t())

    def test_force_close_client_uses_process_tree_termination(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        class _Transport:
            def __init__(self):
                self._process = SimpleNamespace(returncode=None, pid=12345, wait=AsyncMock())
                self._stdout_stream = object()
                self._stdin_stream = object()
                self._stderr_stream = object()
                self._stderr_task_group = None
                self._ready = True

        client = SimpleNamespace(_query=None, _transport=_Transport())

        async def _t():
            with patch(
                "omnigent.inner.claude_sdk_executor._terminate_process_tree"
            ) as terminate_tree:
                await ClaudeSDKExecutor._force_close_client(client)
            terminate_tree.assert_called_once()

        _run(_t())

    def test_force_close_client_handles_sdk_without_stderr_task_group(self):
        # Regression: claude-agent-sdk >=0.2.x renamed the stderr reader from an
        # anyio task group (`_stderr_task_group`) to a single `_stderr_task`
        # TaskHandle. A transport shaped like the current SDK (no
        # `_stderr_task_group` attribute at all) must not raise AttributeError
        # out of `_force_close_client` — that exception escaped the runner's
        # lifespan shutdown and crashed it on every session stop.
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        stderr_task = SimpleNamespace(cancel=Mock())

        class _Transport:
            def __init__(self):
                self._process = SimpleNamespace(returncode=None, pid=12345, wait=AsyncMock())
                self._stdout_stream = object()
                self._stdin_stream = object()
                self._stderr_stream = object()
                self._stderr_task = stderr_task  # current SDK shape
                self._ready = True

        transport = _Transport()
        client = SimpleNamespace(_query=None, _transport=transport)

        async def _t():
            with patch(
                "omnigent.inner.claude_sdk_executor._terminate_process_tree"
            ) as terminate_tree:
                await ClaudeSDKExecutor._force_close_client(client)
            terminate_tree.assert_called_once()

        _run(_t())

        # New-shape stderr task was cancelled, the missing legacy attribute was
        # never created, and the handle was cleared.
        stderr_task.cancel.assert_called_once()
        self.assertFalse(hasattr(transport, "_stderr_task_group"))
        self.assertIsNone(transport._stderr_task)

    def test_claude_internal_write_files_omits_missing_config(self):
        from omnigent.inner.claude_sdk_executor import _claude_internal_write_files

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            config_path = home / ".claude.json"
            self.assertFalse(config_path.exists())
            with patch("omnigent.inner.claude_sdk_executor.pathlib.Path.home", return_value=home):
                paths = _claude_internal_write_files()

            self.assertEqual(paths, [])
            self.assertFalse(config_path.exists())

    def test_claude_internal_write_files_includes_existing_config(self):
        from omnigent.inner.claude_sdk_executor import _claude_internal_write_files

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            config_path = home / ".claude.json"
            config_path.write_text("{}\n", encoding="utf-8")
            with patch("omnigent.inner.claude_sdk_executor.pathlib.Path.home", return_value=home):
                paths = _claude_internal_write_files()

            self.assertEqual(paths, [config_path])


# ---------------------------------------------------------------------------
# Tests: MCP tool building
# ---------------------------------------------------------------------------


class TestBuildMcpTools(unittest.TestCase):
    def test_builds_tools_from_schemas(self):
        from omnigent.inner.claude_sdk_executor import _build_mcp_tools

        async def mock_executor(name, args):
            return {"result": "ok"}

        schemas = [
            {
                "name": "calc",
                "description": "Calculate",
                "parameters": {
                    "type": "object",
                    "properties": {"expr": {"type": "string"}},
                },
            },
        ]
        tools = _build_mcp_tools(schemas, mock_executor)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].name, "calc")

    def test_empty_schemas(self):
        from omnigent.inner.claude_sdk_executor import _build_mcp_tools

        self.assertEqual(_build_mcp_tools([], None), [])

    def test_handler_calls_executor(self):
        from omnigent.inner.claude_sdk_executor import _build_mcp_tools

        calls = []

        async def mock_executor(name, args):
            calls.append((name, args))
            return {"result": 42}

        schemas = [
            {
                "name": "add",
                "description": "Add",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            }
        ]
        tools = _build_mcp_tools(schemas, mock_executor)
        result = _run(tools[0].handler({"a": 1}))
        self.assertEqual(calls, [("add", {"a": 1})])
        self.assertIn("content", result)
        parsed = json.loads(result["content"][0]["text"])
        self.assertEqual(parsed["result"], 42)
        self.assertNotIn("isError", result)

    def test_handler_marks_blocked_result_as_error(self):
        from omnigent.inner.claude_sdk_executor import _build_mcp_tools

        async def mock_executor(name, args):
            return {"blocked": True, "reason": "Exceeded max tool calls"}

        schemas = [
            {
                "name": "add",
                "description": "Add",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            }
        ]
        tools = _build_mcp_tools(schemas, mock_executor)
        result = _run(tools[0].handler({"a": 1}))
        self.assertTrue(result["isError"])
        parsed = json.loads(result["content"][0]["text"])
        self.assertTrue(parsed["blocked"])

    def test_handler_marks_error_result_as_error(self):
        from omnigent.inner.claude_sdk_executor import _build_mcp_tools

        async def mock_executor(name, args):
            return {"error": "boom"}

        schemas = [
            {
                "name": "add",
                "description": "Add",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            }
        ]
        tools = _build_mcp_tools(schemas, mock_executor)
        result = _run(tools[0].handler({"a": 1}))
        self.assertTrue(result["isError"])
        parsed = json.loads(result["content"][0]["text"])
        self.assertEqual(parsed["error"], "boom")

    def test_handler_no_executor(self):
        from omnigent.inner.claude_sdk_executor import _build_mcp_tools

        schemas = [
            {
                "name": "x",
                "description": "x",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            }
        ]
        tools = _build_mcp_tools(schemas, None)
        result = _run(tools[0].handler({}))
        parsed = json.loads(result["content"][0]["text"])
        self.assertIn("error", parsed)


# ---------------------------------------------------------------------------
# Tests: Databricks env resolution
# ---------------------------------------------------------------------------


class TestResolveGatewayEnv(unittest.TestCase):
    def test_from_profile(self):
        from omnigent.inner.claude_sdk_executor import _resolve_gateway_env
        from omnigent.inner.databricks_executor import DatabricksCredentials

        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "omnigent.inner.databricks_executor._read_databrickscfg",
                return_value=DatabricksCredentials(
                    host="https://example.databricks.com",
                    token="dapi_abc123",
                ),
            ),
        ):
            env = _resolve_gateway_env()
            self.assertEqual(
                env["ANTHROPIC_BASE_URL"],
                "https://example.databricks.com/ai-gateway/anthropic",
            )
            self.assertEqual(env["CLAUDE_CODE_API_KEY_HELPER_TTL_MS"], "900000")
            self.assertIn(
                'databricks auth token --host "https://example.databricks.com"',
                env["OMNIGENT_CLAUDE_API_KEY_HELPER"],
            )
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)

    def test_strips_trailing_slash(self):
        from omnigent.inner.claude_sdk_executor import _resolve_gateway_env
        from omnigent.inner.databricks_executor import DatabricksCredentials

        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "omnigent.inner.databricks_executor._read_databrickscfg",
                return_value=DatabricksCredentials(host="https://my-workspace.com/", token="tok"),
            ),
        ):
            env = _resolve_gateway_env()
            self.assertFalse(env["ANTHROPIC_BASE_URL"].endswith("//"))
            self.assertTrue(env["ANTHROPIC_BASE_URL"].endswith("/ai-gateway/anthropic"))

    def test_no_creds_returns_empty(self):
        from omnigent.inner.claude_sdk_executor import _resolve_gateway_env

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("omnigent.inner.databricks_executor._read_databrickscfg", return_value=None),
        ):
            env = _resolve_gateway_env()
            self.assertEqual(env, {})

    def test_host_override_skips_profile_lookup(self):
        from omnigent.inner.claude_sdk_executor import _resolve_gateway_env

        with patch("omnigent.inner.databricks_executor._read_databrickscfg") as read_cfg:
            env = _resolve_gateway_env(
                profile="missing-profile",
                host_override="https://example.databricks.com/",
                base_url_override="https://example.databricks.com/ai-gateway/anthropic",
                auth_command_override="printf token",
            )

        read_cfg.assert_not_called()
        self.assertEqual(
            env["ANTHROPIC_BASE_URL"],
            "https://example.databricks.com/ai-gateway/anthropic",
        )
        self.assertEqual(env["OMNIGENT_CLAUDE_API_KEY_HELPER"], "printf token")

    def test_host_override_requires_base_url(self):
        from omnigent.inner.claude_sdk_executor import _resolve_gateway_env

        with self.assertRaisesRegex(OSError, "GATEWAY_BASE_URL"):
            _resolve_gateway_env(
                host_override="https://example.databricks.com/",
                auth_command_override="printf token",
            )

    def test_host_override_requires_auth_command(self):
        from omnigent.inner.claude_sdk_executor import _resolve_gateway_env

        with self.assertRaisesRegex(OSError, "GATEWAY_AUTH_COMMAND"):
            _resolve_gateway_env(
                host_override="https://example.databricks.com/",
                base_url_override="https://example.databricks.com/ai-gateway/anthropic",
            )


# ---------------------------------------------------------------------------
# Tests: Empty prompt handling
# ---------------------------------------------------------------------------


class TestEmptyPrompt(unittest.TestCase):
    def test_empty_prompt_yields_turn_complete(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        async def _t():
            executor = ClaudeSDKExecutor()
            events = [e async for e in executor.run_turn([], [], "")]
            self.assertEqual(len(events), 1)
            self.assertIsInstance(events[0], TurnComplete)
            # Empty-prompt short-circuit signals "no assistant text this
            # turn" via ``response=None`` rather than an empty string.
            self.assertIsNone(events[0].response)

        _run(_t())


class TestSystemMessages(unittest.TestCase):
    def test_databricks_auth_uses_api_key_helper_settings(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        captured_options = []

        class _ResultMessage:
            def __init__(self, session_id, result):
                self.session_id = session_id
                self.result = result

        class _FakeSDK:
            AssistantMessage = type("AssistantMessage", (), {})
            UserMessage = type("UserMessage", (), {})
            SystemMessage = type("SystemMessage", (), {})
            ResultMessage = _ResultMessage
            StreamEvent = type("StreamEvent", (), {})
            ClaudeAgentOptions = type(
                "ClaudeAgentOptions",
                (),
                {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
            )

            class ClaudeSDKClient:
                def __init__(self, options):
                    captured_options.append(options)

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    return None

                async def receive_response(self):
                    yield _ResultMessage("default", "ok")

                async def disconnect(self):
                    return None

        def _resolve_gateway_env(
            profile=None,
            *,
            host_override=None,
            base_url_override=None,
            auth_command_override=None,
            auth_refresh_interval_ms=None,
        ):
            return {
                "ANTHROPIC_BASE_URL": base_url_override or "https://host/ai-gateway/anthropic",
                "OMNIGENT_CLAUDE_API_KEY_HELPER": "databricks auth token --host https://host",
                "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": "900000",
                "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
            }

        shim_upstream: dict[str, str] = {}

        async def _t():
            executor = ClaudeSDKExecutor()
            executor._gateway = True
            executor._databricks_profile = "oss"
            executor._base_url_override = "https://host/ai-gateway/anthropic"
            executor._extra_env = _resolve_gateway_env(
                profile="oss",
                base_url_override="https://host/ai-gateway/anthropic",
            )
            with (
                patch(
                    "omnigent.inner.claude_sdk_executor._resolve_gateway_env",
                    _resolve_gateway_env,
                ),
                patch("omnigent.inner.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK),
            ):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hello"}],
                        [],
                        "",
                    )
                ]
            self.assertEqual(len(events), 1)
            # The gateway path routes the CLI through the local shim
            # (which restores thinking.display); record its target and
            # shut it down inside the loop that started it.
            self.assertIsNotNone(executor._gateway_shim)
            shim_upstream["base_url"] = executor._gateway_shim.base_url
            shim_upstream["upstream"] = executor._gateway_shim._upstream_base_url
            await executor._gateway_shim.aclose()

        _run(_t())

        self.assertEqual(len(captured_options), 1)
        settings = json.loads(captured_options[0].settings)
        self.assertEqual(
            settings["apiKeyHelper"],
            "databricks auth token --host https://host",
        )
        # The CLI talks to the loopback shim; the shim forwards to the
        # real gateway. A direct gateway URL here would mean the shim was
        # bypassed and opus thinking.display stays stripped.
        self.assertEqual(
            captured_options[0].env["ANTHROPIC_BASE_URL"],
            shim_upstream["base_url"],
        )
        self.assertTrue(shim_upstream["base_url"].startswith("http://127.0.0.1:"))
        self.assertEqual(shim_upstream["upstream"], "https://host/ai-gateway/anthropic")
        self.assertEqual(captured_options[0].env["CLAUDE_CODE_API_KEY_HELPER_TTL_MS"], "900000")
        self.assertNotIn("OMNIGENT_CLAUDE_API_KEY_HELPER", captured_options[0].env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", captured_options[0].env)

    def test_auth_retry_surfaces_executor_error(self):
        from claude_agent_sdk.types import (
            ClaudeAgentOptions as SDKClaudeAgentOptions,
        )
        from claude_agent_sdk.types import (
            StreamEvent as SDKStreamEvent,
        )
        from claude_agent_sdk.types import (
            SystemMessage as SDKSystemMessage,
        )

        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        class _Sentinel:
            pass

        class _FakeSDK:
            AssistantMessage = _Sentinel
            ResultMessage = _Sentinel
            UserMessage = _Sentinel
            SystemMessage = SDKSystemMessage
            StreamEvent = SDKStreamEvent
            ClaudeAgentOptions = SDKClaudeAgentOptions
            messages = [
                SDKSystemMessage(
                    subtype="api_retry",
                    data={
                        "type": "system",
                        "subtype": "api_retry",
                        "attempt": 1,
                        "max_retries": 10,
                        "retry_delay_ms": 500,
                        "error_status": 401,
                        "error": "authentication_failed",
                    },
                )
            ]

            class ClaudeSDKClient:
                def __init__(self, options):
                    self.options = options

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    return None

                async def receive_response(self):
                    for message in _FakeSDK.messages:
                        yield message

                async def disconnect(self):
                    return None

        async def _t():
            executor = ClaudeSDKExecutor()
            with patch("omnigent.inner.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hello"}],
                        [],
                        "",
                    )
                ]
            self.assertEqual(len(events), 1)
            self.assertIsInstance(events[0], ExecutorError)
            self.assertIn("authentication failed", events[0].message)
            self.assertIn("401", events[0].message)
            # Non-gateway executor should suggest checking CLI login, not databrickscfg
            self.assertIn("claude /status", events[0].message)
            self.assertNotIn("databrickscfg", events[0].message)

        _run(_t())

    def test_auth_retry_databricks_gateway_mentions_databrickscfg(self):
        """Databricks-profile gateway auth errors should mention ~/.databrickscfg."""
        from claude_agent_sdk.types import (
            ClaudeAgentOptions as SDKClaudeAgentOptions,
        )
        from claude_agent_sdk.types import (
            StreamEvent as SDKStreamEvent,
        )
        from claude_agent_sdk.types import (
            SystemMessage as SDKSystemMessage,
        )

        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        class _Sentinel:
            pass

        class _FakeSDK:
            AssistantMessage = _Sentinel
            ResultMessage = _Sentinel
            UserMessage = _Sentinel
            SystemMessage = SDKSystemMessage
            StreamEvent = SDKStreamEvent
            ClaudeAgentOptions = SDKClaudeAgentOptions
            messages = [
                SDKSystemMessage(
                    subtype="api_retry",
                    data={
                        "type": "system",
                        "subtype": "api_retry",
                        "attempt": 1,
                        "max_retries": 10,
                        "retry_delay_ms": 500,
                        "error_status": 401,
                        "error": "authentication_failed",
                    },
                )
            ]

            class ClaudeSDKClient:
                def __init__(self, options):
                    self.options = options

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    return None

                async def receive_response(self):
                    for message in _FakeSDK.messages:
                        yield message

                async def disconnect(self):
                    return None

        async def _t():
            # Create a gateway executor that uses a Databricks profile path.
            # gateway=True + no host/base_url overrides → _gateway_uses_databricks_profile is True.
            # Patch _resolve_gateway_env to avoid needing a real ~/.databrickscfg.
            with patch(
                "omnigent.inner.claude_sdk_executor._resolve_gateway_env",
                return_value={
                    "ANTHROPIC_BASE_URL": "https://host/ai-gateway/anthropic",
                    "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": "900000",
                    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
                    "OMNIGENT_CLAUDE_API_KEY_HELPER": "databricks auth token ...",
                },
            ):
                executor = ClaudeSDKExecutor(gateway=True)
            with patch("omnigent.inner.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hello"}],
                        [],
                        "",
                    )
                ]
            self.assertEqual(len(events), 1)
            self.assertIsInstance(events[0], ExecutorError)
            self.assertIn("authentication failed", events[0].message)
            # Databricks gateway should mention databrickscfg
            self.assertIn("databrickscfg", events[0].message)

        _run(_t())


# ---------------------------------------------------------------------------
# Tests: skills_filter → SDK skills option translation + plugin manifest
# ---------------------------------------------------------------------------


class TestSkillsFilterTranslation(unittest.TestCase):
    """
    Pin the mapping from the spec's ``skills_filter`` to the
    Claude Agent SDK's ``skills`` option.

    The filter has three meaningful values; getting any of them
    wrong silently changes which user / project / bundled skills
    the model sees. This is the seam between AGENTSPEC.md's YAML
    ``skills:`` field and the SDK's documented ``skills`` knob.
    """

    def test_all_lets_sdk_default_setting_sources(self) -> None:
        """
        ``"all"`` → SDK ``skills="all"`` and
        ``setting_sources=None`` (the SDK's default-derivation
        kicks in, producing ``["user", "project"]``).

        Claim: setting_sources is ``None`` (not ``[]``), letting
        the SDK fill it in. A regression that hardcoded an
        explicit list here would freeze the default and miss
        future SDK changes.
        """
        from omnigent.inner.claude_sdk_executor import _resolve_skills_option

        result = _resolve_skills_option("all")
        assert result is not None
        self.assertEqual(result.skills, "all")
        self.assertIsNone(result.setting_sources)

    def test_none_zeros_skills_and_setting_sources(self) -> None:
        """
        ``"none"`` → SDK ``skills=[]`` AND
        ``setting_sources=[]``.

        BOTH must be set to truly suppress host skills. The SDK's
        ``_apply_skills_defaults`` auto-fills
        ``setting_sources=["user","project"]`` when ``skills`` is
        non-None — including when ``skills=[]``. That auto-default
        loads ``~/.claude/skills/`` into the system prompt
        listing even though the ``Skill`` tool itself is hidden.
        Forcing ``setting_sources=[]`` is what actually keeps the
        listing empty. The user-reported regression: with only
        ``skills=[]`` set, ``skills: none`` in YAML still showed
        every host skill in the model's output.
        """
        from omnigent.inner.claude_sdk_executor import _resolve_skills_option

        result = _resolve_skills_option("none")
        assert result is not None
        self.assertEqual(result.skills, [])
        self.assertEqual(result.setting_sources, [])

    def test_list_lets_sdk_default_setting_sources(self) -> None:
        """A list of names round-trips and uses the SDK default."""
        from omnigent.inner.claude_sdk_executor import _resolve_skills_option

        result = _resolve_skills_option(["foo", "bar:baz"])
        assert result is not None
        self.assertEqual(result.skills, ["foo", "bar:baz"])
        self.assertIsNone(result.setting_sources)

    def test_unknown_string_returns_none_for_caller_fallback(self) -> None:
        """
        Unknown strings (e.g. malformed config bypass) return
        ``None`` so the caller can fall back to ``"all"``. The
        spec parser already validates, so this is a belt-and-
        suspenders defense at the executor boundary.
        """
        from omnigent.inner.claude_sdk_executor import _resolve_skills_option

        self.assertIsNone(_resolve_skills_option("bogus"))


# ---------------------------------------------------------------------------
# Tests: StreamEvent-based streaming
# ---------------------------------------------------------------------------


class TestStreamEventStreaming(unittest.TestCase):
    def test_live_clients_are_reused_per_omnigent_session(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        query_calls = []
        connect_calls = []

        class _ResultMessage:
            def __init__(self, session_id, result):
                self.session_id = session_id
                self.result = result

        class _FakeSDK:
            AssistantMessage = type("AssistantMessage", (), {})
            UserMessage = type("UserMessage", (), {})
            SystemMessage = type("SystemMessage", (), {})
            ResultMessage = _ResultMessage
            StreamEvent = type("StreamEvent", (), {})
            ClaudeAgentOptions = type(
                "ClaudeAgentOptions",
                (),
                {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
            )
            messages = []

            class ClaudeSDKClient:
                def __init__(self, options):
                    self.options = options

                async def connect(self):
                    connect_calls.append(self)
                    return

                async def query(self, prompt, session_id="default"):
                    query_calls.append(
                        {
                            "prompt": prompt,
                            "session_id": session_id,
                            "extra_args": getattr(self.options, "extra_args", {}),
                            "tools": getattr(self.options, "tools", None),
                            "allowed_tools": getattr(self.options, "allowed_tools", None),
                        }
                    )
                    result_session_id = (
                        session_id
                        if str(session_id).startswith("claude-")
                        else f"claude-{session_id}"
                    )
                    _FakeSDK.messages = [_ResultMessage(result_session_id, f"result for {prompt}")]

                async def receive_response(self):
                    for message in _FakeSDK.messages:
                        yield message

                async def disconnect(self):
                    return None

        async def _t():
            executor = ClaudeSDKExecutor()
            with patch("omnigent.inner.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
                session_a = [{"role": "user", "content": "hello", "session_id": "session-a"}]
                session_b = [{"role": "user", "content": "bonjour", "session_id": "session-b"}]

                events_a1 = [e async for e in executor.run_turn(session_a, [], "")]
                events_b1 = [e async for e in executor.run_turn(session_b, [], "")]
                events_a2 = [e async for e in executor.run_turn(session_a, [], "")]

            self.assertEqual(query_calls[0]["session_id"], "session-a")
            # ``--bare`` was previously included to suppress host config
            # leakage. It was dropped because bare mode also kills
            # CLAUDE.md auto-discovery, plugin sync, and skill loading —
            # which the spec's ``skills:`` field is the proper knob for.
            self.assertEqual(
                query_calls[0]["extra_args"],
                {"no-session-persistence": None},
            )
            # Skill is always in the base tool set so the Skill tool is
            # actually exposed to the model when ``skills="all"`` (the
            # SDK only adds Skill to ``allowedTools`` — without listing
            # it in ``tools`` the CLI passes ``--tools ""`` and zeros
            # the base set).
            self.assertEqual(query_calls[0]["tools"], ["Skill"])
            self.assertEqual(query_calls[0]["allowed_tools"], [])
            self.assertEqual(query_calls[1]["session_id"], "session-b")
            self.assertEqual(query_calls[2]["session_id"], "session-a")
            self.assertEqual(len(connect_calls), 2)
            self.assertEqual(len(executor._clients), 2)
            self.assertEqual(events_a1[-1].response, "result for hello")
            self.assertEqual(events_b1[-1].response, "result for bonjour")
            self.assertEqual(events_a2[-1].response, "result for hello")

        _run(_t())

    def test_os_env_spec_exposes_only_explicit_native_tools(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
        from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

        captured_options = {}

        class _ResultMessage:
            def __init__(self, session_id, result):
                self.session_id = session_id
                self.result = result

        class _FakeSDK:
            AssistantMessage = type("AssistantMessage", (), {})
            UserMessage = type("UserMessage", (), {})
            SystemMessage = type("SystemMessage", (), {})
            ResultMessage = _ResultMessage
            StreamEvent = type("StreamEvent", (), {})
            ClaudeAgentOptions = type(
                "ClaudeAgentOptions",
                (),
                {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
            )
            messages = []

            @staticmethod
            def tool(name, desc, params):
                def decorator(handler):
                    return type(
                        "Tool",
                        (),
                        {
                            "name": name,
                            "description": desc,
                            "parameters": params,
                            "handler": handler,
                        },
                    )()

                return decorator

            @staticmethod
            def create_sdk_mcp_server(**kwargs):
                return kwargs

            class ClaudeSDKClient:
                def __init__(self, options):
                    captured_options["tools"] = getattr(options, "tools", None)
                    captured_options["allowed_tools"] = getattr(options, "allowed_tools", None)

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    _FakeSDK.messages = [_ResultMessage(session_id, "done")]

                async def receive_response(self):
                    for message in _FakeSDK.messages:
                        yield message

                async def disconnect(self):
                    return None

        async def _t():
            # Explicit ``sandbox=none`` so the test runs on any
            # platform. The default ``OSEnvSandboxSpec.type`` is
            # ``"linux_bwrap"``; without this override,
            # ``resolve_sandbox`` calls into the bwrap backend and
            # raises on macOS even though the test isn't exercising
            # sandbox behavior.
            executor = ClaudeSDKExecutor(
                os_env=OSEnvSpec(
                    type="caller_process",
                    sandbox=OSEnvSandboxSpec(type="none"),
                ),
            )
            with patch("omnigent.inner.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hi", "session_id": "session-a"}],
                        [
                            {
                                "name": "sleep",
                                "description": "sleep",
                                "parameters": {"type": "object"},
                            }
                        ],
                        "",
                    )
                ]
            # OS operations route through sys_os_* MCP tools, not SDK
            # built-ins. Only Skill remains in the native base set.
            self.assertEqual(captured_options["tools"], ["Skill"])
            self.assertIn("mcp__omnigent__sleep", captured_options["allowed_tools"])
            self.assertNotIn("Bash", captured_options["allowed_tools"])
            self.assertIsInstance(events[-1], TurnComplete)

        _run(_t())

    def test_mcp_only_session_disables_native_tool_base_set(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        captured_options = {}

        class _ResultMessage:
            def __init__(self, session_id, result):
                self.session_id = session_id
                self.result = result

        class _FakeSDK:
            AssistantMessage = type("AssistantMessage", (), {})
            UserMessage = type("UserMessage", (), {})
            SystemMessage = type("SystemMessage", (), {})
            ResultMessage = _ResultMessage
            StreamEvent = type("StreamEvent", (), {})
            ClaudeAgentOptions = type(
                "ClaudeAgentOptions",
                (),
                {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
            )
            messages = []

            @staticmethod
            def tool(name, desc, params):
                def decorator(handler):
                    return type(
                        "Tool",
                        (),
                        {
                            "name": name,
                            "description": desc,
                            "parameters": params,
                            "handler": handler,
                        },
                    )()

                return decorator

            @staticmethod
            def create_sdk_mcp_server(**kwargs):
                return kwargs

            class ClaudeSDKClient:
                def __init__(self, options):
                    captured_options["tools"] = getattr(options, "tools", None)
                    captured_options["allowed_tools"] = getattr(options, "allowed_tools", None)

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    _FakeSDK.messages = [_ResultMessage(session_id, "done")]

                async def receive_response(self):
                    for message in _FakeSDK.messages:
                        yield message

                async def disconnect(self):
                    return None

        async def _t():
            executor = ClaudeSDKExecutor()
            with patch("omnigent.inner.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hi", "session_id": "session-a"}],
                        [
                            {
                                "name": "sleep",
                                "description": "sleep",
                                "parameters": {"type": "object"},
                            }
                        ],
                        "",
                    )
                ]
            # Default ``skills_filter="all"`` exposes the ``Skill``
            # tool so the model can invoke discovered skills via
            # the Claude SDK plugin mechanism. The OS tools
            # (Bash/Read/Edit/Write/Glob/Grep) stay absent — that's
            # what this test pins. ``Skill`` itself doesn't widen
            # the FS attack surface; it only loads pre-approved
            # SKILL.md content.
            self.assertEqual(captured_options["tools"], ["Skill"])
            self.assertEqual(captured_options["allowed_tools"], ["mcp__omnigent__sleep"])
            self.assertIsInstance(events[-1], TurnComplete)

        _run(_t())

    def test_session_send_tool_is_exposed_via_mcp(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        captured_options = {}

        class _ResultMessage:
            def __init__(self, session_id, result):
                self.session_id = session_id
                self.result = result

        class _FakeSDK:
            AssistantMessage = type("AssistantMessage", (), {})
            UserMessage = type("UserMessage", (), {})
            SystemMessage = type("SystemMessage", (), {})
            ResultMessage = _ResultMessage
            StreamEvent = type("StreamEvent", (), {})
            ClaudeAgentOptions = type(
                "ClaudeAgentOptions",
                (),
                {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
            )
            messages = []

            @staticmethod
            def tool(name, desc, params):
                def decorator(handler):
                    return type(
                        "Tool",
                        (),
                        {
                            "name": name,
                            "description": desc,
                            "parameters": params,
                            "handler": handler,
                        },
                    )()

                return decorator

            @staticmethod
            def create_sdk_mcp_server(**kwargs):
                return kwargs

            class ClaudeSDKClient:
                def __init__(self, options):
                    captured_options["allowed_tools"] = getattr(options, "allowed_tools", None)
                    captured_options["system_prompt"] = getattr(options, "system_prompt", None)

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    _FakeSDK.messages = [_ResultMessage(session_id, "done")]

                async def receive_response(self):
                    for message in _FakeSDK.messages:
                        yield message

                async def disconnect(self):
                    return None

        async def _t():
            executor = ClaudeSDKExecutor()
            with patch("omnigent.inner.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hi", "session_id": "session-a"}],
                        [
                            {
                                "name": "sys_session_send",
                                "description": "Send into session",
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
                        "Delegate through `sys_session_send`.",
                    )
                ]
            self.assertIn("mcp__omnigent__sys_session_send", captured_options["allowed_tools"])
            self.assertIn(
                "use `mcp__omnigent__sys_session_send` when instructions say `sys_session_send`",
                captured_options["system_prompt"],
            )
            self.assertIsInstance(events[-1], TurnComplete)

        _run(_t())

    def test_crashed_session_refuses_future_turns(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        class _FakeSDK:
            AssistantMessage = type("AssistantMessage", (), {})
            UserMessage = type("UserMessage", (), {})
            SystemMessage = type("SystemMessage", (), {})
            ResultMessage = type("ResultMessage", (), {})
            StreamEvent = type("StreamEvent", (), {})
            ClaudeAgentOptions = type(
                "ClaudeAgentOptions",
                (),
                {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
            )

            class ClaudeSDKClient:
                def __init__(self, options):
                    self.options = options

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    raise RuntimeError("claude subprocess crashed")

                async def receive_response(self):
                    if False:
                        yield None

                async def disconnect(self):
                    return None

        async def _t():
            executor = ClaudeSDKExecutor()
            messages = [{"role": "user", "content": "hello", "session_id": "session-a"}]
            with patch("omnigent.inner.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
                first_events = [e async for e in executor.run_turn(messages, [], "")]
                second_events = [e async for e in executor.run_turn(messages, [], "")]

            self.assertIsInstance(first_events[0], ExecutorError)
            self.assertIn("claude subprocess crashed", first_events[0].message)
            self.assertIsInstance(second_events[0], ExecutorError)
            self.assertIn("cannot continue in this Session", second_events[0].message)
            self.assertIn("claude subprocess crashed", second_events[0].message)
            self.assertIn("session-a", executor._crashed_sessions)

        _run(_t())

    def test_close_session_disconnects_live_client(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor, _ClaudeClientState

        disconnect = AsyncMock()
        client = type("Client", (), {"disconnect": disconnect})()

        async def _t():
            executor = ClaudeSDKExecutor()
            executor._clients["session-a"] = _ClaudeClientState(client=client, model=None)
            await executor.close_session("session-a")
            disconnect.assert_awaited_once_with()
            self.assertEqual(executor._clients, {})

        _run(_t())

    def test_interrupt_session_interrupts_then_drops_session(self):
        """A user interrupt fires a safe interrupt, then drops the session.

        ``run_turn`` decides whether to resume via
        ``session_key in self._clients``. Dropping the session after an
        interrupt is what forces the next turn to rebuild from full
        history (with the runner's ``[System: interrupted]`` marker)
        instead of resuming a live session whose transcript still holds
        the abandoned prompt. If ``_clients`` were left populated, the
        next turn would resume and silently continue the canceled
        instruction.
        """
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor, _ClaudeClientState

        interrupt = AsyncMock()
        disconnect = AsyncMock()
        client = type("Client", (), {"interrupt": interrupt, "disconnect": disconnect})()

        async def _t():
            executor = ClaudeSDKExecutor()
            executor._clients["session-a"] = _ClaudeClientState(client=client, model=None)

            interrupted = await executor.interrupt_session("session-a")

            self.assertTrue(interrupted)
            # Safe interrupt is attempted first to halt the in-flight turn.
            interrupt.assert_awaited_once_with()
            # Then the session is torn down (disconnect) and removed from
            # _clients so the next turn cannot resume it. An empty dict here
            # is the invariant that prevents the canceled-instruction leak.
            disconnect.assert_awaited_once_with()
            self.assertEqual(executor._clients, {})

        _run(_t())

    def test_interrupt_session_closes_even_when_interrupt_fails(self):
        """A failed safe interrupt still drops the session.

        The session must be torn down regardless of whether the SDK's
        ``interrupt()`` succeeds — otherwise a flaky interrupt would
        leave the abandoned-prompt session resumable. If ``close_session``
        is not awaited here, the interrupt-failure path leaks the session.
        """
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor, _ClaudeClientState

        async def fail_interrupt():
            raise RuntimeError("boom")

        client = type("Client", (), {"interrupt": fail_interrupt})()

        async def _t():
            executor = ClaudeSDKExecutor()
            executor._clients["session-a"] = _ClaudeClientState(client=client, model=None)
            with patch.object(executor, "close_session", new=AsyncMock()) as close_session:
                interrupted = await executor.interrupt_session("session-a")

            self.assertTrue(interrupted)
            close_session.assert_awaited_once_with("session-a")

        _run(_t())

    def test_close_disconnects_all_live_clients(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor, _ClaudeClientState

        disconnect_a = AsyncMock()
        disconnect_b = AsyncMock()
        client_a = type("Client", (), {"disconnect": disconnect_a})()
        client_b = type("Client", (), {"disconnect": disconnect_b})()

        async def _t():
            executor = ClaudeSDKExecutor()
            executor._clients["session-a"] = _ClaudeClientState(client=client_a, model=None)
            executor._clients["session-b"] = _ClaudeClientState(client=client_b, model=None)
            await executor.close()
            disconnect_a.assert_awaited_once_with()
            disconnect_b.assert_awaited_once_with()
            self.assertEqual(executor._clients, {})

        _run(_t())

    def test_close_session_force_closes_on_loop_mismatch(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor, _ClaudeClientState

        client = type("Client", (), {})()
        client.disconnect = AsyncMock()

        async def _t():
            executor = ClaudeSDKExecutor()
            executor._clients["session-a"] = _ClaudeClientState(
                client=client,
                model=None,
                loop=object(),
                task=None,
            )
            with patch.object(executor, "_force_close_client", new=AsyncMock()) as force_close:
                await executor.close_session("session-a")
            client.disconnect.assert_not_awaited()
            force_close.assert_awaited_once_with(client)
            self.assertEqual(executor._clients, {})

        _run(_t())

    def test_text_deltas_yield_text_chunks(self):
        from claude_agent_sdk.types import (
            ClaudeAgentOptions as SDKClaudeAgentOptions,
        )
        from claude_agent_sdk.types import (
            ResultMessage as SDKResultMessage,
        )
        from claude_agent_sdk.types import (
            StreamEvent as SDKStreamEvent,
        )

        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        class _Sentinel:
            pass

        class _FakeSDK:
            AssistantMessage = _Sentinel
            UserMessage = _Sentinel
            SystemMessage = _Sentinel
            StreamEvent = SDKStreamEvent
            ResultMessage = SDKResultMessage
            ClaudeAgentOptions = SDKClaudeAgentOptions
            messages = [
                SDKStreamEvent(
                    uuid="u1",
                    session_id="s1",
                    event={
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "Hello"},
                    },
                ),
                SDKStreamEvent(
                    uuid="u2",
                    session_id="s1",
                    event={
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": " world"},
                    },
                ),
                SDKResultMessage(
                    subtype="result",
                    session_id="s1",
                    result="Hello world",
                    total_cost_usd=0.0,
                    duration_ms=100,
                    duration_api_ms=80,
                    is_error=False,
                    num_turns=1,
                ),
            ]

            class ClaudeSDKClient:
                def __init__(self, options):
                    self.options = options

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    return None

                async def receive_response(self):
                    for message in _FakeSDK.messages:
                        yield message

                async def disconnect(self):
                    return None

        async def _t():
            executor = ClaudeSDKExecutor()
            with patch("omnigent.inner.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "hi"}],
                        [],
                        "",
                    )
                ]
            chunks = [e for e in events if isinstance(e, TextChunk)]
            self.assertEqual(len(chunks), 2)
            self.assertEqual(chunks[0].text, "Hello")
            self.assertEqual(chunks[1].text, " world")
            self.assertIsInstance(events[-1], TurnComplete)
            self.assertEqual(events[-1].response, "Hello world")

        _run(_t())

    def test_tool_use_assembly_yields_tool_call_request_with_args(self):
        """
        A streaming turn that contains a ``content_block_start`` tool_use
        event plus a subsequent ``AssistantMessage`` with the assembled
        ``ToolUseBlock.input`` must yield exactly one
        :class:`ToolCallRequest` carrying the assembled args.

        Regression pin: the streaming path used to emit
        ``ToolCallRequest(name=..., args={})`` at ``content_block_start``,
        before Claude had streamed the ``input_json_delta`` events. The
        REPL then rendered tool calls as ``⏵ Bash()`` with no visible
        command because the args summary saw an empty dict. The fix
        defers the emission to the ``AssistantMessage`` branch where
        ``tool_block.input`` is populated.

        The test fails pre-fix at the args assertion below:
        ``reqs[0].args`` is ``{}``, not ``{"command": "ls -la"}``.
        """
        # Real types from the SDK so the production
        # ``isinstance(message, sdk.AssistantMessage)`` and
        # ``isinstance(block, sdk.ToolUseBlock)`` checks fire. A
        # MagicMock / _Sentinel would silently bypass both branches
        # and the test would pass vacuously with zero emissions.
        from claude_agent_sdk.types import (
            AssistantMessage as SDKAssistantMessage,
        )
        from claude_agent_sdk.types import (
            ClaudeAgentOptions as SDKClaudeAgentOptions,
        )
        from claude_agent_sdk.types import (
            ResultMessage as SDKResultMessage,
        )
        from claude_agent_sdk.types import (
            StreamEvent as SDKStreamEvent,
        )
        from claude_agent_sdk.types import (
            ToolUseBlock as SDKToolUseBlock,
        )

        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        class _Sentinel:
            pass

        class _FakeSDK:
            AssistantMessage = SDKAssistantMessage
            UserMessage = _Sentinel
            SystemMessage = _Sentinel
            StreamEvent = SDKStreamEvent
            ResultMessage = SDKResultMessage
            ClaudeAgentOptions = SDKClaudeAgentOptions
            TextBlock = _Sentinel
            ThinkingBlock = _Sentinel
            ToolUseBlock = SDKToolUseBlock
            ToolResultBlock = _Sentinel
            messages = [
                # Live SDK order: tool_use block_start fires before
                # Claude has streamed the full ``input_json_delta``
                # sequence. The production path now only tracks the
                # tool id here — it does NOT emit ToolCallRequest
                # yet. Keeping this event in the fixture ensures a
                # future regression that re-introduces the early
                # emission fails the ``len(reqs) == 1`` check below.
                SDKStreamEvent(
                    uuid="u1",
                    session_id="s1",
                    event={
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {
                            "type": "tool_use",
                            "id": "tool_1",
                            "name": "Bash",
                            "input": {},
                        },
                    },
                ),
                # Full AssistantMessage after input deltas have
                # assembled — this is where ``tool_block.input`` is
                # populated and the ToolCallRequest must fire.
                SDKAssistantMessage(
                    content=[
                        SDKToolUseBlock(
                            id="tool_1",
                            name="Bash",
                            input={"command": "ls -la"},
                        ),
                    ],
                    model="claude-sonnet-4",
                ),
                SDKResultMessage(
                    subtype="result",
                    session_id="s1",
                    result="",
                    total_cost_usd=0.0,
                    duration_ms=100,
                    duration_api_ms=80,
                    is_error=False,
                    num_turns=1,
                ),
            ]

            class ClaudeSDKClient:
                def __init__(self, options):
                    self.options = options

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    return None

                async def receive_response(self):
                    for message in _FakeSDK.messages:
                        yield message

                async def disconnect(self):
                    return None

        async def _t():
            executor = ClaudeSDKExecutor()
            with patch("omnigent.inner.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "run ls"}],
                        [],
                        "",
                    )
                ]
            reqs = [e for e in events if isinstance(e, ToolCallRequest)]
            # 1 = exactly one emission. A value of 2 would mean the
            # early (empty-args) emission was re-introduced alongside
            # the AssistantMessage one; 0 would mean the new path
            # silently dropped the emission.
            self.assertEqual(len(reqs), 1)
            self.assertEqual(reqs[0].name, "Bash")
            # The specific regression: args must be the assembled dict
            # from ``tool_block.input``, not ``{}``. Pre-fix this was
            # ``{}`` and downstream rendered ``⏵ Bash()``.
            self.assertEqual(reqs[0].args, {"command": "ls -la"})

        _run(_t())

    def test_tool_result_error_yields_tool_call_complete_error(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        class _ToolUseBlock:
            def __init__(self, id, name, input):
                self.id = id
                self.name = name
                self.input = input

        class _ToolResultBlock:
            def __init__(self, tool_use_id, content, is_error):
                self.tool_use_id = tool_use_id
                self.content = content
                self.is_error = is_error

        class _AssistantMessage:
            def __init__(self, content):
                self.content = content

        class _UserMessage:
            def __init__(self, content):
                self.content = content

        class _ResultMessage:
            def __init__(self, session_id, result):
                self.session_id = session_id
                self.result = result

        class _FakeSDK:
            AssistantMessage = _AssistantMessage
            UserMessage = _UserMessage
            SystemMessage = type("SystemMessage", (), {})
            ResultMessage = _ResultMessage
            ToolUseBlock = _ToolUseBlock
            ToolResultBlock = _ToolResultBlock
            TextBlock = type("TextBlock", (), {})
            ThinkingBlock = type("ThinkingBlock", (), {})
            ClaudeAgentOptions = type(
                "ClaudeAgentOptions",
                (),
                {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
            )
            messages = [
                _AssistantMessage([_ToolUseBlock("tool_1", "fail", {})]),
                _UserMessage(
                    [
                        _ToolResultBlock(
                            "tool_1",
                            [{"type": "text", "text": '{"error": "boom"}'}],
                            True,
                        )
                    ]
                ),
                _ResultMessage("s1", "done"),
            ]

            class ClaudeSDKClient:
                def __init__(self, options):
                    self.options = options

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    return None

                async def receive_response(self):
                    for message in _FakeSDK.messages:
                        yield message

                async def disconnect(self):
                    return None

        async def _t():
            executor = ClaudeSDKExecutor()
            with patch("omnigent.inner.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "run fail"}],
                        [],
                        "",
                    )
                ]
            tool_events = [e for e in events if isinstance(e, ToolCallComplete)]
            self.assertEqual(len(tool_events), 1)
            self.assertEqual(tool_events[0].status, ToolCallStatus.ERROR)
            self.assertEqual(tool_events[0].error, "boom")

        _run(_t())

    def test_tool_result_blocked_yields_blocked_status(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        class _ToolUseBlock:
            def __init__(self, id, name, input):
                self.id = id
                self.name = name
                self.input = input

        class _ToolResultBlock:
            def __init__(self, tool_use_id, content, is_error):
                self.tool_use_id = tool_use_id
                self.content = content
                self.is_error = is_error

        class _AssistantMessage:
            def __init__(self, content):
                self.content = content

        class _UserMessage:
            def __init__(self, content):
                self.content = content

        class _ResultMessage:
            def __init__(self, session_id, result):
                self.session_id = session_id
                self.result = result

        class _FakeSDK:
            AssistantMessage = _AssistantMessage
            UserMessage = _UserMessage
            SystemMessage = type("SystemMessage", (), {})
            ResultMessage = _ResultMessage
            ToolUseBlock = _ToolUseBlock
            ToolResultBlock = _ToolResultBlock
            TextBlock = type("TextBlock", (), {})
            ThinkingBlock = type("ThinkingBlock", (), {})
            ClaudeAgentOptions = type(
                "ClaudeAgentOptions",
                (),
                {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
            )
            messages = [
                _AssistantMessage([_ToolUseBlock("tool_1", "blocked_tool", {})]),
                _UserMessage(
                    [
                        _ToolResultBlock(
                            "tool_1",
                            [{"type": "text", "text": '{"blocked": true, "reason": "Nope"}'}],
                            True,
                        )
                    ]
                ),
                _ResultMessage("s1", "done"),
            ]

            class ClaudeSDKClient:
                def __init__(self, options):
                    self.options = options

                async def connect(self):
                    return None

                async def query(self, prompt, session_id="default"):
                    return None

                async def receive_response(self):
                    for message in _FakeSDK.messages:
                        yield message

                async def disconnect(self):
                    return None

        async def _t():
            executor = ClaudeSDKExecutor()
            with patch("omnigent.inner.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
                events = [
                    e
                    async for e in executor.run_turn(
                        [{"role": "user", "content": "run blocked"}],
                        [],
                        "",
                    )
                ]
            tool_events = [e for e in events if isinstance(e, ToolCallComplete)]
            self.assertEqual(len(tool_events), 1)
            self.assertEqual(tool_events[0].status, ToolCallStatus.BLOCKED)
            self.assertEqual(tool_events[0].error, "Nope")

        _run(_t())


# ---------------------------------------------------------------------------
# Tests: _unset_env_var context manager
# ---------------------------------------------------------------------------


def test_unset_env_var_removes_and_restores(monkeypatch):
    """Env var present before ``with`` is absent during, restored after."""
    from omnigent.inner.claude_sdk_executor import _unset_env_var

    monkeypatch.setenv("CLAUDECODE", "parent-value")
    with _unset_env_var("CLAUDECODE"):
        assert "CLAUDECODE" not in os.environ
    assert os.environ["CLAUDECODE"] == "parent-value"


def test_unset_env_var_noop_when_unset(monkeypatch):
    """When env var is not set before ``with``, block runs cleanly and key stays unset."""
    from omnigent.inner.claude_sdk_executor import _unset_env_var

    monkeypatch.delenv("CLAUDECODE", raising=False)
    with _unset_env_var("CLAUDECODE"):
        assert "CLAUDECODE" not in os.environ
    assert "CLAUDECODE" not in os.environ


def test_unset_env_var_restores_on_exception(monkeypatch):
    """Restoration must still happen when the block raises."""
    from omnigent.inner.claude_sdk_executor import _unset_env_var

    monkeypatch.setenv("CLAUDECODE", "original")
    with pytest.raises(RuntimeError, match="boom"):
        with _unset_env_var("CLAUDECODE"):
            assert "CLAUDECODE" not in os.environ
            raise RuntimeError("boom")
    assert os.environ["CLAUDECODE"] == "original"


def test_databricks_model_without_routing_raises() -> None:
    """``databricks-*`` model with ``gateway=False`` raises ``ValueError``.

    Without the guard the executor silently hits ``api.anthropic.com``
    and surfaces a confusing "model not found" error.
    """
    import pytest

    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

    with pytest.raises(ValueError, match="Databricks-hosted model"):
        ClaudeSDKExecutor(
            model="databricks-claude-sonnet-4-6",
            gateway=False,
        )


def test_non_databricks_model_without_routing_does_not_raise() -> None:
    """Non-``databricks-*`` model with ``gateway=False`` must not raise.

    Ensures the guard only fires on the ``databricks-`` prefix.
    """
    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

    executor = ClaudeSDKExecutor(
        model="claude-3-5-sonnet-20241022",
        gateway=False,
    )
    assert not executor._gateway


# ---------------------------------------------------------------------------
# ANTHROPIC_API_KEY stripping during connect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_api_key_stripped_during_connect(monkeypatch):
    """``_get_or_create_client`` must strip ``ANTHROPIC_API_KEY`` from
    ``os.environ`` during the ``connect()`` window so the Claude CLI
    uses subscription auth instead of a developer API key.

    This test drives ``_get_or_create_client`` with a stub SDK and
    captures ``os.environ`` at the moment ``connect()`` is invoked,
    ensuring both ``CLAUDECODE`` and ``ANTHROPIC_API_KEY`` are absent.
    """
    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

    # Env snapshot captured inside connect() -- proves the real code
    # path strips the keys, not just a standalone _unset_env_var call.
    connect_env: dict[str, str] = {}

    class _StubClient:
        """Minimal client whose connect() captures os.environ."""

        def __init__(self, options):
            """Store options for protocol compliance.

            :param options: SDK options object (unused by stub).
            """
            self.options = options
            self._query = None
            self._transport = None

        async def connect(self) -> None:
            """Record env state at the moment the real code calls connect."""
            connect_env.update(os.environ)

        async def disconnect(self) -> None:
            """No-op disconnect."""

    class _StubSDK:
        """Minimal SDK whose ClaudeSDKClient captures env during connect."""

        ClaudeSDKClient = _StubClient

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-secret")
    monkeypatch.setenv("CLAUDECODE", "parent-value")

    executor = ClaudeSDKExecutor()
    options = SimpleNamespace()
    await executor._get_or_create_client(
        _StubSDK,  # type: ignore[arg-type]
        session_key="test-session",
        options=options,
        model=None,
    )

    # Both keys must be absent at the moment connect() ran.
    assert "ANTHROPIC_API_KEY" not in connect_env, (
        "ANTHROPIC_API_KEY leaked into connect() -- subscription auth bypassed"
    )
    assert "CLAUDECODE" not in connect_env, (
        "CLAUDECODE leaked into connect() -- nested-session error risk"
    )
    # Both are restored after _get_or_create_client returns.
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test-secret"
    assert os.environ["CLAUDECODE"] == "parent-value"


@pytest.mark.asyncio
async def test_get_or_create_client_surfaces_cli_stderr_on_connect_timeout(monkeypatch):
    """A connect timeout must include the CLI's stderr tail in the
    raised ``TimeoutError`` so CI logs surface what the subprocess was
    doing while it hung.
    """
    from omnigent.inner import claude_sdk_executor as cse
    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

    monkeypatch.setattr(cse, "_CONNECT_TIMEOUT_SECONDS", 0.2)

    class _StubClient:
        def __init__(self, options) -> None:
            self.options = options
            # ``_force_close_client`` reads these on the failure path.
            self._query = None
            self._transport = None

        async def connect(self) -> None:
            assert callable(self.options.stderr), (
                "executor must install a stderr callback before connect"
            )
            self.options.stderr("Spawning claude CLI...\n")
            self.options.stderr("ERROR: hanging on auth refresh\n")
            await asyncio.sleep(5.0)

        async def disconnect(self) -> None:
            pass

    class _StubSDK:
        ClaudeSDKClient = _StubClient

    executor = ClaudeSDKExecutor()
    options = SimpleNamespace(stderr=None)

    with pytest.raises(TimeoutError) as excinfo:
        await executor._get_or_create_client(
            _StubSDK,  # type: ignore[arg-type]
            session_key="test-session",
            options=options,
            model=None,
        )

    msg = str(excinfo.value)
    # ``(no CLI stderr captured)`` here means the tee callback was never wired.
    assert "Spawning claude CLI" in msg, msg
    assert "hanging on auth refresh" in msg, msg
    assert "after 0s" in msg
    # ``_tee_stderr`` must be unwired in the finally so the buffer can be
    # GC'd and post-connect stderr flows directly to the original callback.
    assert options.stderr is None


@pytest.mark.parametrize("env_value", ["1", "true", "yes"])
def test_prepare_claude_cli_path_bypasses_wrapper_when_env_set(
    monkeypatch, caplog, env_value: str
) -> None:
    """
    ``OMNIGENT_CLAUDE_SDK_NO_SANDBOX`` (any truthy value) must skip
    ``create_exec_launcher`` and hand back the raw CLI path. Used as a
    diagnostic knob to isolate the sandbox as a cause of the silent
    claude-sdk connect hang on the nightly Linux runner.
    """
    from omnigent.inner.claude_sdk_executor import prepare_claude_cli_path
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    monkeypatch.setenv("OMNIGENT_CLAUDE_SDK_NO_SANDBOX", env_value)

    def _fail_if_called(*args, **kwargs) -> str:
        raise AssertionError("create_exec_launcher must not be called when bypass is enabled")

    monkeypatch.setattr(
        "omnigent.inner.claude_sdk_executor.create_exec_launcher",
        _fail_if_called,
    )

    spec = OSEnvSpec(
        type="caller_process",
        cwd="/tmp/work",
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            read_paths=["."],
            write_paths=["."],
            allow_network=True,
        ),
    )
    with caplog.at_level(logging.WARNING, logger="omnigent.inner.claude_sdk_executor"):
        prepared = prepare_claude_cli_path("/usr/bin/claude", spec)

    assert prepared.cli_path == "/usr/bin/claude"
    # Bypass keeps native tools off so the diagnostic isolates the
    # CLI-startup variable; matches the fallback shape used when the
    # sandbox can't be applied for other reasons (line 569-574).
    assert prepared.enable_native_tools is False
    # The bypass must be loud in CI: a missing warning means a future
    # diagnostic run could silently fall through without us noticing.
    assert any("Sandbox bypass active" in record.getMessage() for record in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def test_prepare_tight_cli_process_path_bypasses_wrapper_when_env_set(monkeypatch) -> None:
    """``prepare_tight_cli_process_path`` must also honor the bypass env."""
    from omnigent.inner.claude_sdk_executor import prepare_tight_cli_process_path

    monkeypatch.setenv("OMNIGENT_CLAUDE_SDK_NO_SANDBOX", "1")

    def _fail_if_called(*args, **kwargs) -> str:
        raise AssertionError("create_exec_launcher must not be called when bypass is enabled")

    monkeypatch.setattr(
        "omnigent.inner.claude_sdk_executor.create_exec_launcher",
        _fail_if_called,
    )

    assert prepare_tight_cli_process_path("/usr/bin/claude") == "/usr/bin/claude"


# ---------------------------------------------------------------------------
# Tests: _to_anthropic_content_blocks
# ---------------------------------------------------------------------------


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def test_to_anthropic_content_blocks_pdf_uses_base64_source() -> None:
    """PDF input_file blocks must use ``source.type = "base64"`` — the
    only MIME Anthropic accepts for the base64 document source shape."""
    pdf_data = _b64(b"%PDF-1.4 fake pdf content".decode("latin-1"))
    blocks = [{"type": "input_file", "file_data": f"data:application/pdf;base64,{pdf_data}"}]
    result = _to_anthropic_content_blocks(blocks)
    assert len(result) == 1
    doc = result[0]
    assert doc["type"] == "document"
    assert doc["source"]["type"] == "base64"
    assert doc["source"]["media_type"] == "application/pdf"
    assert doc["source"]["data"] == pdf_data


def test_to_anthropic_content_blocks_markdown_uses_text_source() -> None:
    """Markdown input_file blocks must use ``source.type = "text"`` —
    Anthropic rejects ``base64`` source with non-PDF media types."""
    md_content = "# Hello\n\nThis is markdown."
    md_b64 = _b64(md_content)
    blocks = [{"type": "input_file", "file_data": f"data:text/markdown;base64,{md_b64}"}]
    result = _to_anthropic_content_blocks(blocks)
    assert len(result) == 1
    doc = result[0]
    assert doc["type"] == "document"
    assert doc["source"]["type"] == "text"
    assert doc["source"]["media_type"] == "text/plain"
    assert doc["source"]["data"] == md_content


def test_to_anthropic_content_blocks_plain_text_uses_text_source() -> None:
    """``text/plain`` input_file blocks must also use ``source.type = "text"``."""
    content = "just some plain text"
    b64 = _b64(content)
    blocks = [{"type": "input_file", "file_data": f"data:text/plain;base64,{b64}"}]
    result = _to_anthropic_content_blocks(blocks)
    assert len(result) == 1
    doc = result[0]
    assert doc["type"] == "document"
    assert doc["source"]["type"] == "text"
    assert doc["source"]["media_type"] == "text/plain"
    assert doc["source"]["data"] == content


# ---------------------------------------------------------------------------
# Tests: connect-failure stderr surfacing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_create_client_surfaces_cli_stderr_on_connect_error() -> None:
    """A non-timeout connect failure includes captured CLI stderr."""
    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

    class _StubClient:
        def __init__(self, options: object) -> None:
            self._stderr_cb = getattr(options, "stderr", None)
            self._query = None
            self._transport = None

        async def connect(self) -> None:
            if self._stderr_cb is not None:
                self._stderr_cb("unknown option '--no-session-persistence'\n")
            raise RuntimeError("Command failed with exit code 1; Check stderr output for details")

        async def disconnect(self) -> None:
            pass

    class _StubSDK:
        ClaudeSDKClient = _StubClient

    executor = ClaudeSDKExecutor()
    options = type("Opts", (), {"stderr": None})()

    with pytest.raises(RuntimeError) as excinfo:
        await executor._get_or_create_client(
            _StubSDK,  # type: ignore[arg-type]
            session_key="test-session",
            options=options,
            model=None,
        )

    msg = str(excinfo.value)
    assert "unknown option" in msg, (
        f"Stderr not included in connect error.\nExpected 'unknown option' in: {msg!r}"
    )
    assert "Command failed" in msg, msg


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Tests: TurnComplete.usage populated from ResultMessage.usage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_message_usage_populates_turn_complete_usage() -> None:
    """``ResultMessage.usage`` flows through to ``TurnComplete.usage``.

    The claude-sdk executor must extract the provider-reported token counts
    from ``ResultMessage.usage`` and forward them via ``TurnComplete.usage``
    so the REPL context ring and ``/context`` command can display accurate
    per-turn fill without falling back to a local ``count_tokens()`` estimate.

    Regression guard: before this fix the executor yielded
    ``TurnComplete(response=..., usage=None)`` unconditionally, causing the
    context ring to stop updating after the first turn (the idle-event fallback
    only fired when ``host.tokens_used is None``, which was only true on the
    first turn once the local estimate had been set).
    """
    from unittest.mock import patch

    from claude_agent_sdk.types import (
        ClaudeAgentOptions as SDKClaudeAgentOptions,
    )
    from claude_agent_sdk.types import ResultMessage as SDKResultMessage
    from claude_agent_sdk.types import StreamEvent as SDKStreamEvent

    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

    class _Sentinel:
        pass

    sdk_result = SDKResultMessage(
        subtype="result",
        session_id="s1",
        result="Hello",
        total_cost_usd=0.0,
        duration_ms=100,
        duration_api_ms=80,
        is_error=False,
        num_turns=1,
        usage={
            "input_tokens": 500,
            "output_tokens": 200,
            "cache_read_input_tokens": 50,
            "cache_creation_input_tokens": 0,
        },
    )

    class _FakeSDK:
        AssistantMessage = _Sentinel
        UserMessage = _Sentinel
        SystemMessage = _Sentinel
        StreamEvent = SDKStreamEvent
        ResultMessage = SDKResultMessage
        ClaudeAgentOptions = SDKClaudeAgentOptions
        messages = [sdk_result]

        class ClaudeSDKClient:
            def __init__(self, options):
                self.options = options

            async def connect(self):
                return None

            async def query(self, prompt, session_id="default"):
                return None

            async def receive_response(self):
                for message in _FakeSDK.messages:
                    yield message

            async def disconnect(self):
                return None

    executor = ClaudeSDKExecutor()
    with patch("omnigent.inner.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
        events = [
            e
            async for e in executor.run_turn(
                [{"role": "user", "content": "hi"}],
                [],
                "",
            )
        ]

    turn = next(e for e in events if isinstance(e, TurnComplete))

    # Provider-reported counts must appear verbatim — if this is None the
    # REPL context ring will never update after the first turn for claude-sdk.
    assert turn.usage is not None, (
        "TurnComplete.usage is None — ResultMessage.usage was not captured. "
        "The context ring and /context command will show stale data after "
        "the first turn."
    )

    # input_tokens / output_tokens must come through from ResultMessage.usage
    assert turn.usage["input_tokens"] == 500, (
        f"input_tokens {turn.usage['input_tokens']} != 500 — "
        "ResultMessage.usage['input_tokens'] was not forwarded."
    )
    assert turn.usage["output_tokens"] == 200, (
        f"output_tokens {turn.usage['output_tokens']} != 200 — "
        "ResultMessage.usage['output_tokens'] was not forwarded."
    )

    # total_tokens is computed as input + output so the toolbar ring can
    # use usage["total_tokens"] without also needing individual fields.
    assert turn.usage["total_tokens"] == 700, (
        f"total_tokens {turn.usage['total_tokens']} != 700 (500+200) — "
        "total_tokens must be computed as input_tokens + output_tokens."
    )

    # Cache fields pass through as-is so the server can persist them.
    assert turn.usage.get("cache_read_input_tokens") == 50, (
        f"cache_read_input_tokens {turn.usage.get('cache_read_input_tokens')} != 50 — "
        "extra usage keys from ResultMessage.usage should be forwarded verbatim."
    )

    # context_tokens is the real prompt size = input + cache_creation +
    # cache_read (500 + 0 + 50). The runner reads this to track context-window
    # fill; without it the runner falls back to total_tokens (input + output,
    # non-cached only), which under-counts because the bulk of a long
    # conversation's prompt lives in cache_read_input_tokens. Note it differs
    # from total_tokens here (550 != 700) precisely because total ignores the
    # cached prompt while context includes it.
    assert turn.usage["context_tokens"] == 550, (
        f"context_tokens {turn.usage.get('context_tokens')} != 550 (500+0+50) — "
        "context_tokens must sum input + cache_creation + cache_read so the "
        "runner reports accurate, accumulating context-window fill."
    )

    # The resolved model is forwarded in usage so the server cost path can
    # price the turn even when the agent spec pins no llm.model. The key must
    # be present (here it's None — no model is configured in this unit test).
    assert "model" in turn.usage, (
        "TurnComplete.usage is missing the 'model' key — the server cost path "
        "relies on it to price relay turns whose spec pins no llm.model."
    )


@pytest.mark.asyncio
async def test_context_tokens_uses_last_call_not_cumulative_on_multi_iteration_turn() -> None:
    """``context_tokens`` must reflect the LAST API call, not the cumulative sum.

    A single user turn can drive several internal API calls (the SDK's
    tool-use loop). ``ResultMessage.usage`` reports the CUMULATIVE usage
    across all of them, so its ``input_tokens`` / cache buckets are the
    sum of every iteration's prompt. Since each iteration re-sends the
    whole (growing) conversation, summing the prompt side K-fold
    over-counts context — which surfaced as the toolbar context ring
    spiking up during a tool-using turn and snapping back down on the
    next plain turn.

    Context-window FILL is a single prompt's size, not a billing sum: the
    final call's prompt already contains the full conversation that carries
    into the next turn. The executor captures each ``message_start``'s
    per-call usage and uses the LAST one for ``context_tokens`` (mirroring
    the openai-agents executor's ``raw_responses[-1]`` choice), while still
    reporting the cumulative totals for billing.

    Regression guard: before this fix ``context_tokens`` was
    ``cumulative_input + cumulative_cache_read`` (= 21100 here), which is
    the K-fold over-count. The correct value is the last call's prompt
    (= 10800).
    """
    from unittest.mock import patch

    from claude_agent_sdk.types import (
        ClaudeAgentOptions as SDKClaudeAgentOptions,
    )
    from claude_agent_sdk.types import ResultMessage as SDKResultMessage
    from claude_agent_sdk.types import StreamEvent as SDKStreamEvent

    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

    class _Sentinel:
        pass

    def _message_start(usage: dict[str, int]) -> SDKStreamEvent:
        # One ``message_start`` opens one API call and carries that call's
        # prompt-side usage. Two of them model a two-call (one tool-loop)
        # turn; the executor must keep only the last for context.
        return SDKStreamEvent(
            uuid="u",
            session_id="s1",
            event={"type": "message_start", "message": {"usage": usage}},
        )

    # Call 1 (cold-ish): non-cached input 300 + 10000 read from cache.
    first_call = _message_start(
        {"input_tokens": 300, "cache_read_input_tokens": 10000, "cache_creation_input_tokens": 0}
    )
    # Call 2 (final, after the tool result is appended): the conversation
    # grew, so this prompt is the real context = 500 + 10300 = 10800.
    second_call = _message_start(
        {"input_tokens": 500, "cache_read_input_tokens": 10300, "cache_creation_input_tokens": 0}
    )
    # ResultMessage carries the CUMULATIVE sum across both calls:
    # input 800, cache_read 20300, output 250.
    cumulative_result = SDKResultMessage(
        subtype="result",
        session_id="s1",
        result="done",
        total_cost_usd=0.0,
        duration_ms=100,
        duration_api_ms=80,
        is_error=False,
        num_turns=2,
        usage={
            "input_tokens": 800,
            "output_tokens": 250,
            "cache_read_input_tokens": 20300,
            "cache_creation_input_tokens": 0,
        },
    )

    class _FakeSDK:
        AssistantMessage = _Sentinel
        UserMessage = _Sentinel
        SystemMessage = _Sentinel
        StreamEvent = SDKStreamEvent
        ResultMessage = SDKResultMessage
        ClaudeAgentOptions = SDKClaudeAgentOptions
        messages = [first_call, second_call, cumulative_result]

        class ClaudeSDKClient:
            def __init__(self, options):
                self.options = options

            async def connect(self):
                return None

            async def query(self, prompt, session_id="default"):
                return None

            async def receive_response(self):
                for message in _FakeSDK.messages:
                    yield message

            async def disconnect(self):
                return None

    executor = ClaudeSDKExecutor()
    with patch("omnigent.inner.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
        events = [
            e
            async for e in executor.run_turn(
                [{"role": "user", "content": "hi"}],
                [],
                "",
            )
        ]

    turn = next(e for e in events if isinstance(e, TurnComplete))
    assert turn.usage is not None, "TurnComplete.usage is None on a multi-call turn."

    # context_tokens = LAST call's prompt (500 + 0 + 10300), NOT the
    # cumulative sum (800 + 20300 = 21100). This is the whole point of the
    # fix: a tool-using turn must not inflate the context ring.
    assert turn.usage["context_tokens"] == 10800, (
        f"context_tokens {turn.usage.get('context_tokens')} != 10800 — must be the "
        "LAST message_start call's prompt (input+cache_creation+cache_read), not the "
        "cumulative ResultMessage sum (21100). Using the cumulative value over-counts "
        "context K-fold on a K-call turn."
    )
    assert turn.usage["context_tokens"] != 21100, (
        "context_tokens equals the cumulative over-count (21100) — the executor is "
        "summing prompt usage across tool-loop iterations instead of taking the last call."
    )

    # Billing totals still come from the cumulative ResultMessage.usage so
    # cost is charged for every call, not just the last one.
    assert turn.usage["input_tokens"] == 800, (
        f"input_tokens {turn.usage.get('input_tokens')} != 800 — billing input must "
        "stay the cumulative cross-call sum from ResultMessage.usage."
    )
    assert turn.usage["output_tokens"] == 250, (
        f"output_tokens {turn.usage.get('output_tokens')} != 250 — billing output must "
        "stay the cumulative sum from ResultMessage.usage."
    )
    assert turn.usage["total_tokens"] == 1050, (
        f"total_tokens {turn.usage.get('total_tokens')} != 1050 (800+250) — total must "
        "be the cumulative input + output for billing."
    )


@pytest.mark.asyncio
async def test_assistant_message_model_flows_to_turn_usage() -> None:
    """The SDK's assistant-message model is forwarded in ``TurnComplete.usage``.

    When the agent spec pins no model (a delegating supervisor on the gateway),
    the resolved config model is ``None`` — but the Claude SDK reports the
    concrete model on each ``AssistantMessage``. The executor captures it and
    forwards it as ``usage["model"]`` so the server cost path can price the
    turn. A regression here leaves cost unpriced ("—") for every unpinned
    claude-sdk agent (the debbie/debby supervisors).
    """
    from unittest.mock import patch

    from claude_agent_sdk.types import (
        ClaudeAgentOptions as SDKClaudeAgentOptions,
    )
    from claude_agent_sdk.types import ResultMessage as SDKResultMessage
    from claude_agent_sdk.types import StreamEvent as SDKStreamEvent

    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

    class _AsstMsg:
        """Minimal stand-in for the SDK AssistantMessage (carries model)."""

        def __init__(self, content: list[object], model: str) -> None:
            self.content = content
            self.model = model

    assistant = _AsstMsg(content=[], model="claude-opus-4-8")
    sdk_result = SDKResultMessage(
        subtype="result",
        session_id="s1",
        result="hi",
        total_cost_usd=0.0,
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        usage={"input_tokens": 100, "output_tokens": 50},
    )

    class _FakeSDK:
        AssistantMessage = _AsstMsg
        UserMessage = type("_U", (), {})
        SystemMessage = type("_S", (), {})
        StreamEvent = SDKStreamEvent
        ResultMessage = SDKResultMessage
        ClaudeAgentOptions = SDKClaudeAgentOptions
        messages = [assistant, sdk_result]

        class ClaudeSDKClient:
            def __init__(self, options: object) -> None:
                self.options = options

            async def connect(self) -> None:
                return None

            async def query(self, prompt: object, session_id: str = "default") -> None:
                return None

            async def receive_response(self):  # type: ignore[no-untyped-def]
                for message in _FakeSDK.messages:
                    yield message

            async def disconnect(self) -> None:
                return None

    executor = ClaudeSDKExecutor()
    with patch("omnigent.inner.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
        events = [
            e
            async for e in executor.run_turn(
                [{"role": "user", "content": "hi"}],
                [],
                "",
            )
        ]

    turn = next(e for e in events if isinstance(e, TurnComplete))
    assert turn.usage is not None
    # No model is configured (no cfg.model / override), so a non-None model here
    # can only be the one captured from the AssistantMessage.
    assert turn.usage["model"] == "claude-opus-4-8", (
        f"usage model {turn.usage.get('model')!r} != 'claude-opus-4-8' — the "
        "assistant-message model was not captured/forwarded."
    )


@pytest.mark.asyncio
async def test_result_message_usage_none_yields_turn_complete_without_usage() -> None:
    """When ``ResultMessage.usage`` is ``None``, ``TurnComplete.usage`` is ``None``.

    The executor must not synthesize fake usage when the SDK doesn't report it
    (e.g. Databricks gateway not returning usage). ``None`` usage causes the
    REPL to fall back to its local ``count_tokens()`` estimate, which is correct
    behaviour — no invented numbers should appear in the context ring.

    Regression guard: if the usage-capture code unconditionally sets
    ``turn_usage = {}`` (or any non-None dict), the REPL would display
    ``0 / 200k (0%)`` instead of triggering the local estimate.
    """
    from unittest.mock import patch

    from claude_agent_sdk.types import (
        ClaudeAgentOptions as SDKClaudeAgentOptions,
    )
    from claude_agent_sdk.types import ResultMessage as SDKResultMessage
    from claude_agent_sdk.types import StreamEvent as SDKStreamEvent

    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

    class _Sentinel:
        pass

    sdk_result = SDKResultMessage(
        subtype="result",
        session_id="s1",
        result="Hello",
        total_cost_usd=0.0,
        duration_ms=100,
        duration_api_ms=80,
        is_error=False,
        num_turns=1,
        usage=None,  # SDK didn't report usage
    )

    class _FakeSDK:
        AssistantMessage = _Sentinel
        UserMessage = _Sentinel
        SystemMessage = _Sentinel
        StreamEvent = SDKStreamEvent
        ResultMessage = SDKResultMessage
        ClaudeAgentOptions = SDKClaudeAgentOptions
        messages = [sdk_result]

        class ClaudeSDKClient:
            def __init__(self, options):
                self.options = options

            async def connect(self):
                return None

            async def query(self, prompt, session_id="default"):
                return None

            async def receive_response(self):
                for message in _FakeSDK.messages:
                    yield message

            async def disconnect(self):
                return None

    executor = ClaudeSDKExecutor()
    with patch("omnigent.inner.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
        events = [
            e
            async for e in executor.run_turn(
                [{"role": "user", "content": "hi"}],
                [],
                "",
            )
        ]

    turn = next(e for e in events if isinstance(e, TurnComplete))

    # usage=None triggers the REPL's local count_tokens() fallback,
    # which is the right behaviour when the SDK doesn't report usage.
    # A non-None empty dict {} would short-circuit the fallback with 0 tokens.
    assert turn.usage is None, (
        f"TurnComplete.usage should be None when ResultMessage.usage is None, "
        f"got {turn.usage!r}. An empty dict would display 0 tokens in the ring."
    )


# ---------------------------------------------------------------------------
# Tests: pre-execution TOOL_CALL policy gate (can_use_tool)
# ---------------------------------------------------------------------------


class TestToolCallPolicyGate(unittest.TestCase):
    """The ``can_use_tool`` gate that enforces TOOL_CALL policy on
    connector-native MCP tools (``mcp__github__*`` etc.) before they run.
    """

    def _make_executor(self, permission_mode="bypassPermissions"):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        return ClaudeSDKExecutor(permission_mode=permission_mode)

    @staticmethod
    def _verdict(action, reason=None):
        from omnigent.runtime.harnesses._scaffold import PolicyVerdictPayload

        return PolicyVerdictPayload(action=action, reason=reason)

    @staticmethod
    def _perm_ctx():
        return SimpleNamespace(tool_use_id="tu_1", agent_id=None, suggestions=[])

    def test_connector_native_tool_triggers_tool_call_eval(self):
        """A connector-native tool name drives a PHASE_TOOL_CALL evaluation
        with the SDK-native tool name and arguments."""
        from claude_agent_sdk import PermissionResultAllow

        async def _t():
            executor = self._make_executor()
            evaluator = AsyncMock(return_value=self._verdict("POLICY_ACTION_ALLOW"))
            executor._policy_evaluator = evaluator

            result = await executor._can_use_tool_gate(
                "mcp__github__issue_write",
                {"title": "bug", "body": "x"},
                self._perm_ctx(),
            )

            evaluator.assert_awaited_once()
            phase, data = evaluator.await_args.args
            self.assertEqual(phase, "PHASE_TOOL_CALL")
            self.assertEqual(
                data,
                {
                    "name": "mcp__github__issue_write",
                    "arguments": {"title": "bug", "body": "x"},
                },
            )
            self.assertIsInstance(result, PermissionResultAllow)

        _run(_t())

    def test_deny_verdict_blocks_execution(self):
        """A DENY verdict returns PermissionResultDeny carrying the
        policy's reason and never reaches the elicitation handler."""
        from claude_agent_sdk import PermissionResultDeny

        async def _t():
            executor = self._make_executor(permission_mode="default")
            executor._policy_evaluator = AsyncMock(
                return_value=self._verdict("POLICY_ACTION_DENY", reason="no writes to github")
            )
            elicit = AsyncMock(return_value=True)
            executor._elicitation_handler = elicit

            result = await executor._can_use_tool_gate(
                "mcp__github__issue_write",
                {"title": "bug"},
                self._perm_ctx(),
            )

            self.assertIsInstance(result, PermissionResultDeny)
            self.assertEqual(result.message, "no writes to github")
            # DENY short-circuits — no human prompt.
            elicit.assert_not_awaited()

        _run(_t())

    def test_ask_verdict_prompts_even_under_bypass(self):
        """A raw ASK verdict is supported by routing to Omnigent
        elicitation, even under bypassPermissions."""
        from claude_agent_sdk import PermissionResultAllow

        async def _t():
            executor = self._make_executor(permission_mode="bypassPermissions")
            executor._policy_evaluator = AsyncMock(
                return_value=self._verdict("POLICY_ACTION_ASK", reason="approval required")
            )
            elicit = AsyncMock(return_value=True)
            executor._elicitation_handler = elicit

            result = await executor._can_use_tool_gate(
                "mcp__github__issue_write",
                {"title": "bug"},
                self._perm_ctx(),
            )

            self.assertIsInstance(result, PermissionResultAllow)
            elicit.assert_awaited_once_with("mcp__github__issue_write", {"title": "bug"})

        _run(_t())

    def test_ask_verdict_denies_when_user_declines(self):
        """A declined raw ASK blocks execution with the policy reason."""
        from claude_agent_sdk import PermissionResultDeny

        async def _t():
            executor = self._make_executor(permission_mode="bypassPermissions")
            executor._policy_evaluator = AsyncMock(
                return_value=self._verdict("POLICY_ACTION_ASK", reason="approval required")
            )
            executor._elicitation_handler = AsyncMock(return_value=False)

            result = await executor._can_use_tool_gate(
                "mcp__github__issue_write",
                {"title": "bug"},
                self._perm_ctx(),
            )

            self.assertIsInstance(result, PermissionResultDeny)
            self.assertEqual(result.message, "approval required")

        _run(_t())

    def test_ask_verdict_without_elicitation_handler_fails_closed(self):
        """If raw ASK reaches the callback but no handler is available,
        the tool must not run."""
        from claude_agent_sdk import PermissionResultDeny

        async def _t():
            executor = self._make_executor(permission_mode="bypassPermissions")
            executor._policy_evaluator = AsyncMock(
                return_value=self._verdict("POLICY_ACTION_ASK", reason="approval required")
            )

            result = await executor._can_use_tool_gate(
                "mcp__github__issue_write",
                {"title": "bug"},
                self._perm_ctx(),
            )

            self.assertIsInstance(result, PermissionResultDeny)
            self.assertEqual(result.message, "approval required")

        _run(_t())

    def test_unspecified_verdict_falls_through(self):
        """UNSPECIFIED is a proto no-op verdict and should behave like no match."""
        from claude_agent_sdk import PermissionResultAllow

        async def _t():
            executor = self._make_executor(permission_mode="bypassPermissions")
            executor._policy_evaluator = AsyncMock(
                return_value=self._verdict("POLICY_ACTION_UNSPECIFIED")
            )

            result = await executor._can_use_tool_gate(
                "mcp__github__issue_read",
                {"number": 1},
                self._perm_ctx(),
            )

            self.assertIsInstance(result, PermissionResultAllow)

        _run(_t())

    def test_unexpected_verdict_fails_closed(self):
        """Unknown policy actions should not silently allow a tool call."""
        from claude_agent_sdk import PermissionResultDeny

        async def _t():
            executor = self._make_executor(permission_mode="bypassPermissions")
            executor._policy_evaluator = AsyncMock(return_value=self._verdict("BOGUS"))

            result = await executor._can_use_tool_gate(
                "mcp__github__issue_write",
                {"title": "bug"},
                self._perm_ctx(),
            )

            self.assertIsInstance(result, PermissionResultDeny)
            self.assertIn("Unexpected Omnigent TOOL_CALL policy verdict", result.message)

        _run(_t())

    def test_allow_verdict_no_human_prompt_under_bypass(self):
        """ALLOW under bypassPermissions allows the call with no human
        prompt, preserving bypass ergonomics."""
        from claude_agent_sdk import PermissionResultAllow

        async def _t():
            executor = self._make_executor(permission_mode="bypassPermissions")
            executor._policy_evaluator = AsyncMock(
                return_value=self._verdict("POLICY_ACTION_ALLOW")
            )
            elicit = AsyncMock(return_value=True)
            executor._elicitation_handler = elicit

            result = await executor._can_use_tool_gate(
                "mcp__github__issue_read",
                {"number": 1},
                self._perm_ctx(),
            )

            self.assertIsInstance(result, PermissionResultAllow)
            elicit.assert_not_awaited()

        _run(_t())

    def test_no_policy_evaluator_no_match_allows_silently(self):
        """With no policy evaluator wired (default ALLOW), the gate allows
        with no evaluation and no human prompt under bypassPermissions."""
        from claude_agent_sdk import PermissionResultAllow

        async def _t():
            executor = self._make_executor(permission_mode="bypassPermissions")
            # No _policy_evaluator set, no elicitation handler.
            result = await executor._can_use_tool_gate(
                "mcp__atlassian__createJiraIssue",
                {"summary": "x"},
                self._perm_ctx(),
            )
            self.assertIsInstance(result, PermissionResultAllow)

        _run(_t())

    def test_omnigent_own_tool_skips_evaluation(self):
        """``mcp__omnigent__*`` tools are already TOOL_CALL-gated server-side
        via the dispatch bridge / ProxyMcpManager, so the gate must NOT
        evaluate them again (avoids double-evaluation)."""
        from claude_agent_sdk import PermissionResultAllow

        async def _t():
            executor = self._make_executor(permission_mode="bypassPermissions")
            evaluator = AsyncMock(return_value=self._verdict("POLICY_ACTION_ALLOW"))
            executor._policy_evaluator = evaluator

            result = await executor._can_use_tool_gate(
                "mcp__omnigent__sys_os_read",
                {"path": "/tmp/x"},
                self._perm_ctx(),
            )

            self.assertIsInstance(result, PermissionResultAllow)
            evaluator.assert_not_awaited()

        _run(_t())

    def test_non_bypass_runs_elicitation_after_policy_allow(self):
        """In a non-bypass mode, a policy ALLOW falls through to the
        human-consent elicitation gate (pre-existing behavior)."""
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        async def _t():
            executor = self._make_executor(permission_mode="default")
            executor._policy_evaluator = AsyncMock(
                return_value=self._verdict("POLICY_ACTION_ALLOW")
            )

            # Human approves.
            executor._elicitation_handler = AsyncMock(return_value=True)
            approved = await executor._can_use_tool_gate(
                "mcp__github__issue_read", {}, self._perm_ctx()
            )
            self.assertIsInstance(approved, PermissionResultAllow)

            # Human denies.
            executor._elicitation_handler = AsyncMock(return_value=False)
            denied = await executor._can_use_tool_gate(
                "mcp__github__issue_read", {}, self._perm_ctx()
            )
            self.assertIsInstance(denied, PermissionResultDeny)

        _run(_t())

    def test_gate_installed_under_bypass_permissions(self):
        """run_turn installs the can_use_tool gate even under
        bypassPermissions when a policy evaluator is wired — the
        regression this feature fixes."""
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        async def _t():
            executor = ClaudeSDKExecutor(permission_mode="bypassPermissions")
            executor._policy_evaluator = AsyncMock(
                return_value=self._verdict("POLICY_ACTION_ALLOW")
            )

            captured = {}

            async def fake_get_or_create_client(sdk, *, session_key, options, model):
                captured["can_use_tool"] = options.can_use_tool
                raise RuntimeError("stop after options build")

            with patch.object(
                executor,
                "_get_or_create_client",
                side_effect=fake_get_or_create_client,
            ):
                with self.assertRaises(RuntimeError):
                    async for _ in executor.run_turn([{"role": "user", "content": "hi"}], [], ""):
                        pass

            # Bound-method identity differs per access; compare equality.
            self.assertEqual(captured["can_use_tool"], executor._can_use_tool_gate)
            self.assertIsNotNone(captured["can_use_tool"])

        _run(_t())

    def test_gate_not_installed_without_evaluator_or_handler(self):
        """With neither a policy evaluator nor an elicitation handler, no
        can_use_tool callback is installed (unchanged baseline)."""
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        async def _t():
            executor = ClaudeSDKExecutor(permission_mode="bypassPermissions")
            captured = {}

            async def fake_get_or_create_client(sdk, *, session_key, options, model):
                captured["can_use_tool"] = options.can_use_tool
                raise RuntimeError("stop after options build")

            with patch.object(
                executor,
                "_get_or_create_client",
                side_effect=fake_get_or_create_client,
            ):
                with self.assertRaises(RuntimeError):
                    async for _ in executor.run_turn([{"role": "user", "content": "hi"}], [], ""):
                        pass

            self.assertIsNone(captured["can_use_tool"])

        _run(_t())


# ---------------------------------------------------------------------------
# Tests: Compaction detection via PreCompact hook
# ---------------------------------------------------------------------------


def test_precompact_hook_emits_compaction_complete_with_session_messages() -> None:
    """When PreCompact fires and a ResultMessage carries a session_id,
    CompactionComplete is emitted with compacted_messages read from
    the CLI's session transcript."""
    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
    from omnigent.inner.executor import CompactionComplete

    class _ResultMessage:
        def __init__(self, session_id, result):
            self.session_id = session_id
            self.result = result
            self.content = []
            self.model = "claude-test"
            self.usage = type(
                "U",
                (),
                {
                    "input_tokens": 500,
                    "output_tokens": 100,
                },
            )()

    class _SystemMessage:
        def __init__(self, subtype, data, hook_event_name=None):
            self.subtype = subtype
            self.data = data
            self.hook_event_name = hook_event_name

    class _HookEventMessage(_SystemMessage):
        pass

    class _FakeSessionMessage:
        def __init__(self, type, message):
            self.type = type
            self.message = message

    class _FakeSDK:
        AssistantMessage = type("AssistantMessage", (), {})
        UserMessage = type("UserMessage", (), {})
        SystemMessage = _SystemMessage
        ResultMessage = _ResultMessage
        StreamEvent = type("StreamEvent", (), {})
        ClaudeAgentOptions = type(
            "ClaudeAgentOptions",
            (),
            {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
        )
        messages: list = []

        class ClaudeSDKClient:
            def __init__(self, options):
                self.options = options

            async def connect(self):
                return

            async def query(self, prompt, session_id="default"):
                _FakeSDK.messages = [
                    _HookEventMessage(
                        subtype="hook_started",
                        data={"hook_event": "PreCompact"},
                        hook_event_name="PreCompact",
                    ),
                    _ResultMessage("claude-uuid-123", "compacted result"),
                ]

            async def receive_response(self):
                for message in _FakeSDK.messages:
                    yield message

            async def disconnect(self):
                return None

    fake_session_msgs = [
        _FakeSessionMessage("user", {"content": [{"type": "text", "text": "hi"}]}),
        _FakeSessionMessage("assistant", {"content": [{"type": "text", "text": "compacted"}]}),
    ]

    async def _t():
        executor = ClaudeSDKExecutor()
        with (
            patch(
                "omnigent.inner.claude_sdk_executor._ensure_sdk",
                return_value=_FakeSDK,
            ),
            patch(
                "claude_agent_sdk.get_session_messages",
                return_value=fake_session_msgs,
            ) as mock_get_msgs,
        ):
            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hi", "session_id": "s1"}],
                    [],
                    "",
                )
            ]

        compaction_events = [e for e in events if isinstance(e, CompactionComplete)]
        assert len(compaction_events) == 1
        ce = compaction_events[0]
        assert ce.compacted_messages is not None
        assert len(ce.compacted_messages) == 2
        assert ce.compacted_messages[0]["role"] == "user"
        assert ce.compacted_messages[1]["role"] == "assistant"
        mock_get_msgs.assert_called_once_with("claude-uuid-123", directory=None)
        # CompactionComplete before TurnComplete
        turn_completes = [e for e in events if isinstance(e, TurnComplete)]
        assert len(turn_completes) == 1
        assert events.index(compaction_events[0]) < events.index(turn_completes[0])

    _run(_t())


def test_no_precompact_no_compaction_event() -> None:
    """When no PreCompact hook fires, no CompactionComplete is yielded."""
    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
    from omnigent.inner.executor import CompactionComplete

    class _ResultMessage:
        def __init__(self, session_id, result):
            self.session_id = session_id
            self.result = result
            self.content = []
            self.model = "claude-test"
            self.usage = type(
                "U",
                (),
                {
                    "input_tokens": 500,
                    "output_tokens": 100,
                },
            )()

    class _FakeSDK:
        AssistantMessage = type("AssistantMessage", (), {})
        UserMessage = type("UserMessage", (), {})
        SystemMessage = type("SystemMessage", (), {})
        ResultMessage = _ResultMessage
        StreamEvent = type("StreamEvent", (), {})
        ClaudeAgentOptions = type(
            "ClaudeAgentOptions",
            (),
            {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
        )
        messages: list = []

        class ClaudeSDKClient:
            def __init__(self, options):
                self.options = options

            async def connect(self):
                return

            async def query(self, prompt, session_id="default"):
                _FakeSDK.messages = [_ResultMessage(session_id, "normal result")]

            async def receive_response(self):
                for message in _FakeSDK.messages:
                    yield message

            async def disconnect(self):
                return None

    async def _t():
        executor = ClaudeSDKExecutor()
        with patch("omnigent.inner.claude_sdk_executor._ensure_sdk", return_value=_FakeSDK):
            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hi", "session_id": "s1"}],
                    [],
                    "",
                )
            ]

        compaction_events = [e for e in events if isinstance(e, CompactionComplete)]
        assert len(compaction_events) == 0

    _run(_t())


class TestModelResolution(unittest.TestCase):
    """``_resolve_turn_model`` precedence + fail-closed (issue #1128)."""

    def _databricks_executor(self, **kwargs):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
        from omnigent.inner.databricks_executor import DatabricksCredentials

        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "omnigent.inner.databricks_executor._read_databrickscfg",
                return_value=DatabricksCredentials(
                    host="https://example.cloud.databricks.com",
                    token="dapi_test_token",
                ),
            ),
        ):
            return ClaudeSDKExecutor(gateway=True, **kwargs)

    def test_cfg_model_wins(self):
        executor = self._databricks_executor(model="databricks-claude-opus-4-8")
        self.assertTrue(executor._gateway_uses_databricks_profile)
        self.assertEqual(
            executor._resolve_turn_model("databricks-claude-sonnet-4-6"),
            "databricks-claude-sonnet-4-6",
        )

    def test_construction_model_used_when_no_cfg_model(self):
        executor = self._databricks_executor(
            model="databricks-claude-sonnet-4-6", model_source="session-override"
        )
        self.assertEqual(executor._resolve_turn_model(None), "databricks-claude-sonnet-4-6")

    def test_unconfigured_keeps_opus_default(self):
        executor = self._databricks_executor(model=None, model_source="unconfigured-default")
        self.assertEqual(executor._resolve_turn_model(None), "databricks-claude-opus-4-8")

    def test_absent_source_keeps_default_for_backcompat(self):
        executor = self._databricks_executor(model=None)
        self.assertEqual(executor._resolve_turn_model(None), "databricks-claude-opus-4-8")

    def test_lost_selection_fails_closed(self):
        executor = self._databricks_executor(
            model=None,
            model_source="session-override",
            requested_model="databricks-claude-sonnet-4-6",
        )
        with self.assertRaises(ValueError) as ctx:
            executor._resolve_turn_model(None)
        self.assertIn("databricks-claude-sonnet-4-6", str(ctx.exception))
        self.assertNotIn(
            "Refusing to silently fall back to 'databricks-claude-sonnet",
            str(ctx.exception),
        )
        self.assertIn("databricks-claude-opus-4-8", str(ctx.exception))

    def test_non_databricks_gateway_returns_none(self):
        from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

        with patch.dict("os.environ", {}, clear=True):
            executor = ClaudeSDKExecutor(
                gateway=True,
                base_url_override="https://mock-llm.example/v1",
                gateway_auth_command="printf token",
            )
        self.assertFalse(executor._gateway_uses_databricks_profile)
        self.assertIsNone(executor._resolve_turn_model(None))


if __name__ == "__main__":
    unittest.main()
