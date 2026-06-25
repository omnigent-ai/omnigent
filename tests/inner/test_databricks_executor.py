"""Tests for DatabricksExecutor with a mock OpenAI client."""

import asyncio
import json
import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import databricks.sdk.config as _sdk_config_mod

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omnigent.inner.databricks_executor import (
    DatabricksExecutor,
    _convert_messages,
    _convert_tools_to_openai,
)
from omnigent.inner.executor import (
    ExecutorConfig,
    ExecutorError,
    TextChunk,
    ToolCallRequest,
    TurnComplete,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()


# ---------------------------------------------------------------------------
# Fake streaming response objects (mimicking OpenAI streaming types)
# ---------------------------------------------------------------------------


@dataclass
class FakeFunctionDelta:
    name: str | None = None
    arguments: str | None = None


@dataclass
class FakeToolCallDelta:
    index: int = 0
    function: FakeFunctionDelta | None = None


@dataclass
class FakeDelta:
    """
    Minimal stream-delta test double for DatabricksExecutor.

    :param content: Raw ``delta.content`` payload, e.g. ``"hello"`` or
        ``[{"type": "reasoning", "summary": [...]}]``.
    :param tool_calls: Optional streamed tool-call deltas.
    """

    content: Any = None
    tool_calls: list[FakeToolCallDelta] | None = None


@dataclass
class FakeStreamChoice:
    delta: FakeDelta = field(default_factory=FakeDelta)
    index: int = 0
    finish_reason: str | None = None


@dataclass
class FakeStreamChunk:
    choices: list[FakeStreamChoice] = field(default_factory=list)


def _make_text_stream(text: str) -> list[FakeStreamChunk]:
    """Create a stream that yields text content then stops."""
    chunks = []
    # Yield text in a single chunk for simplicity
    if text:
        chunks.append(FakeStreamChunk(choices=[FakeStreamChoice(delta=FakeDelta(content=text))]))
    # Final chunk with finish_reason=stop
    chunks.append(FakeStreamChunk(choices=[FakeStreamChoice(finish_reason="stop")]))
    return chunks


def _make_tool_call_stream(
    tool_calls: list[tuple[str, str]],
    text: str | None = None,
) -> list[FakeStreamChunk]:
    """Create a stream that yields tool calls.

    Args:
        tool_calls: list of (name, arguments_json) tuples
        text: optional text content to include before tool calls
    """
    chunks = []
    if text:
        chunks.append(FakeStreamChunk(choices=[FakeStreamChoice(delta=FakeDelta(content=text))]))
    for idx, (name, args) in enumerate(tool_calls):
        chunks.append(
            FakeStreamChunk(
                choices=[
                    FakeStreamChoice(
                        delta=FakeDelta(
                            tool_calls=[
                                FakeToolCallDelta(
                                    index=idx,
                                    function=FakeFunctionDelta(name=name, arguments=args),
                                )
                            ]
                        )
                    )
                ]
            )
        )
    # Final chunk with finish_reason=tool_calls
    chunks.append(FakeStreamChunk(choices=[FakeStreamChoice(finish_reason="tool_calls")]))
    return chunks


class FakeCompletions:
    """Mimics client.chat.completions."""

    def __init__(self, stream_chunks: list[FakeStreamChunk]):
        self._chunks = stream_chunks
        self.last_kwargs: dict[str, Any] = {}

    def create(self, **kwargs) -> list[FakeStreamChunk]:
        self.last_kwargs = kwargs
        return iter(self._chunks)


class FakeChat:
    def __init__(self, completions: FakeCompletions):
        self.completions = completions


class FakeClient:
    """Mimics the OpenAI client."""

    def __init__(self, stream_chunks: list[FakeStreamChunk]):
        self.chat = FakeChat(FakeCompletions(stream_chunks))


def test_kimi_reasoning_content_blocks_are_not_text_chunks() -> None:
    """
    Kimi streams reasoning summaries as ``delta.content`` block lists before
    the assistant answer; the executor must not hand those lists to
    :class:`TextChunk` or append them to the final assistant response.
    """

    async def _t() -> None:
        """Drive DatabricksExecutor over a Kimi-shaped stream."""
        chunks = [
            FakeStreamChunk(
                choices=[
                    FakeStreamChoice(
                        delta=FakeDelta(
                            content=[
                                {
                                    "type": "reasoning",
                                    "summary": [{"type": "summary_text", "text": "Thinking"}],
                                }
                            ]
                        )
                    )
                ]
            ),
            FakeStreamChunk(choices=[FakeStreamChoice(delta=FakeDelta(content="Hello"))]),
            FakeStreamChunk(choices=[FakeStreamChoice(finish_reason="stop")]),
        ]
        executor = DatabricksExecutor(client=FakeClient(chunks))

        events = [
            e
            async for e in executor.run_turn(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                system_prompt="Be helpful.",
                config=ExecutorConfig(model="databricks-kimi-k2-6"),
            )
        ]

        text_events = [e for e in events if isinstance(e, TextChunk)]
        turn_events = [e for e in events if isinstance(e, TurnComplete)]
        assert [e.text for e in text_events] == ["Hello"]
        assert all(isinstance(e.text, str) for e in text_events)
        assert len(turn_events) == 1
        assert turn_events[0].response == "Hello"

    _run(_t())


def test_text_content_block_lists_are_collapsed_to_text_chunks() -> None:
    """
    Providers may stream assistant-visible text as content block lists; those
    recognized text blocks must still produce normal string
    :class:`TextChunk` events and the correct final response.
    """

    async def _t() -> None:
        """Drive DatabricksExecutor over text content block deltas."""
        chunks = [
            FakeStreamChunk(
                choices=[
                    FakeStreamChoice(
                        delta=FakeDelta(
                            content=[
                                {"type": "text", "text": "Hello"},
                                {"type": "output_text", "text": " world"},
                            ]
                        )
                    )
                ]
            ),
            FakeStreamChunk(choices=[FakeStreamChoice(finish_reason="stop")]),
        ]
        executor = DatabricksExecutor(client=FakeClient(chunks))

        events = [
            e
            async for e in executor.run_turn(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                system_prompt="Be helpful.",
                config=ExecutorConfig(model="block-list-model"),
            )
        ]

        text_events = [e for e in events if isinstance(e, TextChunk)]
        turn_events = [e for e in events if isinstance(e, TurnComplete)]
        assert [e.text for e in text_events] == ["Hello world"]
        assert len(turn_events) == 1
        assert turn_events[0].response == "Hello world"

    _run(_t())


# ---------------------------------------------------------------------------
# Tests: message and tool conversion helpers
# ---------------------------------------------------------------------------


class TestConvertTools(unittest.TestCase):
    def test_basic_tool(self):
        tools = [
            {
                "name": "sql",
                "description": "Run SQL",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            }
        ]
        result = _convert_tools_to_openai(tools)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "function")
        self.assertEqual(result[0]["function"]["name"], "sql")
        self.assertIn("properties", result[0]["function"]["parameters"])

    def test_tool_without_parameters(self):
        tools = [{"name": "ping", "description": "Ping"}]
        result = _convert_tools_to_openai(tools)
        self.assertEqual(result[0]["function"]["parameters"], {"type": "object", "properties": {}})

    def test_preserves_required_args_in_async_tool_schema(self):
        tools = [
            {
                "name": "sys_call_async",
                "description": "Async call",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string"},
                        "args": {
                            "type": "object",
                            "default": {},
                            "properties": {
                                "example_key": {
                                    "anyOf": [{"type": "string"}],
                                },
                            },
                            "additionalProperties": {
                                "anyOf": [{"type": "string"}],
                            },
                        },
                    },
                    "required": ["tool", "args"],
                },
            }
        ]
        result = _convert_tools_to_openai(tools)
        self.assertEqual(
            result[0]["function"]["parameters"]["required"],
            ["tool", "args"],
        )
        self.assertIn(
            "example_key",
            result[0]["function"]["parameters"]["properties"]["args"]["properties"],
        )

    def test_preserves_required_args_in_session_send_schema(self):
        tools = [
            {
                "name": "sys_session_send",
                "description": "Session send",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string"},
                        "session": {"type": "string"},
                        "args": {"type": "object", "additionalProperties": True},
                    },
                    "required": ["tool", "session", "args"],
                },
            }
        ]
        result = _convert_tools_to_openai(tools)
        self.assertEqual(
            result[0]["function"]["parameters"]["required"],
            ["tool", "session", "args"],
        )

    def test_empty_tools(self):
        self.assertEqual(_convert_tools_to_openai([]), [])

    def test_invalid_tool_name_is_normalized_for_provider(self):
        tools = [{"name": "sys_runtime_execute", "description": "Run code"}]
        result = _convert_tools_to_openai(tools)
        self.assertEqual(result[0]["function"]["name"], "sys_runtime_execute")
        self.assertEqual(result[0]["function"]["description"], "Run code")


class TestConvertMessages(unittest.TestCase):
    def test_system_prompt(self):
        result = _convert_messages([], "You are helpful.")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["role"], "system")
        self.assertEqual(result[0]["content"], "You are helpful.")

    def test_user_and_assistant(self):
        msgs = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        result = _convert_messages(msgs, "")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["role"], "user")
        self.assertEqual(result[1]["role"], "assistant")

    def test_tool_call_and_result_pair(self):
        msgs = [
            {"role": "user", "content": "Run a query"},
            {"role": "tool_call", "content": {"tool": "sql", "args": {"q": "SELECT 1"}}},
            {"role": "tool_result", "content": {"rows": [1]}},
        ]
        result = _convert_messages(msgs, "sys")
        # system + user + assistant(tool_calls) + tool
        self.assertEqual(len(result), 4)
        self.assertEqual(result[0]["role"], "system")
        self.assertEqual(result[1]["role"], "user")
        self.assertEqual(result[2]["role"], "assistant")

    def test_invalid_tool_name_is_normalized_in_history_replay(self):
        msgs = [
            {
                "role": "tool_call",
                "content": {"tool": "sys_runtime_execute", "args": {"code": "print(1)"}},
            },
            {"role": "tool_result", "content": {"stdout": "1\n"}},
        ]
        result = _convert_messages(msgs, "")
        self.assertEqual(result[0]["tool_calls"][0]["function"]["name"], "sys_runtime_execute")
        self.assertEqual(result[1]["role"], "tool")

    def test_orphan_tool_result(self):
        msgs = [{"role": "tool_result", "content": "some result"}]
        result = _convert_messages(msgs, "")
        self.assertEqual(len(result), 1)
        self.assertIn("tool result", result[0]["content"])

    def test_tool_call_content_as_string(self):
        """tool_call content might be a JSON string instead of dict."""
        msgs = [
            {
                "role": "tool_call",
                "content": json.dumps({"tool": "search", "args": {"q": "test"}}),
            },
            {"role": "tool_result", "content": "found it"},
        ]
        result = _convert_messages(msgs, "")
        self.assertEqual(result[0]["tool_calls"][0]["function"]["name"], "search")


# ---------------------------------------------------------------------------
# Tests: DatabricksExecutor with fake streaming client
# ---------------------------------------------------------------------------


class TestDatabricksExecutorTextResponse(unittest.TestCase):
    def test_simple_text_response(self):
        async def _t():
            chunks = _make_text_stream("Hello world!")
            executor = DatabricksExecutor(client=FakeClient(chunks))

            events = [
                e
                async for e in executor.run_turn(
                    messages=[{"role": "user", "content": "Hi"}],
                    tools=[],
                    system_prompt="Be nice.",
                    config=ExecutorConfig(model="test-model"),
                )
            ]

            # TextChunk + TurnComplete
            text_events = [e for e in events if isinstance(e, TextChunk)]
            turn_events = [e for e in events if isinstance(e, TurnComplete)]
            self.assertEqual(len(text_events), 1)
            self.assertEqual(text_events[0].text, "Hello world!")
            self.assertEqual(len(turn_events), 1)
            self.assertEqual(turn_events[0].response, "Hello world!")

        _run(_t())

    def test_empty_content(self):
        async def _t():
            chunks = _make_text_stream("")
            executor = DatabricksExecutor(client=FakeClient(chunks))
            events = [e async for e in executor.run_turn([], [], "")]
            turn_events = [e for e in events if isinstance(e, TurnComplete)]
            self.assertEqual(len(turn_events), 1)
            self.assertEqual(turn_events[0].response, "")

        _run(_t())


class TestDatabricksExecutorToolCalls(unittest.TestCase):
    def test_single_tool_call(self):
        async def _t():
            chunks = _make_tool_call_stream([("sql_query", '{"query": "SELECT 1"}')])
            executor = DatabricksExecutor(client=FakeClient(chunks))
            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "query"}],
                    [{"name": "sql_query", "description": "Run SQL"}],
                    "sys",
                )
            ]
            tool_events = [e for e in events if isinstance(e, ToolCallRequest)]
            self.assertEqual(len(tool_events), 1)
            self.assertEqual(tool_events[0].name, "sql_query")
            self.assertEqual(tool_events[0].args["query"], "SELECT 1")

        _run(_t())

    def test_multiple_tool_calls(self):
        async def _t():
            chunks = _make_tool_call_stream(
                [
                    ("tool_a", '{"x": 1}'),
                    ("tool_b", '{"y": 2}'),
                ]
            )
            executor = DatabricksExecutor(client=FakeClient(chunks))
            events = [e async for e in executor.run_turn([], [], "")]
            tool_events = [e for e in events if isinstance(e, ToolCallRequest)]
            self.assertEqual(len(tool_events), 2)
            self.assertEqual(tool_events[0].name, "tool_a")
            self.assertEqual(tool_events[1].name, "tool_b")

        _run(_t())

    def test_tool_call_with_text(self):
        """Model returns both text and tool calls."""

        async def _t():
            chunks = _make_tool_call_stream(
                [("search", "{}")],
                text="Let me search for that.",
            )
            executor = DatabricksExecutor(client=FakeClient(chunks))
            events = [e async for e in executor.run_turn([], [], "")]
            names = [type(e).__name__ for e in events]
            self.assertIn("ToolCallRequest", names)
            self.assertIn("TextChunk", names)
            self.assertNotIn("TurnComplete", names)

        _run(_t())

    def test_malformed_arguments(self):
        """If arguments aren't valid JSON, put them in a 'raw' key."""

        async def _t():
            chunks = _make_tool_call_stream([("t", "not json")])
            executor = DatabricksExecutor(client=FakeClient(chunks))
            events = [e async for e in executor.run_turn([], [], "")]
            tool_events = [e for e in events if isinstance(e, ToolCallRequest)]
            self.assertEqual(len(tool_events), 1)
            self.assertEqual(tool_events[0].args["raw"], "not json")

        _run(_t())


class TestDatabricksExecutorErrors(unittest.TestCase):
    def test_empty_stream(self):
        """Stream with no chunks (no finish_reason, no content, no tool calls) is
        a truncated turn that died mid-stream, so it surfaces an ExecutorError
        rather than a silent empty TurnComplete (#1118)."""

        async def _t():
            executor = DatabricksExecutor(client=FakeClient([]))
            events = [e async for e in executor.run_turn([], [], "")]
            self.assertEqual(len(events), 1)
            self.assertIsInstance(events[0], ExecutorError)
            self.assertEqual(events[0].message, "Stream ended without finish_reason")

        _run(_t())

    def test_api_exception(self):
        async def _t():
            class ExplodingClient:
                class chat:
                    class completions:
                        @staticmethod
                        def create(**kwargs):
                            raise RuntimeError("API down")

            executor = DatabricksExecutor(client=ExplodingClient())
            events = [e async for e in executor.run_turn([], [], "")]
            self.assertEqual(len(events), 1)
            self.assertIsInstance(events[0], ExecutorError)
            self.assertIn("API down", events[0].message)

        _run(_t())


class TestDatabricksExecutorConfig(unittest.TestCase):
    def test_passes_model_and_params(self):
        """Verify the executor passes model, temperature, max_tokens to the API."""

        async def _t():
            chunks = _make_text_stream("ok")
            client = FakeClient(chunks)
            executor = DatabricksExecutor(client=client)

            config = ExecutorConfig(
                model="claude-sonnet-4",
                temperature=0.7,
                max_tokens=2048,
            )
            [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "Hi"}],
                    [],
                    "system prompt",
                    config=config,
                )
            ]

            kwargs = client.chat.completions.last_kwargs
            self.assertEqual(kwargs["model"], "claude-sonnet-4")
            self.assertEqual(kwargs["temperature"], 0.7)
            self.assertEqual(kwargs["max_tokens"], 2048)
            self.assertTrue(kwargs["stream"])

        _run(_t())

    def test_default_model(self):
        """When no model is specified, falls back to databricks-claude-sonnet-4-6."""

        async def _t():
            chunks = _make_text_stream("ok")
            client = FakeClient(chunks)
            executor = DatabricksExecutor(client=client)

            [e async for e in executor.run_turn([], [], "", config=ExecutorConfig())]
            self.assertEqual(
                client.chat.completions.last_kwargs["model"],
                "databricks-claude-sonnet-4-6",
            )

        _run(_t())

    def test_tools_passed_in_openai_format(self):
        async def _t():
            chunks = _make_text_stream("ok")
            client = FakeClient(chunks)
            executor = DatabricksExecutor(client=client)

            tools = [
                {
                    "name": "sql",
                    "description": "Run SQL",
                    "parameters": {"type": "object", "properties": {}},
                }
            ]
            [e async for e in executor.run_turn([], tools, "")]

            passed_tools = client.chat.completions.last_kwargs["tools"]
            self.assertEqual(len(passed_tools), 1)
            self.assertEqual(passed_tools[0]["type"], "function")
            self.assertEqual(passed_tools[0]["function"]["name"], "sql")

        _run(_t())


class TestDatabricksExecutorMultiTurn(unittest.TestCase):
    """Test a realistic multi-turn scenario: user asks -> tool call -> tool result -> response."""

    def test_tool_call_then_response(self):
        async def _t():
            # Turn 1: model wants to call a tool
            chunks1 = _make_tool_call_stream([("search", '{"q": "test"}')])
            client1 = FakeClient(chunks1)
            executor = DatabricksExecutor(client=client1)

            events1 = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "search for test"}],
                    [{"name": "search", "description": "Search"}],
                    "sys",
                )
            ]
            tool_events = [e for e in events1 if isinstance(e, ToolCallRequest)]
            self.assertEqual(len(tool_events), 1)
            self.assertEqual(tool_events[0].name, "search")

            # Turn 2: after tool result, model gives final answer
            chunks2 = _make_text_stream("Found 3 results.")
            executor._client = FakeClient(chunks2)
            events2 = [
                e
                async for e in executor.run_turn(
                    [
                        {"role": "user", "content": "search for test"},
                        {
                            "role": "tool_call",
                            "content": {"tool": "search", "args": {"q": "test"}},
                        },
                        {"role": "tool_result", "content": {"results": ["a", "b", "c"]}},
                    ],
                    [{"name": "search", "description": "Search"}],
                    "sys",
                )
            ]
            turn_events = [e for e in events2 if isinstance(e, TurnComplete)]
            self.assertEqual(len(turn_events), 1)
            self.assertEqual(turn_events[0].response, "Found 3 results.")

        _run(_t())

    def test_interrupt_session_closes_active_stream(self):
        class ClosableStream:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        async def _t():
            executor = DatabricksExecutor(client=FakeClient([]))
            state = executor._get_or_create_session_state("s1")
            state.active_stream = ClosableStream()

            interrupted = await executor.interrupt_session("s1")

            self.assertTrue(interrupted)
            self.assertTrue(state.interrupt_requested)
            self.assertTrue(state.active_stream.closed)

        _run(_t())


# ---------------------------------------------------------------------------
# Credential resolution (_read_databrickscfg) — function-based pytest tests.
#
# These exercise the OAuth bug fix: _read_databrickscfg now delegates to the
# databricks-sdk so OAuth profiles (auth_type: databricks-cli) return a fresh
# minted bearer instead of the stale ``token`` field in ~/.databrickscfg.
# ---------------------------------------------------------------------------

# Imports below are intentionally at the bottom because the pytest-based section
# is appended to the pre-existing unittest module above.
import textwrap  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

import pytest  # noqa: E402

from omnigent.inner.databricks_executor import (  # noqa: E402
    DatabricksAuthError,
    _DatabricksBearerAuth,
    _read_databrickscfg,
    _read_databrickscfg_file_fallback,
    _read_databrickscfg_host,
)

_AUTH_ENV_VARS: tuple[str, ...] = (
    "DATABRICKS_HOST",
    "DATABRICKS_TOKEN",
    "DATABRICKS_CONFIG_PROFILE",
    "DATABRICKS_CONFIG_FILE",
    "DATABRICKS_CLIENT_ID",
    "DATABRICKS_CLIENT_SECRET",
    "DATABRICKS_AUTH_TYPE",
)


@pytest.fixture
def clean_databricks_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Clear every DATABRICKS_* env var that affects credential resolution.

    The agent harness that runs these tests may have e.g. ``DATABRICKS_TOKEN``
    exported for the coding agent itself — which would override profile-based
    resolution in the SDK and confuse these tests.
    """
    for var in _AUTH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _raise_offline_host_metadata(host: str) -> None:
    """Stand-in for ``databricks.sdk.config.get_host_metadata`` that fails fast.

    :param host: Workspace URL the SDK would have probed,
        e.g. ``"https://example.cloud.databricks.com"``.
    :raises ConnectionError: Always — simulates an unreachable host without
        the SDK's network retry loop.
    """
    raise ConnectionError(f"offline test stub: refusing to probe {host}")


@pytest.fixture
def pat_only_cfg(
    tmp_path: _Path, monkeypatch: pytest.MonkeyPatch, clean_databricks_env: None
) -> _Path:
    """
    Materialize a temp ``.databrickscfg`` containing a single PAT profile
    and point the SDK at it via ``DATABRICKS_CONFIG_FILE``.

    A PAT profile makes the SDK's ``authenticate()`` return the token
    verbatim without any OAuth exchange. The host is a placeholder, so the
    SDK's ``Config.__init__`` host-metadata probe (a real HTTP GET against
    ``/.well-known/databricks-config``, with retries) is stubbed to fail
    fast — ``_resolve_host_metadata`` logs and falls back to the explicit
    config, which is exactly the offline behavior these tests need.
    """
    monkeypatch.setattr(
        "databricks.sdk.config.get_host_metadata",
        _raise_offline_host_metadata,
    )
    contents = textwrap.dedent(
        """
        [pat-profile]
        host = https://example.cloud.databricks.com
        token = dapi-fake-pat-token-for-unit-test
        """
    ).lstrip()
    cfg_path = tmp_path / "databrickscfg"
    cfg_path.write_text(contents)
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))
    return cfg_path


def test_read_databrickscfg_pat_profile_returns_token_verbatim(
    pat_only_cfg: _Path,
) -> None:
    """
    For a plain ``auth_type=pat`` profile, the SDK should return the PAT
    from the file verbatim (no OAuth exchange, no token rewriting). This
    confirms we haven't regressed the common PAT-user path.
    """
    creds = _read_databrickscfg("pat-profile")

    assert creds is not None
    assert creds.host == "https://example.cloud.databricks.com"
    assert creds.token == "dapi-fake-pat-token-for-unit-test"


def test_read_databrickscfg_missing_profile_falls_back_to_file_reader(
    pat_only_cfg: _Path,
) -> None:
    """
    Requesting a profile that doesn't exist makes Config raise ValueError;
    the wrapper catches that and falls through to the legacy file reader.

    The legacy reader's documented resolution order is: explicit profile
    -> DATABRICKS_CONFIG_PROFILE env -> DEFAULT section -> first section
    with both host+token. So on a config file that contains only
    ``pat-profile``, an unknown requested profile falls through to
    "first section" — i.e. we recover PAT credentials.
    """
    creds = _read_databrickscfg("no-such-profile-xyz")

    assert creds is not None
    assert creds.host == "https://example.cloud.databricks.com"
    assert creds.token == "dapi-fake-pat-token-for-unit-test"


def test_read_databrickscfg_empty_config_file_returns_none(
    tmp_path: _Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_databricks_env: None,
) -> None:
    """
    With a config file that exists but contains no valid profiles, both
    the SDK path and the file fallback should resolve to ``None``.
    """
    cfg_path = tmp_path / "databrickscfg"
    cfg_path.write_text("# empty\n")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))

    assert _read_databrickscfg("missing-profile") is None


def test_read_databrickscfg_host_reads_oauth_profile_without_token(
    tmp_path: _Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_databricks_env: None,
) -> None:
    """
    Host-only resolution supports Databricks CLI OAuth profile sections.

    Native Codex does not need a static token at startup: it only needs the
    workspace host to build the Codex provider base URL, then Codex's
    ``auth.command`` calls ``databricks auth token --profile`` for live bearer
    refresh. A default install without ``databricks-sdk`` must therefore still
    accept a present ``auth_type=databricks-cli`` section with no ``token``.
    """
    cfg_path = tmp_path / "databrickscfg"
    cfg_path.write_text(
        textwrap.dedent(
            """
        [oauth-profile]
        host = https://oauth-host.example.com
        auth_type = databricks-cli
        """
        ).lstrip()
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))

    assert _read_databrickscfg_host("oauth-profile") == "https://oauth-host.example.com"


def test_read_databrickscfg_host_missing_named_profile_does_not_fallback(
    tmp_path: _Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_databricks_env: None,
) -> None:
    """An explicit missing profile must not borrow a different profile's host."""
    cfg_path = tmp_path / "databrickscfg"
    cfg_path.write_text(
        textwrap.dedent(
            """
        [other-profile]
        host = https://other.example.com
        auth_type = databricks-cli
        """
        ).lstrip()
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))

    assert _read_databrickscfg_host("missing-profile") is None


def test_codex_executor_gateway_uses_host_only_oauth_profile(
    tmp_path: _Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_databricks_env: None,
) -> None:
    """
    Wrapped Codex shares the native Codex host-only Databricks profile path.

    This covers default installs where the runner can read a
    ``databricks-cli`` profile's host but cannot mint a bearer snapshot at
    construction time.
    """
    cfg_path = tmp_path / "databrickscfg"
    cfg_path.write_text(
        textwrap.dedent(
            """
        [oauth-profile]
        host = https://oauth-host.example.com
        auth_type = databricks-cli
        """
        ).lstrip()
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(
        "omnigent.inner.codex_executor._read_databrickscfg",
        lambda _profile: None,
    )

    from omnigent.inner.codex_executor import CodexExecutor

    executor = CodexExecutor(
        codex_path=sys.executable,
        gateway=True,
        databricks_profile="oauth-profile",
    )

    overrides = "\n".join(executor._codex_config_overrides)
    assert "https://oauth-host.example.com/ai-gateway/codex/v1" in overrides
    assert 'databricks auth token --profile \\"oauth-profile\\"' in overrides


def test_read_databrickscfg_no_config_file_returns_none(
    tmp_path: _Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_databricks_env: None,
) -> None:
    """
    When ``~/.databrickscfg`` is absent and no env auth is set, both the
    SDK path and the file fallback should return ``None``.
    """
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / "does-not-exist"))

    assert _read_databrickscfg("DEFAULT") is None


def test_file_fallback_reads_token_field_directly(
    tmp_path: _Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_databricks_env: None,
) -> None:
    """
    The legacy fallback intentionally reads the ``token`` field as-is
    (that is the pre-fix behavior we preserve for exotic setups where
    the SDK's Config init raises). Confirm the fallback still works.
    """
    cfg_path = tmp_path / "databrickscfg"
    cfg_path.write_text(
        textwrap.dedent(
            """
        [p]
        host = https://legacy-host.example.com
        token = legacy-pat-value
        """
        ).lstrip()
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))

    creds = _read_databrickscfg_file_fallback("p")

    assert creds is not None
    assert creds.host == "https://legacy-host.example.com"
    assert creds.token == "legacy-pat-value"


def test_read_databrickscfg_falls_back_when_sdk_raises(
    tmp_path: _Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_databricks_env: None,
) -> None:
    """
    Verify the SDK-failure path: if ``databricks.sdk.config.Config`` raises
    ``ValueError`` during construction, the wrapper silently falls through
    to the file reader so plain PAT setups still work.

    We simulate this by stubbing the SDK's ``Config`` with a tiny real
    class that unconditionally raises on init — no MagicMock.
    """

    class _AlwaysFailsConfig:
        """
        Test double for ``databricks.sdk.config.Config`` that always
        raises ValueError — emulates an exotic setup the SDK can't parse.
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise ValueError("simulated SDK resolution failure")

    cfg_path = tmp_path / "databrickscfg"
    cfg_path.write_text(
        textwrap.dedent(
            """
        [fallback-profile]
        host = https://fallback-host.example.com
        token = fallback-pat-value
        """
        ).lstrip()
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))

    monkeypatch.setattr(_sdk_config_mod, "Config", _AlwaysFailsConfig)

    creds = _read_databrickscfg("fallback-profile")

    assert creds is not None
    assert creds.host == "https://fallback-host.example.com"
    assert creds.token == "fallback-pat-value"


def test_read_databrickscfg_missing_profile_uses_ambient_credentials(
    tmp_path: _Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_databricks_env: None,
) -> None:
    """
    When a named profile is given but ``Config(profile=...)`` fails, the
    wrapper tries the ambient credential chain (``Config()`` with no args)
    before falling back to the file reader.

    This covers the Databricks App deployment case: the spec was authored
    locally with ``profile: my-profile`` but runs on an App container that
    has no ``~/.databrickscfg`` — the App container supplies credentials
    via environment variables instead.
    """

    class _AmbientOnlyConfig:
        """
        Test double for ``databricks.sdk.config.Config``.

        Raises ``ValueError`` when called with a profile (simulating a
        missing named profile), but succeeds when called with no args
        (simulating ambient env-var credentials).
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            if kwargs.get("profile") is not None:
                raise ValueError("simulated missing profile")
            # Ambient path — expose host and a Bearer token.
            self.host = "https://ambient.example.com"

        def authenticate(self) -> dict[str, str]:
            return {"Authorization": "Bearer ambient-bearer-token"}

    monkeypatch.setattr(_sdk_config_mod, "Config", _AmbientOnlyConfig)

    creds = _read_databrickscfg("no-such-profile-xyz")

    # Ambient credentials resolved — the App-server fallback path works.
    # If this is None, the ambient Config() branch was not reached.
    assert creds is not None
    # Host must come from the ambient Config, not the file reader.
    assert creds.host == "https://ambient.example.com", (
        "Expected host from ambient Config(); got a different value, which "
        "suggests the file-reader fallback ran instead of the ambient path."
    )
    # Token must be the bearer stripped of the 'Bearer ' prefix.
    assert creds.token == "ambient-bearer-token", (
        "Expected token from ambient Config.authenticate(); a different value "
        "means the ambient credentials were not used."
    )


def test_read_databrickscfg_missing_profile_ambient_also_fails_uses_file_fallback(
    tmp_path: _Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_databricks_env: None,
) -> None:
    """
    When both ``Config(profile=...)`` and ``Config()`` (ambient) raise
    ``ValueError``, the wrapper falls through to the legacy file reader.

    This is the two-step failure path: named profile absent AND no ambient
    credentials in the environment — the file reader is the last resort.
    """

    class _ProfileFailsThenAmbientFails:
        """
        Test double for ``databricks.sdk.config.Config`` that raises
        ``ValueError`` unconditionally — emulates a machine with neither
        a matching named profile nor ambient env-var credentials.
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise ValueError("simulated: no profile, no ambient credentials")

    cfg_path = tmp_path / "databrickscfg"
    cfg_path.write_text(
        textwrap.dedent(
            """
        [some-profile]
        host = https://file-fallback.example.com
        token = file-fallback-token
        """
        ).lstrip()
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))

    monkeypatch.setattr(_sdk_config_mod, "Config", _ProfileFailsThenAmbientFails)

    creds = _read_databrickscfg("no-such-profile-xyz")

    # File reader ran as the last resort after both SDK paths failed.
    # If None, the file reader itself failed — check DATABRICKS_CONFIG_FILE.
    assert creds is not None
    # Host must come from the file reader, not any SDK path.
    assert creds.host == "https://file-fallback.example.com", (
        "Expected host from file-reader fallback; a different value means "
        "the SDK path resolved unexpectedly."
    )
    assert creds.token == "file-fallback-token", (
        "Expected token from file-reader fallback; a different value means "
        "the SDK path resolved unexpectedly."
    )


def test_read_databrickscfg_missing_profile_service_principal_via_ambient(
    monkeypatch: pytest.MonkeyPatch,
    clean_databricks_env: None,
) -> None:
    """
    Service principal (M2M) credentials supplied via environment variables
    (``DATABRICKS_HOST`` + ``DATABRICKS_CLIENT_ID`` + ``DATABRICKS_CLIENT_SECRET``)
    are resolved through the ambient ``Config()`` fallback when the named
    profile is absent.

    The Databricks SDK's ``Config()`` natively handles client-credential env
    vars, so no explicit SP handling is needed in ``_read_databrickscfg``.
    This test confirms the ambient path reaches a ``Config()`` call with no
    profile — the same call the SDK uses to pick up those env vars.
    """

    class _SPAmbientConfig:
        """
        Test double for ``databricks.sdk.config.Config``.

        Raises ``ValueError`` when called with a named profile (simulating a
        missing profile), succeeds when called with no args — as the SDK would
        do when ``DATABRICKS_HOST`` / ``DATABRICKS_CLIENT_ID`` /
        ``DATABRICKS_CLIENT_SECRET`` env vars are set.
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            if kwargs.get("profile") is not None:
                raise ValueError("simulated: named profile not found")
            # Ambient path — service principal resolved host + M2M token.
            self.host = "https://sp-workspace.example.com"

        def authenticate(self) -> dict[str, str]:
            return {"Authorization": "Bearer sp-m2m-access-token"}

    monkeypatch.setattr(_sdk_config_mod, "Config", _SPAmbientConfig)

    creds = _read_databrickscfg("missing-named-profile")

    assert creds is not None, (
        "Expected service-principal credentials via ambient Config(); got None. "
        "The ambient fallback for a missing named profile did not run."
    )
    assert creds.host == "https://sp-workspace.example.com", (
        f"Expected SP workspace host; got {creds.host!r}."
    )
    assert creds.token == "sp-m2m-access-token", f"Expected M2M bearer token; got {creds.token!r}."


def test_read_databrickscfg_oauth_profile_returns_fresh_bearer(
    tmp_path: _Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_databricks_env: None,
) -> None:
    """
    The core regression guard. For an OAuth profile
    (``auth_type: databricks-cli``), the returned token MUST be a freshly
    minted JWT, NOT the stale ``token`` field sitting in the config file.

    The test stubs the SDK's ``Config`` with a small real class so the
    assertion is deterministic and never depends on the developer or CI
    machine's real ``~/.databrickscfg``.
    """
    stale_field = "dapi-stale-config-token-fixture"
    fresh_bearer = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJvYXV0aCJ9.signature"
    cfg_path = tmp_path / "databrickscfg"
    cfg_path.write_text(
        textwrap.dedent(
            f"""
            [oauth-profile]
            host = https://oauth-workspace.example.com
            auth_type = databricks-cli
            token = {stale_field}
            """
        ).lstrip()
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))

    class _OAuthConfig:
        """
        Test double for ``databricks.sdk.config.Config``.

        It emulates SDK OAuth resolution: the config file may contain a stale
        PAT-shaped token field, but ``authenticate()`` returns the fresh
        bearer that ``_read_databrickscfg`` must use.
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            assert kwargs.get("profile") == "oauth-profile"
            self.host = "https://oauth-workspace.example.com"

        def authenticate(self) -> dict[str, str]:
            return {"Authorization": f"Bearer {fresh_bearer}"}

    monkeypatch.setattr(_sdk_config_mod, "Config", _OAuthConfig)

    creds = _read_databrickscfg("oauth-profile")

    assert creds is not None, "OAuth profile should resolve credentials"
    assert creds.host == "https://oauth-workspace.example.com"
    # OAuth access tokens are JWTs (header.payload.signature) — far longer
    # than a 36-char PAT and they start with "eyJ" (base64 of '{"alg":...').
    assert creds.token == fresh_bearer
    assert creds.token != stale_field, (
        "OAuth profile returned the stale PAT from the file — the fix is "
        "not actually exchanging credentials via the SDK."
    )


def test_resolve_databricks_auth_returns_bearer_auth_and_host(
    monkeypatch,
):
    """``_resolve_databricks_auth`` returns an httpx Auth + host.

    Verifies that the helper creates a ``_DatabricksBearerAuth``
    instance backed by the SDK Config and returns the workspace
    host URL.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.inner.databricks_executor import (
        _DatabricksBearerAuth,
        _resolve_databricks_auth,
    )

    class _FakeConfig:
        host = "https://example.cloud.databricks.com"
        profile = "dev"

        def authenticate(self):
            return {"Authorization": "Bearer fresh-tok"}

    monkeypatch.setattr(_sdk_config_mod, "Config", lambda **_kw: _FakeConfig())

    auth, host = _resolve_databricks_auth("dev")
    assert isinstance(auth, _DatabricksBearerAuth)
    assert host == "https://example.cloud.databricks.com"


def test_resolve_databricks_auth_invalid_profile_raises_clear_error(
    monkeypatch,
):
    """``_resolve_databricks_auth`` raises ``DatabricksAuthError``
    with an actionable message when the profile is not authenticated.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    import pytest

    import omnigent.inner.databricks_executor as db_exec
    from omnigent.inner.databricks_executor import (
        DatabricksAuthError,
        _resolve_databricks_auth,
    )

    def _failing_config(**_kw):
        raise ValueError("no credentials")

    monkeypatch.setattr(_sdk_config_mod, "Config", _failing_config)
    monkeypatch.setattr(db_exec, "_read_databrickscfg", lambda _p: None)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)

    with pytest.raises(DatabricksAuthError, match="databricks auth login -p dogfood"):
        _resolve_databricks_auth("dogfood")


def test_resolve_databricks_auth_env_profile_falls_back_to_ambient_with_warning(
    monkeypatch,
    caplog,
):
    """``_resolve_databricks_auth`` falls back to ambient when the profile
    came from DATABRICKS_CONFIG_PROFILE (not an explicit argument).

    When ``profile=None`` and ``DATABRICKS_CONFIG_PROFILE`` names a profile
    that cannot be resolved, the function falls back to the ambient credential
    chain and logs a warning.  This covers CI environments that provide OIDC
    tokens via env vars but don't have a matching profile in
    ``~/.databrickscfg``.

    The fallback must NOT fire when the profile was passed explicitly — that
    case must fail loud (see
    ``test_resolve_databricks_auth_invalid_profile_raises_clear_error``).

    :param monkeypatch: Pytest monkeypatch fixture.
    :param caplog: Pytest log-capture fixture.
    """
    import logging

    import omnigent.inner.databricks_executor as db_exec
    from omnigent.inner.databricks_executor import (
        _DatabricksBearerAuth,
        _resolve_databricks_auth,
    )

    def _config_factory(**kw):
        if kw.get("profile") is not None:
            raise ValueError("simulated: profile not found in ~/.databrickscfg")

        class _AmbientConfig:
            host = "https://example.cloud.databricks.com"

            def authenticate(self):
                return {"Authorization": "Bearer oidc-tok"}

        return _AmbientConfig()

    monkeypatch.setattr(_sdk_config_mod, "Config", _config_factory)
    monkeypatch.setattr(db_exec, "_read_databrickscfg", lambda _p: None)
    # Profile comes from env var, not an explicit argument.
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "missing-profile")

    with caplog.at_level(logging.WARNING, logger="omnigent.inner.databricks_executor"):
        auth, host = _resolve_databricks_auth()  # profile=None — uses env var

    assert isinstance(auth, _DatabricksBearerAuth), (
        "Expected a _DatabricksBearerAuth from the ambient Config() fallback; "
        "the ambient path was not reached."
    )
    assert host == "https://example.cloud.databricks.com", (
        f"Expected ambient workspace host, got {host!r}"
    )
    # The fallback must be logged — it must not be silent.
    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("missing-profile" in m and "ambient" in m for m in warning_messages), (
        f"Expected a warning mentioning the profile name and 'ambient', got: {warning_messages!r}"
    )


def test_resolve_databricks_auth_explicit_profile_not_found_raises(
    monkeypatch,
):
    """Explicit profile failures raise ``DatabricksAuthError``, no ambient fallback.

    When the user explicitly passes ``--profile dev`` and the profile cannot
    be resolved, ``_resolve_databricks_auth`` must raise ``DatabricksAuthError``
    immediately — NOT silently fall back to a different workspace.  This
    upholds the "Fail loud" design principle.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    import pytest

    import omnigent.inner.databricks_executor as db_exec
    from omnigent.inner.databricks_executor import (
        DatabricksAuthError,
        _resolve_databricks_auth,
    )

    call_log: list[dict] = []

    def _config_factory(**kw):
        call_log.append(kw)
        raise ValueError("simulated: profile not found in ~/.databrickscfg")

    monkeypatch.setattr(_sdk_config_mod, "Config", _config_factory)
    monkeypatch.setattr(db_exec, "_read_databrickscfg", lambda _p: None)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)

    with pytest.raises(DatabricksAuthError, match="databricks auth login -p dev"):
        _resolve_databricks_auth("dev")

    # Ambient Config() must NOT be called — no fallback for explicit profiles.
    # If Config() were called with no args, call_log would have a second entry.
    assert len(call_log) == 1, (
        f"Expected exactly one Config() call (the explicit profile), "
        f"got {len(call_log)}: {call_log!r}. "
        f"The ambient fallback fired for an explicit profile — violates Fail Loud."
    )


def test_bearer_auth_injects_fresh_token_per_request():
    """``_DatabricksBearerAuth`` calls ``Config.authenticate()`` per request.

    Verifies that a fresh token is injected into each HTTP request,
    and that different calls can return different tokens (simulating
    OAuth refresh).
    """
    import httpx

    from omnigent.inner.databricks_executor import _DatabricksBearerAuth

    call_count = 0

    class _RotatingConfig:
        def authenticate(self):
            nonlocal call_count
            call_count += 1
            return {"Authorization": f"Bearer tok-{call_count}"}

    auth = _DatabricksBearerAuth(_RotatingConfig(), profile_name="dev")

    request1 = httpx.Request("GET", "https://example.com/v1/chat")
    gen1 = auth.auth_flow(request1)
    next(gen1)
    assert request1.headers["Authorization"] == "Bearer tok-1"

    request2 = httpx.Request("GET", "https://example.com/v1/chat")
    gen2 = auth.auth_flow(request2)
    next(gen2)
    assert request2.headers["Authorization"] == "Bearer tok-2"

    assert call_count == 2


def test_bearer_auth_raises_on_expired_refresh_token():
    """``_DatabricksBearerAuth`` wraps SDK failures as ``DatabricksAuthError``.

    When the underlying ``Config.authenticate()`` fails (e.g. the OAuth
    refresh token expired), the auth flow raises ``DatabricksAuthError``
    with an actionable error message.
    """
    import httpx
    import pytest

    from omnigent.inner.databricks_executor import (
        DatabricksAuthError,
        _DatabricksBearerAuth,
    )

    class _DeadConfig:
        def authenticate(self):
            raise ValueError("token expired")

    auth = _DatabricksBearerAuth(_DeadConfig(), profile_name="dogfood")
    request = httpx.Request("GET", "https://example.com/v1/chat")

    with pytest.raises(DatabricksAuthError, match="databricks auth login -p dogfood"):
        gen = auth.auth_flow(request)
        next(gen)


def test_claude_sdk_executor_reuses_api_key_helper_between_turns(
    monkeypatch,
):
    """ClaudeSDKExecutor leaves refresh cadence to Claude Code.

    The Claude CLI subprocess should not receive a static
    ``ANTHROPIC_AUTH_TOKEN``. It receives an apiKeyHelper command
    that Claude Code can re-run while a long turn is in flight.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """

    call_count = 0

    def _counting_resolve(
        profile=None,
        *,
        host_override=None,
        base_url_override=None,
        auth_command_override=None,
        auth_refresh_interval_ms=None,
    ):
        """Return a distinct helper command if a turn tries to refresh.

        :param profile: Databricks profile name.
        :param host_override: Optional ucode workspace host.
        :param base_url_override: Optional ucode gateway base URL.
        :param auth_command_override: Optional ucode auth command.
        :param auth_refresh_interval_ms: Optional ucode refresh cadence.
        :returns: Fake Claude Databricks env payload.
        """
        nonlocal call_count
        call_count += 1
        return {
            "ANTHROPIC_BASE_URL": "https://host/ai-gateway/anthropic",
            "OMNIGENT_CLAUDE_API_KEY_HELPER": f"helper-{call_count}",
            "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": "900000",
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        }

    monkeypatch.setattr(
        "omnigent.inner.claude_sdk_executor._resolve_gateway_env",
        _counting_resolve,
    )

    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

    # Construct with initial env.
    executor = ClaudeSDKExecutor.__new__(ClaudeSDKExecutor)
    executor._gateway = True
    executor._databricks_profile = "oss"
    executor._gateway_host = None
    executor._base_url_override = None
    executor._extra_env = {
        "OMNIGENT_CLAUDE_API_KEY_HELPER": "helper-initial",
        "ANTHROPIC_BASE_URL": "https://host/ai-gateway/anthropic",
        "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": "900000",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    }

    async def _trigger_refresh():
        # run_turn refreshes at the top, then fails (no real SDK).
        try:
            async for _ in executor.run_turn(
                [{"role": "user", "content": "hi", "session_id": "s1"}],
                [],
                "",
            ):
                pass
        except Exception:
            pass
        return executor._extra_env.get("OMNIGENT_CLAUDE_API_KEY_HELPER")

    helper = _run(_trigger_refresh())

    assert helper == "helper-initial"
    assert "ANTHROPIC_AUTH_TOKEN" not in executor._extra_env
    assert call_count == 0


def test_codex_executor_uses_cli_auth_command_not_env_token(
    monkeypatch,
):
    """CodexExecutor leaves token refresh to Codex's ``auth.command``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    call_count = 0

    def _counting_read(profile=None):
        nonlocal call_count
        call_count += 1
        return type(
            "Creds",
            (),
            {
                "host": "https://host",
                "token": f"tok-{call_count}",
            },
        )()

    monkeypatch.setattr(
        "omnigent.inner.codex_executor._read_databrickscfg",
        _counting_read,
    )

    from omnigent.inner.codex_executor import CodexExecutor

    executor = CodexExecutor.__new__(CodexExecutor)
    executor._databricks = True
    executor._databricks_profile = "oss"
    executor._env = {}

    async def _trigger_refresh():
        try:
            async for _ in executor.run_turn(
                [{"role": "user", "content": "hi", "session_id": "s1"}],
                [],
                "",
            ):
                pass
        except Exception:
            pass
        return executor._env.get("DATABRICKS_TOKEN")

    token = _run(_trigger_refresh())

    assert token is None
    assert call_count == 0


def test_bearer_auth_current_token_reuses_config_and_strips_prefix() -> None:
    """
    ``_DatabricksBearerAuth.current_token()`` returns the bare bearer (no
    ``"Bearer "`` prefix) from the WRAPPED config's ``authenticate()`` and
    reuses that one config across calls.

    Reuse is the whole point of the per-request auth-tax fix: the SDK serves
    repeat calls from its in-memory token cache instead of re-shelling to the
    Databricks CLI. If ``current_token`` rebuilt a Config per call or failed
    to strip the prefix, this test fails.
    """

    class _CountingConfig:
        """Config double whose authenticate() counts calls."""

        def __init__(self) -> None:
            self.authenticate_calls = 0

        def authenticate(self) -> dict[str, str]:
            self.authenticate_calls += 1
            return {"Authorization": "Bearer dapi-XYZ"}

    cfg = _CountingConfig()
    auth = _DatabricksBearerAuth(cfg, profile_name="oss")

    tokens = [auth.current_token() for _ in range(4)]

    # Bare token (prefix stripped) on every call. A failure here means
    # current_token returned the raw header or the wrong field.
    assert tokens == ["dapi-XYZ"] * 4
    # All 4 calls went through the SAME wrapped config — proving reuse, so
    # the SDK's own cache (not a rebuilt Config) backs repeat calls. If the
    # method rebuilt Config, it would not be this single object's counter.
    assert cfg.authenticate_calls == 4


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Basic dXNlcjpwYXNz"},  # non-Bearer scheme
        {},  # no Authorization header at all
    ],
)
def test_bearer_auth_current_token_none_for_non_bearer(headers: dict[str, str]) -> None:
    """
    ``current_token()`` returns ``None`` for a non-Bearer or empty
    ``Authorization`` header (the system only supports Bearer). Returning the
    raw header would feed callers a credential the Omnigent server can't use.
    """

    class _Config:
        """Config double returning a fixed (non-Bearer) header set."""

        def authenticate(self) -> dict[str, str]:
            return headers

    assert _DatabricksBearerAuth(_Config()).current_token() is None


def test_bearer_auth_current_token_wraps_failure_as_auth_error() -> None:
    """
    ``current_token()`` raises :class:`DatabricksAuthError` when the SDK's
    ``authenticate()`` raises — the same fail-loud contract as ``auth_flow``,
    so token-mint failures surface with a re-login hint instead of silently
    yielding a bad/empty credential.
    """

    class _FailingConfig:
        """Config double whose authenticate() always raises."""

        def authenticate(self) -> dict[str, str]:
            raise RuntimeError("token endpoint unreachable")

    with pytest.raises(DatabricksAuthError):
        _DatabricksBearerAuth(_FailingConfig(), profile_name="oss").current_token()


if __name__ == "__main__":
    unittest.main()


# ── host-keyed resolution (omnigent login pointer records) ─────────


class _StubSdkConfig:
    """Minimal stand-in for ``databricks.sdk.config.Config``.

    Real ``Config`` probes host metadata at construction, which fails
    offline for placeholder hosts — the production seam
    (``_sdk_config``) exists precisely so tests can substitute this.

    :param host: The workspace host the config resolves to.
    :param token: The bearer minted by :meth:`authenticate`.
    """

    def __init__(self, host: str, token: str) -> None:
        self.host = host
        self._token = token

    def authenticate(self) -> dict[str, str]:
        """Return ``Authorization`` headers like the real Config."""
        return {"Authorization": f"Bearer {self._token}"}


def test_resolve_auth_for_host_prefers_matching_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host resolution authenticates via the cfg profile pinned to the host.

    ``databricks auth login --host <ws>`` saves a named profile, and the
    CLI's host-keyed token lookup is unreliable across versions (it can
    miss profile-keyed grants, or return a grant for a different
    workspace). If this regresses to the bare ``databricks-cli`` host
    lookup, the constructed kwargs below change and the test fails.
    """
    from omnigent.inner import databricks_executor

    cfg_path = tmp_path / "databrickscfg"
    cfg_path.write_text(
        "[my-ws]\nhost = https://example.databricks.com\ntoken = dapi-fake-token\n"
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))

    constructed: list[dict[str, str]] = []

    def _fake_sdk_config(**kwargs: str) -> _StubSdkConfig:
        constructed.append(kwargs)
        assert kwargs == {"profile": "my-ws"}, (
            f"expected the host-matched profile to be tried first, got {kwargs}. "
            "A host/auth_type construction here means the unreliable "
            "databricks-cli host lookup regained priority."
        )
        return _StubSdkConfig(host="https://example.databricks.com", token="dapi-fake-token")

    monkeypatch.setattr(databricks_executor, "_sdk_config", _fake_sdk_config)

    auth, host = databricks_executor._resolve_databricks_auth(
        host="https://example.databricks.com"
    )

    # The stub's PAT coming back proves the profile-path config is the
    # one wired into the returned auth (not a second construction).
    assert auth.current_token() == "dapi-fake-token"
    assert host == "https://example.databricks.com"
    # Exactly one construction: the matched profile authenticated, so the
    # CLI host fallback must not run at all.
    assert constructed == [{"profile": "my-ws"}]


def test_resolve_auth_for_host_falls_back_to_cli_when_no_profile_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no cfg profile pinned to the host, the CLI host lookup runs.

    Cfg-less machines (fresh laptop, CI) have only the host-keyed OAuth
    cache from ``databricks auth login --host`` — dropping this fallback
    would strand them.
    """
    from omnigent.inner import databricks_executor

    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / "absent"))

    def _fake_sdk_config(**kwargs: str) -> _StubSdkConfig:
        # The only construction allowed is the host-keyed CLI lookup —
        # a profile= construction here means a phantom profile matched.
        assert kwargs == {
            "host": "https://example.databricks.com",
            "auth_type": "databricks-cli",
        }, f"unexpected Config construction: {kwargs}"
        return _StubSdkConfig(host="https://example.databricks.com", token="tok-cli")

    monkeypatch.setattr(databricks_executor, "_sdk_config", _fake_sdk_config)

    auth, host = databricks_executor._resolve_databricks_auth(
        host="https://example.databricks.com"
    )

    assert auth.current_token() == "tok-cli"
    assert host == "https://example.databricks.com"


def test_profiles_for_host_normalizes_scheme_and_slash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host matching ignores scheme and trailing slashes, in file order.

    ``databrickscfg`` hosts appear both with and without ``https://`` and
    trailing ``/`` in the wild; a strict string compare would silently
    miss the profile and fall through to the unreliable CLI host lookup.
    """
    from omnigent.inner.databricks_executor import _databrickscfg_profiles_for_host

    cfg_path = tmp_path / "databrickscfg"
    cfg_path.write_text(
        "[bare]\nhost = example.databricks.com\ntoken = t1\n"
        "[slashed]\nhost = https://example.databricks.com/\ntoken = t2\n"
        "[other]\nhost = https://other.databricks.com\ntoken = t3\n"
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))

    matches = _databrickscfg_profiles_for_host("https://example.databricks.com")

    # Both spellings of the host match; the unrelated workspace does not.
    assert matches == ["bare", "slashed"]


def test_profiles_for_host_missing_file_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No config file → no profile candidates (CLI fallback territory)."""
    from omnigent.inner.databricks_executor import _databrickscfg_profiles_for_host

    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / "absent"))

    assert _databrickscfg_profiles_for_host("https://example.databricks.com") == []


def test_stream_ended_without_finish_reason_with_content_completes() -> None:
    """A truncated stream that still produced text surfaces that text as a
    TurnComplete (not an error) — only the empty case is fatal (#1118)."""

    async def _t() -> None:
        # Content arrives, then the stream ends without a finish_reason.
        chunks = [FakeStreamChunk(choices=[FakeStreamChoice(delta=FakeDelta(content="partial"))])]
        executor = DatabricksExecutor(client=FakeClient(chunks))

        events = [
            e
            async for e in executor.run_turn(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                system_prompt="Be helpful.",
                config=ExecutorConfig(model="databricks-kimi-k2-6"),
            )
        ]

        assert not [e for e in events if isinstance(e, ExecutorError)]
        turn_events = [e for e in events if isinstance(e, TurnComplete)]
        assert len(turn_events) == 1
        assert turn_events[0].response == "partial"

    _run(_t())
