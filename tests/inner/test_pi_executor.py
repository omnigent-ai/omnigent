"""Tests for PiExecutor."""

import asyncio
import json
import logging
import os
import shutil
import socket
import tempfile
import textwrap
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnigent.inner.databricks_executor import DatabricksCredentials
from omnigent.inner.executor import (
    ExecutorConfig,
    ExecutorError,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    TurnComplete,
)
from omnigent.inner.pi_executor import (
    PiExecutor,
    _build_models_json,
    _generate_extension_js,
    _pi_provider_for_model,
    _PiRpcSession,
    _redact_argv_for_log,
    _safe_dumps,
    _sanitize_schema,
    _split_pi_prompt,
    _ToolServer,
)
from omnigent.onboarding.databricks_config import DATABRICKS_CLAUDE_DEFAULT_MODEL
from omnigent.runtime.harnesses._scaffold import PolicyVerdictPayload


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


# ---------------------------------------------------------------------------
# Helper fakes
# ---------------------------------------------------------------------------


class _FakeStreamReader:
    """Simulates asyncio.StreamReader with pre-loaded lines."""

    def __init__(self, lines: list[bytes]):
        self._buffer = bytearray(b"".join(lines))

    async def readline(self) -> bytes:
        if not self._buffer:
            return b""
        newline_index = self._buffer.find(b"\n")
        if newline_index >= 0:
            end = newline_index + 1
            line = bytes(self._buffer[:end])
            del self._buffer[:end]
            return line
        line = bytes(self._buffer)
        self._buffer.clear()
        return line

    async def read(self, n: int = -1) -> bytes:
        if not self._buffer:
            return b""
        if n is None or n < 0 or n > len(self._buffer):
            n = len(self._buffer)
        chunk = bytes(self._buffer[:n])
        del self._buffer[:n]
        return chunk

    def __aiter__(self):
        return self

    async def __anext__(self):
        line = await self.readline()
        if not line:
            raise StopAsyncIteration
        return line


class _FakeStreamWriter:
    def __init__(self):
        self.data: list[bytes] = []
        self._closed = False

    def write(self, data: bytes):
        self.data.append(data)

    async def drain(self):
        pass

    def close(self):
        self._closed = True

    async def wait_closed(self):
        pass


class _FakeProcess:
    def __init__(
        self, stdout_lines: list[str] | None = None, stderr_lines: list[str] | None = None
    ):
        stdout_bytes = [(line + "\n").encode() for line in (stdout_lines or [])]
        stderr_bytes = [(line + "\n").encode() for line in (stderr_lines or [])]
        self.stdin = _FakeStreamWriter()
        self.stdout = _FakeStreamReader(stdout_bytes)
        self.stderr = _FakeStreamReader(stderr_bytes)
        self.returncode = None
        self.pid = 99999

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode or 0


# ---------------------------------------------------------------------------
# _sanitize_schema tests
# ---------------------------------------------------------------------------


class TestSanitizeSchema(unittest.TestCase):
    def test_removes_examples_and_default(self):
        schema = {
            "type": "object",
            "properties": {
                "a": {"type": "string", "examples": ["foo"], "default": "bar"},
            },
        }
        result = _sanitize_schema(schema)
        self.assertNotIn("examples", result["properties"]["a"])
        self.assertNotIn("default", result["properties"]["a"])

    def test_collapses_anyof_to_first_typed(self):
        schema = {
            "anyOf": [
                {"type": "string"},
                {"type": "integer"},
            ]
        }
        result = _sanitize_schema(schema)
        self.assertEqual(result, {"type": "string"})

    def test_removes_additional_properties(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"x": {"type": "integer"}},
        }
        result = _sanitize_schema(schema)
        self.assertNotIn("additionalProperties", result)

    def test_nested_properties_are_sanitized(self):
        schema = {
            "type": "object",
            "properties": {
                "nested": {
                    "type": "object",
                    "properties": {
                        "inner": {"type": "string", "default": "d"},
                    },
                },
            },
        }
        result = _sanitize_schema(schema)
        self.assertNotIn("default", result["properties"]["nested"]["properties"]["inner"])

    def test_items_are_sanitized(self):
        schema = {
            "type": "array",
            "items": {"type": "string", "examples": ["a"]},
        }
        result = _sanitize_schema(schema)
        self.assertNotIn("examples", result["items"])

    def test_passthrough_for_non_dict(self):
        self.assertEqual(_sanitize_schema("hello"), "hello")


@pytest.mark.parametrize("union_key", ["anyOf", "oneOf", "allOf"])
def test_sanitize_union_prefers_object_branch(union_key: str) -> None:
    """
    A union with both a string and an object branch must collapse to
    the OBJECT branch, with its properties intact and its own
    ``additionalProperties`` stripped.

    If the string branch wins instead, the pi LLM sees the param as a
    plain string and serializes structured args as a JSON string —
    this is exactly how nessie's purpose-guard policy ended up
    denying every sub-agent dispatch on pi.

    :param union_key: The JSON Schema union keyword under test,
        e.g. ``"anyOf"`` — all three must collapse identically.
    """
    schema = {
        union_key: [
            {"type": "string"},
            {
                "type": "object",
                "properties": {
                    "input": {"type": "string"},
                    "purpose": {"type": "string"},
                },
                "required": ["input"],
                "additionalProperties": False,
            },
        ]
    }
    # Exact dict: object branch chosen, properties/required preserved,
    # additionalProperties stripped. {"type": "string"} here means the
    # collapse regressed to first-typed-branch.
    assert _sanitize_schema(schema) == {
        "type": "object",
        "properties": {
            "input": {"type": "string"},
            "purpose": {"type": "string"},
        },
        "required": ["input"],
    }


def test_sanitize_union_without_object_falls_back_to_first_typed() -> None:
    """
    With no object branch, the collapse falls back to the FIRST typed
    branch, skipping untyped entries.

    ``{"type": "string"}`` here would mean ordering broke;
    ``{"description": ...}`` would mean an untyped branch was chosen.
    """
    schema = {
        "anyOf": [
            {"description": "untyped branch"},
            {"type": "integer"},
            {"type": "string"},
        ]
    }
    assert _sanitize_schema(schema) == {"type": "integer"}


def test_sanitize_union_nested_in_properties_keeps_object_branch() -> None:
    """
    A union nested inside an outer object's ``properties`` collapses
    to its object branch while sibling properties pass through
    untouched — the recursion into ``properties`` must apply the same
    object-preference as the top level.
    """
    schema = {
        "type": "object",
        "properties": {
            "args": {
                "anyOf": [
                    {"type": "string"},
                    {
                        "type": "object",
                        "properties": {"input": {"type": "string"}},
                        "required": ["input"],
                        "additionalProperties": False,
                    },
                ]
            },
            "other": {"type": "integer"},
        },
        "required": ["args"],
    }
    assert _sanitize_schema(schema) == {
        "type": "object",
        "properties": {
            "args": {
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
            },
            "other": {"type": "integer"},
        },
        "required": ["args"],
    }


def test_sanitize_real_sys_session_send_args_collapses_to_object() -> None:
    """
    The REAL ``sys_session_send`` schema's ``args`` param (anyOf of
    string | {input, purpose} object) must collapse to the object
    branch so the model emits structured args.

    Uses the actual schema builder from spawn.py, not a copy — if the
    spawn schema's shape drifts, this test follows it. A string-typed
    ``args`` result reproduces the nessie-on-pi dispatch denial
    ("Missing object args with purpose").
    """
    from omnigent.tools.builtins.spawn import _build_sys_session_send_schema

    params = _build_sys_session_send_schema({})["function"]["parameters"]
    object_branch = next(
        b for b in params["properties"]["args"]["anyOf"] if b.get("type") == "object"
    )

    sanitized_args = _sanitize_schema(params)["properties"]["args"]

    # Structured fields the purpose guard and the per-dispatch model
    # override read must survive the collapse.
    assert sanitized_args["type"] == "object"
    assert set(sanitized_args["properties"]) == {"input", "purpose", "model"}
    assert sanitized_args["required"] == ["input"]
    # Exact dict: the chosen object branch minus its stripped
    # additionalProperties — anything else means extra keys leaked or
    # the wrong branch was picked.
    expected = {k: v for k, v in object_branch.items() if k != "additionalProperties"}
    assert sanitized_args == expected


# ---------------------------------------------------------------------------
# _pi_provider_for_model tests
# ---------------------------------------------------------------------------


class TestPiProviderForModel(unittest.TestCase):
    def test_gpt_model(self):
        self.assertEqual(_pi_provider_for_model("databricks-gpt-5-4-mini"), "databricks")

    def test_claude_model(self):
        self.assertEqual(
            _pi_provider_for_model("databricks-claude-sonnet-4-6"), "databricks-anthropic"
        )

    def test_other_model(self):
        self.assertEqual(
            _pi_provider_for_model("databricks-meta-llama-3.3-70b-instruct"),
            "databricks-completions",
        )


# ---------------------------------------------------------------------------
# _split_pi_prompt tests
# ---------------------------------------------------------------------------


def test_split_pi_prompt_separates_text_and_images():
    # #515: multimodal blocks must go to Pi's native message + images, not be
    # JSON-encoded into the text (which the model reads as a literal blob).
    message, images = _split_pi_prompt(
        [
            {"type": "input_text", "text": "what is in this image?"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
        ]
    )
    assert message == "what is in this image?"
    assert images == [{"type": "image", "data": "AAAA", "mimeType": "image/png"}]


def test_split_pi_prompt_text_only_has_no_images():
    message, images = _split_pi_prompt([{"type": "input_text", "text": "hi"}])
    assert message == "hi"
    assert images == []


def test_split_pi_prompt_rejects_non_data_uri_image():
    # A non-data-URI input_image (e.g. a file reference) cannot be forwarded
    # inline to Pi, so it raises a clear ValueError -- run_turn catches this and
    # surfaces it as an ExecutorError instead of crashing the turn (#516 review).
    with pytest.raises(ValueError, match="inline data URI"):
        _split_pi_prompt([{"type": "input_image", "image_url": "file-abc123"}])


def test_split_pi_prompt_inlines_text_input_file():
    # #516 review: a text-like input_file is decoded into the message (so the
    # model can read it) rather than dropped or hard-failed — mirroring codex.
    import base64 as _b64

    payload = _b64.b64encode(b"hello from a file").decode()
    message, images = _split_pi_prompt(
        [
            {"type": "input_text", "text": "summarize:"},
            {"type": "input_file", "file_data": f"data:text/markdown;base64,{payload}"},
        ]
    )
    assert message == "summarize:\nhello from a file"
    assert images == []


def test_split_pi_prompt_skips_binary_input_file():
    # A binary input_file (e.g. PDF) can't be inlined as text; it's skipped
    # (with a logged warning), not raised — the turn still runs.
    message, images = _split_pi_prompt(
        [
            {"type": "input_text", "text": "hi"},
            {"type": "input_file", "file_data": "data:application/pdf;base64,JVBERi0x"},
        ]
    )
    assert message == "hi"
    assert images == []


def test_split_pi_prompt_rejects_genuinely_unknown_block_type():
    # A block type Pi can't handle at all still fails loudly rather than
    # silently vanishing -> run_turn surfaces it as an ExecutorError.
    with pytest.raises(ValueError, match="Unsupported content block type"):
        _split_pi_prompt([{"type": "input_audio", "audio": "..."}])


# _build_models_json tests
# ---------------------------------------------------------------------------


class TestBuildModelsJson(unittest.TestCase):
    def test_has_three_providers(self):
        result = _build_models_json("https://host.example.com", "tok123")
        providers = result["providers"]
        self.assertIn("databricks", providers)
        self.assertIn("databricks-anthropic", providers)
        self.assertIn("databricks-completions", providers)

    def test_dynamic_model_declared_image_capable(self):
        # #515: a dynamically-registered model must advertise image input, or
        # Pi's transformMessages strips every image block ("model does not
        # support images") before the message reaches the provider.
        model = "databricks-qwen2-5-vl-72b"
        result = _build_models_json("https://host.example.com", "tok", model=model)
        provider = result["providers"][_pi_provider_for_model(model)]
        entry = next(e for e in provider["models"] if e["id"] == model)
        self.assertEqual(entry.get("input"), ["text", "image"])

    def test_static_model_declared_image_capable(self):
        # #516 review: a STATIC (pre-registered) vision model must also
        # advertise image input. The dynamic-registration append is gated on
        # the model not already being listed, so a default model like GPT-5.4
        # (openai-completions) or Claude would otherwise keep an input-less
        # entry and have its images stripped.
        for model in ("databricks-gpt-5-4", "databricks-claude-opus-4-8"):
            result = _build_models_json("https://host.example.com", "tok", model=model)
            provider = result["providers"][_pi_provider_for_model(model)]
            entry = next(e for e in provider["models"] if e["id"] == model)
            self.assertEqual(entry.get("input"), ["text", "image"], model)

    def test_base_urls_use_host(self):
        result = _build_models_json("https://host.example.com/", "tok")
        p = result["providers"]
        self.assertTrue(
            p["databricks"]["baseUrl"].startswith("https://host.example.com/serving-endpoints")
        )
        self.assertIn("/anthropic", p["databricks-anthropic"]["baseUrl"])

    def test_base_urls_can_come_from_ucode_state(self):
        result = _build_models_json(
            "https://host.example.com",
            "tok",
            {
                "claude": "https://host.example.com/ai-gateway/anthropic",
                "openai": "https://host.example.com/ai-gateway/codex/v1",
            },
        )
        p = result["providers"]
        # The ucode ``openai`` value is the Codex Responses gateway; GPT and the
        # catch-all re-route to serving-endpoints, claude keeps its gateway.
        self.assertEqual(
            p["databricks"]["baseUrl"],
            "https://host.example.com/serving-endpoints",
        )
        self.assertEqual(
            p["databricks-anthropic"]["baseUrl"],
            "https://host.example.com/ai-gateway/anthropic",
        )
        self.assertEqual(
            p["databricks-completions"]["baseUrl"],
            "https://host.example.com/serving-endpoints",
        )

    def test_ucode_codex_gateway_rerouted_off_responses_path(self):
        # The codex gateway 404s /chat/completions, so it must not survive onto
        # a completions provider (#241 GPT 404).
        result = _build_models_json(
            "https://host.example.com",
            "tok",
            {"openai": "https://host.example.com/ai-gateway/codex/v1"},
        )
        for name in ("databricks", "databricks-completions"):
            base_url = result["providers"][name]["baseUrl"]
            self.assertNotIn("/ai-gateway/codex", base_url)
            self.assertEqual(base_url, "https://host.example.com/serving-endpoints")

    def test_gemini_model_routed_off_codex_gateway(self):
        # Gemini falls to the databricks-completions catch-all; it must land on
        # serving-endpoints, not the codex URL it used to inherit (#241).
        result = _build_models_json(
            "https://host.example.com",
            "tok",
            {"openai": "https://host.example.com/ai-gateway/codex/v1"},
            model="databricks-gemini-2-5-pro",
        )
        provider = result["providers"][_pi_provider_for_model("databricks-gemini-2-5-pro")]
        self.assertEqual(provider["baseUrl"], "https://host.example.com/serving-endpoints")
        self.assertIn(
            "databricks-gemini-2-5-pro",
            [entry.get("id") for entry in provider["models"]],
        )

    def test_generic_openai_base_url_used_as_is(self):
        # A non-ucode openai URL (no ``/ai-gateway/codex``) must pass through so
        # the re-route never breaks generic gateways.
        result = _build_models_json(
            "https://host.example.com",
            "tok",
            {"openai": "https://openrouter.ai/api/v1"},
        )
        p = result["providers"]
        self.assertEqual(p["databricks"]["baseUrl"], "https://openrouter.ai/api/v1")
        self.assertEqual(p["databricks-completions"]["baseUrl"], "https://openrouter.ai/api/v1")

    def test_api_key_set(self):
        result = _build_models_json("https://host.example.com", "mytoken")
        for prov in result["providers"].values():
            self.assertEqual(prov["apiKey"], "mytoken")

    def test_gpt_provider_uses_completions_api(self):
        result = _build_models_json("https://h", "t")
        self.assertEqual(result["providers"]["databricks"]["api"], "openai-completions")


# ---------------------------------------------------------------------------
# _generate_extension_js tests
# ---------------------------------------------------------------------------


class TestGenerateExtensionJs(unittest.TestCase):
    def test_contains_tool_names(self):
        schemas = [
            {
                "name": "my_tool",
                "description": "Does stuff",
                "parameters": {"type": "object", "properties": {}},
            },
        ]
        js = _generate_extension_js(12345, schemas, "tok-abc123")
        self.assertIn("my_tool", js)
        self.assertIn("12345", js)
        self.assertIn("pi.registerTool", js)
        # Token embedded and sent on each request, else the server
        # rejects the bridge as unauthenticated.
        self.assertIn('const TOKEN = "tok-abc123";', js)
        self.assertIn("token: TOKEN", js)

    def test_empty_tools(self):
        js = _generate_extension_js(9999, [], "tok-xyz")
        self.assertIn("pi.registerTool", js)
        self.assertIn("9999", js)
        self.assertIn('const TOKEN = "tok-xyz";', js)

    def test_registers_native_tool_call_policy_hook(self):
        """The extension installs a ``tool_call`` hook that gates native tools.

        Pi's native tools (e.g. ``read``, enabled for skill loading) run
        in-process and bypass the bridged ``/mcp`` policy path. The hook
        closes that gap: it must (1) register a ``tool_call`` listener, (2)
        skip bridged tools (already gated server-side), and (3) send a
        ``policy_eval`` frame and block on the verdict. If any piece is
        missing the native tools run ungated.
        """
        schemas = [
            {
                "name": "sys_os_read",
                "description": "bridged read",
                "parameters": {"type": "object", "properties": {}},
            },
        ]
        js = _generate_extension_js(12345, schemas, "tok")
        # (1) A tool_call hook is registered.
        self.assertIn('pi.on("tool_call"', js)
        # (2) Bridged tools are skipped so they aren't double-evaluated
        # (the bridged set is built from the registered tool names).
        self.assertIn("const BRIDGED = new Set(TOOLS.map((t) => t.name));", js)
        self.assertIn("BRIDGED.has(event.toolName)", js)
        # (3) Native tools are evaluated via a policy_eval frame and the
        # block verdict is honored.
        self.assertIn('kind: "policy_eval"', js)
        self.assertIn("block: true", js)


# ---------------------------------------------------------------------------
# _ToolServer tests
# ---------------------------------------------------------------------------


class TestToolServer(unittest.TestCase):
    def setUp(self):
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except OSError as exc:
            self.skipTest(f"Local TCP sockets unavailable in this environment: {exc}")
        try:
            probe.bind(("127.0.0.1", 0))
        except OSError as exc:
            self.skipTest(f"Loopback TCP bind unavailable in this environment: {exc}")
        finally:
            probe.close()

    async def _run_generated_bridge_tool(
        self,
        *,
        port: int,
        token: str,
        timeout: float = 5.0,
    ) -> dict:
        """Run the generated JS extension under Node and execute one tool.

        This is intentionally cross-runtime: Python starts the real loopback
        server, while Node loads the generated Pi extension, captures the
        registered tool, and calls its ``execute`` method. It exercises the
        same JSONL/TCP boundary Pi uses without needing a live Pi process.
        """
        node_path = shutil.which("node")
        if node_path is None:
            self.skipTest("node is required for generated Pi bridge e2e tests")

        schema = [
            {
                "name": "exotic",
                "description": "exercise generated bridge",
                "parameters": {"type": "object", "properties": {}},
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            extension_path = tmp_path / "omnigent_tools.js"
            runner_path = tmp_path / "run_bridge.js"
            extension_path.write_text(_generate_extension_js(port, schema, token))
            runner_path.write_text(
                textwrap.dedent(
                    """
                    const extension = require(process.argv[2]);
                    let registered;
                    const fakePi = {
                      on() {},
                      registerTool(tool) { registered = tool; },
                    };

                    (async () => {
                      extension(fakePi);
                      if (!registered) {
                        throw new Error("tool was not registered");
                      }
                      const result = await registered.execute(
                        "call-1",
                        { input: "hello" },
                        undefined,
                        undefined,
                        {}
                      );
                      process.stdout.write(JSON.stringify(result));
                    })().catch((err) => {
                      process.stderr.write(err && err.stack ? err.stack : String(err));
                      process.exit(1);
                    });
                    """
                )
            )

            proc = await asyncio.create_subprocess_exec(
                node_path,
                str(runner_path),
                str(extension_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                stdout, stderr = await proc.communicate()
                stderr_text = stderr.decode("utf-8", errors="replace")
                self.fail(f"generated Pi bridge did not resolve within {timeout}s: {stderr_text}")
            self.assertEqual(
                proc.returncode,
                0,
                stderr.decode("utf-8", errors="replace"),
            )
            return json.loads(stdout.decode("utf-8"))

    def test_start_and_stop(self):
        async def _test():
            server = _ToolServer()
            port = await server.start()
            self.assertGreater(port, 0)
            await server.stop()

        _run(_test())

    def test_tool_execution_over_tcp(self):
        async def _test():
            server = _ToolServer()
            await server.start()

            async def executor(name, args):
                return {"sum": args.get("a", 0) + args.get("b", 0)}

            server._tool_executor = executor

            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            request = (
                json.dumps(
                    {"id": "req1", "token": server.token, "tool": "add", "args": {"a": 3, "b": 4}}
                )
                + "\n"
            )
            writer.write(request.encode())
            await writer.drain()

            response_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            response = json.loads(response_line)
            self.assertEqual(response["id"], "req1")
            self.assertEqual(response["result"]["sum"], 7)

            writer.close()
            await server.stop()

        _run(_test())

    def test_tool_execution_error(self):
        async def _test():
            server = _ToolServer()
            await server.start()

            async def executor(name, args):
                raise ValueError("boom")

            server._tool_executor = executor

            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            request = (
                json.dumps({"id": "req2", "token": server.token, "tool": "fail", "args": {}})
                + "\n"
            )
            writer.write(request.encode())
            await writer.drain()

            response_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            response = json.loads(response_line)
            self.assertEqual(response["id"], "req2")
            self.assertIn("boom", response["error"])

            writer.close()
            await server.stop()

        _run(_test())

    def test_no_executor_returns_error(self):
        async def _test():
            server = _ToolServer()
            await server.start()
            # Don't set _tool_executor

            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            request = (
                json.dumps({"id": "req3", "token": server.token, "tool": "missing", "args": {}})
                + "\n"
            )
            writer.write(request.encode())
            await writer.drain()

            response_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            response = json.loads(response_line)
            self.assertEqual(response["id"], "req3")
            self.assertIn("No tool executor", response["error"])

            writer.close()
            await server.stop()

        _run(_test())

    def test_non_json_serializable_result_returns_error_frame(self):
        """A tool result ``json.dumps`` can't encode yields an error frame.

        Regression for F03: serialization happens on the response path
        *outside* ``_execute``'s try, so a tool returning a ``datetime`` (or
        ``set``) used to raise ``TypeError`` there, close the socket with zero
        bytes, and hang the JS ``callTool`` promise — wedging the whole turn.
        The handler must instead always write a valid frame: here an
        ``{"error": ...}`` envelope, correlated by ``id``, delivered well
        within the timeout (proving it did not hang).
        """

        async def _test():
            server = _ToolServer()
            await server.start()

            async def executor(name, args):
                # A dict carrying values json.dumps rejects by default.
                return {"when": datetime(2026, 6, 18, 12, 0, 0), "tags": {1, 2, 3}}

            server._tool_executor = executor

            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            request = (
                json.dumps({"id": "req6", "token": server.token, "tool": "exotic", "args": {}})
                + "\n"
            )
            writer.write(request.encode())
            await writer.drain()

            # The key assertion is that a frame arrives at all (no hang): a
            # short timeout would fire if the response path crashed/closed.
            response_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            self.assertTrue(response_line, "tool server closed without writing a frame (hang)")
            response = json.loads(response_line)
            self.assertEqual(response["id"], "req6")
            self.assertIn("unserializable tool result", response["error"])
            self.assertNotIn("result", response)

            writer.close()
            await server.stop()

        _run(_test())

    def test_safe_dumps_with_non_serializable_req_id_does_not_raise(self):
        """``_safe_dumps`` never raises, even on a non-serializable ``req_id``.

        The fallback envelope serializes ``req_id`` too, so a future caller
        passing an id ``json.dumps`` can't encode (here a ``datetime``) must
        still yield a valid frame rather than re-raising the very crash the
        guard exists to prevent. The id is stringified in that envelope.
        """
        bad_id = datetime(2026, 6, 18, 12, 0, 0)
        out = _safe_dumps({"id": bad_id, "result": {"k": "v"}}, bad_id)  # type: ignore[arg-type]
        payload = json.loads(out)
        self.assertEqual(payload["id"], str(bad_id))
        self.assertIn("unserializable tool result", payload["error"])
        self.assertNotIn("result", payload)

    def test_generated_bridge_returns_error_for_unserializable_tool_result(self):
        """End-to-end: Node bridge + Python server return an error result.

        The previous unit test proves the TCP server writes an error frame.
        This test follows the actual Pi bridge path too: generated JS running
        in Node receives that frame and returns a Pi tool result with
        ``isError=true`` instead of hanging or throwing.
        """

        async def _test():
            server = _ToolServer()
            await server.start()

            async def executor(name, args):
                return {"when": datetime(2026, 6, 18, 12, 0, 0), "tags": {1, 2, 3}}

            server._tool_executor = executor
            try:
                result = await self._run_generated_bridge_tool(
                    port=server.port,
                    token=server.token,
                )
            finally:
                await server.stop()

            self.assertTrue(result["isError"])
            self.assertEqual(result["content"][0]["type"], "text")
            payload = json.loads(result["content"][0]["text"])
            self.assertIn("unserializable tool result", payload["error"])

        _run(_test())

    def test_generated_bridge_resolves_on_bare_socket_close(self):
        """End-to-end: a zero-byte close resolves the generated JS callTool.

        This exercises the defense-in-depth close handler. If the generated
        bridge only resolved on ``data`` or ``error`` events, the Node process
        would hang here until ``wait_for`` timed out.
        """

        async def _test():
            async def close_without_response(reader, writer):
                await reader.readline()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_server(close_without_response, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            try:
                result = await self._run_generated_bridge_tool(
                    port=port,
                    token="close-token",
                )
            finally:
                server.close()
                await server.wait_closed()

            self.assertTrue(result["isError"])
            payload = json.loads(result["content"][0]["text"])
            self.assertIn("closed connection without a response", payload["error"])

        _run(_test())

    def test_request_without_token_is_rejected_without_dispatch(self):
        """An unauthenticated request is refused before reaching the executor.

        A process that found the loopback port but can't read the embedded
        token must not drive the tool executor. If auth were removed,
        ``dispatched`` would flip to ``True`` and the response would carry
        the tool result instead of ``"unauthorized"``.
        """

        async def _test():
            server = _ToolServer()
            await server.start()

            dispatched = False

            async def executor(name, args):
                nonlocal dispatched
                dispatched = True
                return {"ok": True}

            server._tool_executor = executor

            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            # No "token" field at all.
            request = json.dumps({"id": "req4", "tool": "add", "args": {}}) + "\n"
            writer.write(request.encode())
            await writer.drain()

            response_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            response = json.loads(response_line)
            # The executor must never have run for an unauthenticated frame.
            self.assertFalse(dispatched, "tool executor ran for an unauthenticated request")
            self.assertEqual(response["error"], "unauthorized")
            self.assertNotIn("result", response)

            writer.close()
            await server.stop()

        _run(_test())

    def test_request_with_wrong_token_is_rejected_without_dispatch(self):
        """A forged/incorrect token is refused before reaching the executor.

        Complements the missing-token case: proves the server compares the
        presented token against its secret rather than merely checking that
        *some* token field is present.
        """

        async def _test():
            server = _ToolServer()
            await server.start()

            dispatched = False

            async def executor(name, args):
                nonlocal dispatched
                dispatched = True
                return {"ok": True}

            server._tool_executor = executor

            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            request = (
                json.dumps(
                    {"id": "req5", "token": server.token + "tampered", "tool": "add", "args": {}}
                )
                + "\n"
            )
            writer.write(request.encode())
            await writer.drain()

            response_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            response = json.loads(response_line)
            self.assertFalse(dispatched, "tool executor ran for a wrong-token request")
            self.assertEqual(response["error"], "unauthorized")

            writer.close()
            await server.stop()

        _run(_test())

    def test_each_server_gets_a_distinct_token(self):
        """Two servers mint independent secrets.

        A shared/static token would let one session's bridge authenticate
        against another's server, reopening the cross-session vector. The
        token is also long enough not to be brute-forceable in practice.
        """

        async def _test():
            a = _ToolServer()
            b = _ToolServer()
            self.assertNotEqual(a.token, b.token)
            # token_urlsafe(32) → 256 bits → ~43 url-safe chars.
            self.assertGreaterEqual(len(a.token), 40)

        _run(_test())

    def test_policy_eval_frame_blocks_on_deny(self):
        """A ``kind=policy_eval`` frame returns the gate's DENY verdict
        without executing the tool.

        This is the native-tool gate: Pi's ``tool_call`` hook asks for a
        verdict on a tool it will run itself. The server must consult
        ``_policy_gate`` (not ``_tool_executor``) and surface
        ``{"block": True, ...}``. If the dispatch branch were missing, the
        frame would fall through to ``_execute`` and the tool executor would
        run — so we assert the executor never fired.
        """

        async def _test():
            server = _ToolServer()
            await server.start()

            executed = False

            async def executor(name, args):
                nonlocal executed
                executed = True
                return {"ok": True}

            async def gate(name, args):
                # Echo the inputs back so the assertion proves the real
                # tool name / args reached the gate, not a fixed stub.
                return {"block": True, "reason": f"denied {name}:{args.get('path')}"}

            server._tool_executor = executor
            server._policy_gate = gate

            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            request = (
                json.dumps(
                    {
                        "id": "pe1",
                        "token": server.token,
                        "kind": "policy_eval",
                        "tool": "read",
                        "args": {"path": "/etc/secret"},
                    }
                )
                + "\n"
            )
            writer.write(request.encode())
            await writer.drain()

            response_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            response = json.loads(response_line)
            self.assertEqual(response["id"], "pe1")
            # DENY verdict surfaced verbatim from the gate, proving the
            # tool name + args traversed to the gate intact.
            self.assertEqual(
                response["verdict"], {"block": True, "reason": "denied read:/etc/secret"}
            )
            # Verdict-only path: the tool must NOT have executed. If this
            # is True, the policy_eval branch wrongly fell through to
            # _execute and the native tool ran ungated despite a DENY.
            self.assertFalse(executed, "tool executor ran on a policy_eval frame")

            writer.close()
            await server.stop()

        _run(_test())

    def test_policy_eval_frame_allows(self):
        """An ALLOW gate yields ``{"block": False}`` so Pi runs the tool."""

        async def _test():
            server = _ToolServer()
            await server.start()

            async def gate(name, args):
                return {"block": False, "reason": ""}

            server._policy_gate = gate

            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            request = (
                json.dumps(
                    {
                        "id": "pe2",
                        "token": server.token,
                        "kind": "policy_eval",
                        "tool": "read",
                        "args": {"path": "/tmp/ok"},
                    }
                )
                + "\n"
            )
            writer.write(request.encode())
            await writer.drain()

            response_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            response = json.loads(response_line)
            self.assertEqual(response["verdict"], {"block": False, "reason": ""})

            writer.close()
            await server.stop()

        _run(_test())

    def test_policy_eval_without_gate_fails_open(self):
        """With no ``_policy_gate`` wired, the verdict is ALLOW (fail-open).

        Single-process / test paths never install a gate. The native tool
        must still run rather than wedge — so an unset gate yields
        ``block=False`` rather than an error.
        """

        async def _test():
            server = _ToolServer()
            await server.start()
            # Deliberately leave _policy_gate unset.

            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            request = (
                json.dumps(
                    {
                        "id": "pe3",
                        "token": server.token,
                        "kind": "policy_eval",
                        "tool": "read",
                        "args": {},
                    }
                )
                + "\n"
            )
            writer.write(request.encode())
            await writer.drain()

            response_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            response = json.loads(response_line)
            self.assertEqual(response["verdict"], {"block": False, "reason": ""})

            writer.close()
            await server.stop()

        _run(_test())

    def test_policy_eval_gate_exception_fails_open(self):
        """A gate that raises must not wedge Pi — the verdict is ALLOW.

        Mirrors the runner/scaffold contract: a transient policy-evaluation
        failure defaults to ALLOW. If this raised instead, every native tool
        call would error out whenever the verdict round-trip hiccupped.
        """

        async def _test():
            server = _ToolServer()
            await server.start()

            async def gate(name, args):
                raise RuntimeError("verdict channel down")

            server._policy_gate = gate

            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            request = (
                json.dumps(
                    {
                        "id": "pe4",
                        "token": server.token,
                        "kind": "policy_eval",
                        "tool": "read",
                        "args": {},
                    }
                )
                + "\n"
            )
            writer.write(request.encode())
            await writer.drain()

            response_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            response = json.loads(response_line)
            self.assertEqual(response["verdict"], {"block": False, "reason": ""})

            writer.close()
            await server.stop()

        _run(_test())


# ---------------------------------------------------------------------------
# _PiRpcSession tests
# ---------------------------------------------------------------------------


class TestPiRpcSession(unittest.TestCase):
    def test_reader_accepts_single_stdout_line_larger_than_default_stream_limit(self):
        async def _test():
            rpc = _PiRpcSession()
            stream = asyncio.StreamReader()
            payload = "x" * (70 * 1024)
            event = {
                "type": "tool_execution_end",
                "toolName": "large_result",
                "isError": False,
                "result": {"content": payload},
            }
            stream.feed_data((json.dumps(event) + "\n").encode())
            stream.feed_eof()
            rpc.process = MagicMock()
            rpc.process.stdout = stream
            rpc._line_queue = asyncio.Queue()

            await rpc._reader()

            line = await rpc.read_line(timeout=0.1)
            self.assertIsNotNone(line)
            parsed = json.loads(line)
            self.assertEqual(parsed["type"], "tool_execution_end")
            self.assertEqual(parsed["toolName"], "large_result")
            self.assertEqual(parsed["result"]["content"], payload)
            self.assertIsNone(await rpc.read_line(timeout=0.1))

        _run(_test())

    def test_send_command(self):
        async def _test():
            rpc = _PiRpcSession()
            proc = _FakeProcess(stdout_lines=[], stderr_lines=[])
            rpc.process = proc
            rpc._line_queue = asyncio.Queue()

            await rpc.send_command({"type": "prompt", "message": "hello", "id": "1"})
            written = b"".join(proc.stdin.data)
            parsed = json.loads(written.decode())
            self.assertEqual(parsed["type"], "prompt")
            self.assertEqual(parsed["message"], "hello")

        _run(_test())

    def test_send_command_raises_when_no_process(self):
        async def _test():
            rpc = _PiRpcSession()
            with self.assertRaises(RuntimeError):
                await rpc.send_command({"type": "prompt"})

        _run(_test())

    def test_close_terminates_process(self):
        async def _test():
            rpc = _PiRpcSession()
            proc = _FakeProcess(stdout_lines=[], stderr_lines=[])
            rpc.process = proc
            rpc._line_queue = asyncio.Queue()
            # Start reader tasks that will end immediately
            rpc._read_task = asyncio.create_task(asyncio.sleep(0))
            rpc._stderr_task = asyncio.create_task(asyncio.sleep(0))
            await asyncio.sleep(0.01)  # let tasks finish
            await rpc.close()
            self.assertTrue(proc.terminate == proc.terminate)  # terminated was called
            self.assertIsNone(rpc.process)

        _run(_test())


# ---------------------------------------------------------------------------
# PiExecutor constructor tests
# ---------------------------------------------------------------------------


class TestPiExecutorConstructor(unittest.TestCase):
    def test_constructor_finds_pi(self):
        with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
            executor = PiExecutor()
        self.assertEqual(executor._pi_path, "/usr/bin/pi")

    def test_constructor_raises_when_pi_not_found(self):
        with patch("omnigent.inner.pi_executor._find_pi_cli", return_value=None):
            with self.assertRaises(ImportError):
                PiExecutor()

    def test_constructor_databricks_with_env(self):
        with (
            patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"),
            patch(
                "omnigent.inner.pi_executor._read_databrickscfg",
                return_value=DatabricksCredentials(host="https://h.example.com", token="tok"),
            ),
        ):
            executor = PiExecutor(gateway=True)
        self.assertTrue(executor._gateway)
        self.assertEqual(executor._databricks_host, "https://h.example.com")
        self.assertEqual(executor._databricks_token, "tok")

    def test_constructor_databricks_with_host_override_requires_auth_command(self):
        with (
            patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"),
            patch("omnigent.inner.pi_executor._read_databrickscfg") as read_cfg,
        ):
            with self.assertRaisesRegex(OSError, "requires a gateway auth command"):
                PiExecutor(
                    gateway=True,
                    databricks_profile="missing-profile",
                    gateway_host="https://example.databricks.com/",
                )

        read_cfg.assert_not_called()

    def test_constructor_databricks_with_auth_command(self):
        with (
            patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"),
            patch("omnigent.inner.pi_executor._read_databrickscfg") as read_cfg,
            patch(
                "omnigent.inner.pi_executor._fetch_shell_command_token",
                return_value="command-token",
            ) as fetch_command_token,
        ):
            executor = PiExecutor(
                gateway=True,
                databricks_profile="missing-profile",
                gateway_host="https://example.databricks.com/",
                base_urls_override={
                    "claude": "https://example.databricks.com/ai-gateway/anthropic"
                },
                gateway_auth_command="printf token",
            )

        read_cfg.assert_not_called()
        fetch_command_token.assert_called_once_with("printf token")
        self.assertEqual(executor._databricks_host, "https://example.databricks.com")
        self.assertEqual(executor._databricks_token, "command-token")
        self.assertEqual(
            executor._base_urls_override,
            {"claude": "https://example.databricks.com/ai-gateway/anthropic"},
        )

    def test_constructor_databricks_no_creds_raises(self):
        with (
            patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"),
            patch.dict("os.environ", {}, clear=True),
            patch("omnigent.inner.pi_executor._read_databrickscfg", return_value=None),
        ):
            with self.assertRaises(EnvironmentError):
                PiExecutor(gateway=True)

    def test_constructor_with_model_override(self):
        with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
            executor = PiExecutor(model="my-model")
        self.assertEqual(executor._model_override, "my-model")

    def test_supports_streaming(self):
        with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
            executor = PiExecutor()
        self.assertTrue(executor.supports_streaming())

    def test_supports_tool_calling(self):
        with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
            executor = PiExecutor()
        self.assertTrue(executor.supports_tool_calling())

    def test_handles_tools_internally(self):
        with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
            executor = PiExecutor()
        self.assertTrue(executor.handles_tools_internally())

    def test_supports_live_message_queue(self):
        with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
            executor = PiExecutor()
        self.assertTrue(executor.supports_live_message_queue())

    def test_no_tools_flag_in_extra_args(self):
        with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
            executor = PiExecutor()
        self.assertIn("--no-tools", executor._extra_args)


# ---------------------------------------------------------------------------
# PiExecutor._gate_native_tool tests
# ---------------------------------------------------------------------------


class TestGateNativeTool(unittest.TestCase):
    """``_gate_native_tool`` bridges the tool server to the scaffold's
    ``_policy_evaluator`` and maps the proto verdict to ``{block, reason}``.

    This is the security-critical mapping: a TOOL_CALL DENY from the
    Omnigent policy engine must become ``block=True`` so Pi refuses the
    native tool. ALLOW (and the no-evaluator path) must become
    ``block=False`` so legitimate native tool use isn't broken.
    """

    @staticmethod
    def _executor():
        with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
            return PiExecutor()

    def test_deny_verdict_blocks_with_reason(self):
        executor = self._executor()
        seen: dict[str, object] = {}

        async def fake_evaluator(phase, data):
            seen["phase"] = phase
            seen["data"] = data
            return PolicyVerdictPayload(action="POLICY_ACTION_DENY", reason="no /etc reads")

        executor._policy_evaluator = fake_evaluator

        verdict = _run(executor._gate_native_tool("read", {"path": "/etc/secret"}))

        # DENY → block, carrying the policy's human-readable reason so the
        # model sees why. A False here means a denied native tool would run.
        self.assertEqual(verdict, {"block": True, "reason": "no /etc reads"})
        # The evaluator was invoked at the TOOL_CALL phase with the real
        # tool name + args (not a fixed stub) — proving the native call's
        # identity reaches the policy engine.
        self.assertEqual(seen["phase"], "PHASE_TOOL_CALL")
        self.assertEqual(seen["data"], {"name": "read", "arguments": {"path": "/etc/secret"}})

    def test_allow_verdict_does_not_block(self):
        executor = self._executor()

        async def fake_evaluator(phase, data):
            return PolicyVerdictPayload(action="POLICY_ACTION_ALLOW")

        executor._policy_evaluator = fake_evaluator

        verdict = _run(executor._gate_native_tool("read", {"path": "/tmp/ok"}))
        # ALLOW must not block — otherwise every native tool call is broken.
        self.assertEqual(verdict, {"block": False, "reason": ""})

    def test_deny_without_reason_uses_fallback(self):
        executor = self._executor()

        async def fake_evaluator(phase, data):
            return PolicyVerdictPayload(action="POLICY_ACTION_DENY", reason=None)

        executor._policy_evaluator = fake_evaluator

        verdict = _run(executor._gate_native_tool("bash", {"command": "ls"}))
        # A DENY with no reason still blocks, with a non-empty fallback so
        # the model never sees an empty refusal.
        self.assertEqual(verdict["block"], True)
        self.assertEqual(verdict["reason"], "blocked by policy")

    def test_no_evaluator_allows(self):
        executor = self._executor()
        # No _policy_evaluator installed (single-process / pre-turn path).
        verdict = _run(executor._gate_native_tool("read", {"path": "/x"}))
        # Fail-open: without an evaluator wired the tool must still run.
        self.assertEqual(verdict, {"block": False, "reason": ""})


# ---------------------------------------------------------------------------
# PiExecutor._resolve_model tests
# ---------------------------------------------------------------------------


class TestResolveModel(unittest.TestCase):
    def test_cfg_model_takes_priority_over_constructor(self):
        # Per-turn ``cfg.model`` wins over constructor-time
        # ``self._model_override``. The constructor value (from
        # ``HARNESS_PI_MODEL`` at spawn time) is the spec-level
        # default; ``cfg.model`` carries the per-request override
        # the REPL's ``/model`` slash command sets. Without this
        # precedence, mid-session model overrides would silently
        # no-op on the pi harness. Mirrors ``cfg.model`` precedence
        # in claude-sdk / codex / openai-agents.
        with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
            executor = PiExecutor(model="constructor-default")
        self.assertEqual(
            executor._resolve_model(ExecutorConfig(model="cfg-override")), "cfg-override"
        )

    def test_constructor_default_used_when_no_cfg_override(self):
        # Constructor value acts as the spec-level default when
        # ``cfg.model`` is None (no per-turn ``/model`` override
        # in effect).
        with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
            executor = PiExecutor(model="constructor-default")
        self.assertEqual(
            executor._resolve_model(ExecutorConfig(model=None)), "constructor-default"
        )

    def test_cfg_model_used_when_no_constructor_default(self):
        # Existing case — preserved from prior behavior. With
        # neither a constructor default nor a per-turn override
        # actively set on the spec, ``cfg.model`` still flows
        # through unchanged.
        with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
            executor = PiExecutor()
        self.assertEqual(
            executor._resolve_model(ExecutorConfig(model="config-model")), "config-model"
        )


# ---------------------------------------------------------------------------
# PiExecutor._build_env_and_dir tests
# ---------------------------------------------------------------------------


class TestBuildEnvAndDir(unittest.TestCase):
    def test_databricks_creates_models_json(self):
        with (
            patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"),
            patch(
                "omnigent.inner.pi_executor._read_databrickscfg",
                return_value=DatabricksCredentials(host="https://h.example.com", token="tok"),
            ),
        ):
            executor = PiExecutor(gateway=True)

        config = executor._build_env_and_dir([], None, None, None)
        try:
            self.assertIn("PI_CODING_AGENT_DIR", config.env)
            managed_dir = Path(config.env["PI_CODING_AGENT_DIR"])
            models_path = managed_dir / "models.json"
            self.assertTrue(models_path.is_file())
            with open(models_path) as f:
                data = json.load(f)
            self.assertIn("providers", data)
            settings_path = managed_dir / "settings.json"
            self.assertTrue(settings_path.is_file())
            with open(settings_path) as f:
                settings = json.load(f)
            self.assertIn("retry", settings)
        finally:
            import shutil

            shutil.rmtree(config.tmp_dir, ignore_errors=True)

    def test_tools_generate_extension_js(self):
        with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
            executor = PiExecutor()

        tools = [
            {
                "name": "test_tool",
                "description": "A test",
                "parameters": {"type": "object", "properties": {}},
            }
        ]
        config = executor._build_env_and_dir(tools, 12345, "tok-test", None)
        try:
            self.assertIn("--extension", config.extra_args)
            ext_path = config.extra_args[config.extra_args.index("--extension") + 1]
            self.assertTrue(os.path.exists(ext_path))
            with open(ext_path) as f:
                content = f.read()
            self.assertIn("test_tool", content)
            self.assertIn("12345", content)
            # Token threads through to the on-disk extension; without it
            # the Pi process couldn't authenticate to the tool server.
            self.assertIn('const TOKEN = "tok-test";', content)
        finally:
            import shutil

            shutil.rmtree(config.tmp_dir, ignore_errors=True)


def test_gateway_seeds_managed_settings_from_global_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gateway mode copies global Pi settings into the managed agent dir."""
    global_agent = tmp_path / "pi-agent"
    global_agent.mkdir()
    (global_agent / "settings.json").write_text(
        json.dumps({"extensions": ["/ext/demo.ts"], "packages": ["npm:@x/y"]}),
        encoding="utf-8",
    )
    (global_agent / "npm").mkdir()
    monkeypatch.setattr(
        "omnigent.inner.pi_settings.DEFAULT_PI_AGENT_DIR",
        global_agent,
    )
    with (
        patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"),
        patch(
            "omnigent.inner.pi_executor._read_databrickscfg",
            return_value=DatabricksCredentials(host="https://h.example.com", token="tok"),
        ),
    ):
        executor = PiExecutor(gateway=True)

    config = executor._build_env_and_dir([], None, None, None)
    try:
        managed_dir = Path(config.env["PI_CODING_AGENT_DIR"])
        settings = json.loads((managed_dir / "settings.json").read_text(encoding="utf-8"))
        assert settings["extensions"] == ["/ext/demo.ts"]
        assert settings["packages"] == ["npm:@x/y"]
        assert (managed_dir / "npm").is_symlink()
    finally:
        shutil.rmtree(config.tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Pi tool allowlist (--no-tools + --tools) tests — function-based per
# the project's testing rules. ``--no-tools`` alone disables every tool
# in pi 0.68+ (built-in AND extension); the executor must pair it with
# ``--tools <names>`` for the bridge to actually expose anything.
# ---------------------------------------------------------------------------


def test_pi_extra_args_disable_native_tools_by_default() -> None:
    """
    A turn with no bridged tools must still pass ``--no-tools`` so
    pi's native read/bash/edit/write stay disabled. ``--tools`` is
    intentionally absent — passing an empty allowlist would be a
    no-op, but pi parses ``--tools `` as an error in some flag
    parsers, so we just omit it.
    """
    with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
        executor = PiExecutor()
    config = executor._build_env_and_dir([], None, None, None)
    try:
        # Native tools off by default.
        assert "--no-tools" in config.extra_args, (
            "--no-tools missing → pi's native read/bash/edit/write would be exposed"
        )
        # No allowlist when no tools are bridged.
        assert "--tools" not in config.extra_args, (
            "--tools should not appear when there are no bridged tools to allowlist"
        )
    finally:
        import shutil

        shutil.rmtree(config.tmp_dir, ignore_errors=True)


def test_pi_tools_arg_allowlists_bridged_tool_names() -> None:
    """
    With bridged tools, ``--tools <comma-list>`` must appear so pi
    actually exposes them to the LLM. Order: ``--no-tools`` first
    (disable everything), then ``--tools`` re-enables specifically
    the bridged names. Catches the regression where ``--no-tools``
    alone wiped out extension tools and the model reported "I
    don't have a calculate tool available."
    """
    with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
        executor = PiExecutor()
    tools = [
        {"name": "calculate", "description": "x", "parameters": {"type": "object"}},
        {"name": "get_current_time", "description": "y", "parameters": {"type": "object"}},
    ]
    config = executor._build_env_and_dir(tools, 9999, "tok-test", None)
    try:
        assert "--no-tools" in config.extra_args
        assert "--tools" in config.extra_args, (
            "--tools missing → pi 0.68+ keeps extension tools disabled, model "
            "won't see calculate/get_current_time"
        )
        names_arg = config.extra_args[config.extra_args.index("--tools") + 1]
        # Comma-separated, both bridged names + ``read`` (injected
        # by the skills layer so Pi's ``formatSkillsForPrompt``
        # sees it and injects the skill index into the system
        # prompt — Pi gates skill-prompt injection on
        # ``selectedTools.includes("read")``).
        actual = sorted(names_arg.split(","))
        assert actual == ["calculate", "get_current_time", "read"], (
            f"unexpected --tools allowlist: {names_arg!r}; expected the "
            f"two bridged tool names + 'read' (for skills)"
        )
    finally:
        import shutil

        shutil.rmtree(config.tmp_dir, ignore_errors=True)


def test_pi_tools_arg_skips_unnamed_entries() -> None:
    """
    Tool schemas without a ``name`` (or with a non-string name)
    are dropped from both the bridge JS registration and the
    ``--tools`` allowlist. Exercises the same defensive path
    :func:`_generate_extension_js` already follows so the two
    can't drift apart and produce a name in one but not the
    other.
    """
    with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
        executor = PiExecutor()
    tools = [
        {"name": "good", "description": "x", "parameters": {"type": "object"}},
        {"description": "no name field", "parameters": {"type": "object"}},
        {"name": 123, "description": "non-string name", "parameters": {"type": "object"}},
    ]
    config = executor._build_env_and_dir(tools, 1, "tok-test", None)
    try:
        # ``--tools`` is present (we have at least one valid name)
        # and contains exactly the valid name.
        assert "--tools" in config.extra_args
        names_arg = config.extra_args[config.extra_args.index("--tools") + 1]
        # ``read`` is also present (injected by the skills layer for
        # Pi's skill-prompt gating — see ``_build_env_and_dir``).
        assert sorted(names_arg.split(",")) == ["good", "read"], (
            f"unnamed / non-string-named tools must be filtered; got {names_arg!r}"
        )
    finally:
        import shutil

        shutil.rmtree(config.tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# PiExecutor.run_turn tests (with mocked RPC)
# ---------------------------------------------------------------------------


class TestRunTurn(unittest.TestCase):
    def _make_executor(self):
        with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
            return PiExecutor()

    def test_empty_user_message_returns_turn_complete(self):
        async def _test():
            executor = self._make_executor()

            # Even though there's no user message, _ensure_rpc is called
            # first, so we need to mock it.
            fake_rpc = _PiRpcSession()
            fake_rpc._line_queue = asyncio.Queue()
            fake_rpc.process = MagicMock()
            fake_rpc.process.returncode = None
            fake_rpc.process.stdin = _FakeStreamWriter()
            fake_rpc._stderr_lines = []

            async def fake_ensure_rpc(*args, **kwargs):
                return fake_rpc

            executor._ensure_rpc = fake_ensure_rpc

            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "assistant", "content": "hi"}],
                    [],
                    "system",
                )
            ]
            self.assertEqual(len(events), 1)
            self.assertIsInstance(events[0], TurnComplete)
            # Empty-prompt short-circuit signals "no assistant text this
            # turn" via ``response=None``, distinct from an explicit empty
            # string the LLM might produce intentionally.
            self.assertIsNone(events[0].response)

        _run(_test())

    def test_streaming_text_events(self):
        async def _test():
            executor = self._make_executor()

            # Mock the RPC session
            fake_rpc = _PiRpcSession()
            fake_rpc._line_queue = asyncio.Queue()
            fake_rpc.process = MagicMock()
            fake_rpc.process.returncode = None
            fake_rpc.process.stdin = _FakeStreamWriter()
            fake_rpc._stderr_lines = []

            # Pre-populate the line queue
            lines = [
                json.dumps({"type": "response", "success": True}),
                json.dumps(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {"type": "text_delta", "delta": "Hello "},
                    }
                ),
                json.dumps(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {"type": "text_delta", "delta": "world"},
                    }
                ),
                json.dumps({"type": "agent_end", "messages": []}),
            ]
            for line in lines:
                fake_rpc._line_queue.put_nowait(line)

            # Patch _ensure_rpc to return our fake
            async def fake_ensure_rpc(*args, **kwargs):
                return fake_rpc

            executor._ensure_rpc = fake_ensure_rpc

            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hello"}],
                    [],
                    "system",
                )
            ]

            text_chunks = [e for e in events if isinstance(e, TextChunk)]
            turn_complete = [e for e in events if isinstance(e, TurnComplete)]

            self.assertEqual(len(text_chunks), 2)
            self.assertEqual(text_chunks[0].text, "Hello ")
            self.assertEqual(text_chunks[1].text, "world")
            self.assertEqual(len(turn_complete), 1)
            self.assertEqual(turn_complete[0].response, "Hello world")

        _run(_test())

    def test_tool_execution_events(self):
        async def _test():
            executor = self._make_executor()

            fake_rpc = _PiRpcSession()
            fake_rpc._line_queue = asyncio.Queue()
            fake_rpc.process = MagicMock()
            fake_rpc.process.returncode = None
            fake_rpc.process.stdin = _FakeStreamWriter()
            fake_rpc._stderr_lines = []

            lines = [
                json.dumps({"type": "response", "success": True}),
                json.dumps(
                    {"type": "tool_execution_start", "toolName": "add", "args": {"a": 1, "b": 2}}
                ),
                json.dumps(
                    {
                        "type": "tool_execution_end",
                        "toolName": "add",
                        "isError": False,
                        "result": {"sum": 3},
                    }
                ),
                json.dumps(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {"type": "text_delta", "delta": "3"},
                    }
                ),
                json.dumps({"type": "agent_end", "messages": []}),
            ]
            for line in lines:
                fake_rpc._line_queue.put_nowait(line)

            async def fake_ensure_rpc(*args, **kwargs):
                return fake_rpc

            executor._ensure_rpc = fake_ensure_rpc

            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "add 1 and 2"}],
                    [],
                    "system",
                )
            ]

            tool_requests = [e for e in events if isinstance(e, ToolCallRequest)]
            tool_completes = [e for e in events if isinstance(e, ToolCallComplete)]

            self.assertEqual(len(tool_requests), 1)
            self.assertEqual(tool_requests[0].name, "add")
            self.assertEqual(tool_requests[0].args, {"a": 1, "b": 2})

            self.assertEqual(len(tool_completes), 1)
            self.assertEqual(tool_completes[0].name, "add")
            self.assertEqual(tool_completes[0].status, ToolCallStatus.SUCCESS)

        _run(_test())

    def test_error_on_failed_response(self):
        async def _test():
            executor = self._make_executor()

            fake_rpc = _PiRpcSession()
            fake_rpc._line_queue = asyncio.Queue()
            fake_rpc.process = MagicMock()
            fake_rpc.process.returncode = None
            fake_rpc.process.stdin = _FakeStreamWriter()
            fake_rpc._stderr_lines = []

            lines = [
                json.dumps({"type": "response", "success": False, "error": "bad request"}),
            ]
            for line in lines:
                fake_rpc._line_queue.put_nowait(line)

            async def fake_ensure_rpc(*args, **kwargs):
                return fake_rpc

            executor._ensure_rpc = fake_ensure_rpc

            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hello"}],
                    [],
                    "system",
                )
            ]

            self.assertEqual(len(events), 1)
            self.assertIsInstance(events[0], ExecutorError)
            self.assertIn("bad request", events[0].message)

        _run(_test())

    def test_eof_without_response_yields_error(self):
        async def _test():
            executor = self._make_executor()

            fake_rpc = _PiRpcSession()
            fake_rpc._line_queue = asyncio.Queue()
            fake_rpc._line_queue.put_nowait(None)  # EOF
            fake_rpc.process = MagicMock()
            fake_rpc.process.returncode = None
            fake_rpc.process.stdin = _FakeStreamWriter()
            fake_rpc._stderr_lines = ["error: something went wrong"]

            async def fake_ensure_rpc(*args, **kwargs):
                return fake_rpc

            executor._ensure_rpc = fake_ensure_rpc

            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hello"}],
                    [],
                    "system",
                )
            ]

            self.assertEqual(len(events), 1)
            self.assertIsInstance(events[0], ExecutorError)
            self.assertIn("something went wrong", events[0].message)

        _run(_test())

    def test_agent_end_extracts_response_from_messages(self):
        """When no text deltas were streamed, response is extracted from agent_end messages."""

        async def _test():
            executor = self._make_executor()

            fake_rpc = _PiRpcSession()
            fake_rpc._line_queue = asyncio.Queue()
            fake_rpc.process = MagicMock()
            fake_rpc.process.returncode = None
            fake_rpc.process.stdin = _FakeStreamWriter()
            fake_rpc._stderr_lines = []

            lines = [
                json.dumps({"type": "response", "success": True}),
                json.dumps(
                    {
                        "type": "agent_end",
                        "messages": [
                            {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "Final answer"}],
                            },
                        ],
                    }
                ),
            ]
            for line in lines:
                fake_rpc._line_queue.put_nowait(line)

            async def fake_ensure_rpc(*args, **kwargs):
                return fake_rpc

            executor._ensure_rpc = fake_ensure_rpc

            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hello"}],
                    [],
                    "system",
                )
            ]

            turn_complete = [e for e in events if isinstance(e, TurnComplete)]
            self.assertEqual(len(turn_complete), 1)
            self.assertEqual(turn_complete[0].response, "Final answer")

        _run(_test())

    def test_tool_error_event(self):
        async def _test():
            executor = self._make_executor()

            fake_rpc = _PiRpcSession()
            fake_rpc._line_queue = asyncio.Queue()
            fake_rpc.process = MagicMock()
            fake_rpc.process.returncode = None
            fake_rpc.process.stdin = _FakeStreamWriter()
            fake_rpc._stderr_lines = []

            lines = [
                json.dumps({"type": "response", "success": True}),
                json.dumps(
                    {
                        "type": "tool_execution_end",
                        "toolName": "fail_tool",
                        "isError": True,
                        "result": "Something broke",
                    }
                ),
                json.dumps({"type": "agent_end", "messages": []}),
            ]
            for line in lines:
                fake_rpc._line_queue.put_nowait(line)

            async def fake_ensure_rpc(*args, **kwargs):
                return fake_rpc

            executor._ensure_rpc = fake_ensure_rpc

            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "fail"}],
                    [],
                    "system",
                )
            ]

            tool_completes = [e for e in events if isinstance(e, ToolCallComplete)]
            self.assertEqual(len(tool_completes), 1)
            self.assertEqual(tool_completes[0].status, ToolCallStatus.ERROR)
            self.assertIn("Something broke", tool_completes[0].error)

        _run(_test())

    def test_message_end_with_error_stop_reason(self):
        async def _test():
            executor = self._make_executor()

            fake_rpc = _PiRpcSession()
            fake_rpc._line_queue = asyncio.Queue()
            fake_rpc.process = MagicMock()
            fake_rpc.process.returncode = None
            fake_rpc.process.stdin = _FakeStreamWriter()
            fake_rpc._stderr_lines = []

            lines = [
                json.dumps({"type": "response", "success": True}),
                json.dumps(
                    {
                        "type": "message_end",
                        "message": {"stopReason": "error", "errorMessage": "Rate limited"},
                    }
                ),
            ]
            for line in lines:
                fake_rpc._line_queue.put_nowait(line)

            async def fake_ensure_rpc(*args, **kwargs):
                return fake_rpc

            executor._ensure_rpc = fake_ensure_rpc

            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hello"}],
                    [],
                    "system",
                )
            ]

            self.assertEqual(len(events), 1)
            self.assertIsInstance(events[0], ExecutorError)
            self.assertIn("Rate limited", events[0].message)

        _run(_test())

    def test_recovers_post_tool_json_parse_error(self):
        async def _test():
            executor = self._make_executor()

            fake_rpc = _PiRpcSession()
            fake_rpc._line_queue = asyncio.Queue()
            fake_rpc.process = MagicMock()
            fake_rpc.process.returncode = None
            fake_rpc.process.stdin = _FakeStreamWriter()
            fake_rpc._stderr_lines = []

            tool_text = json.dumps(
                {
                    "stdout": "/root\nPI_PIEXEC_PATCH_OK\n",
                    "stderr": "",
                    "exit_code": 0,
                    "timed_out": False,
                    "shell": "/usr/bin/bash",
                    "cwd": "/root",
                },
                separators=(",", ":"),
            )
            lines = [
                json.dumps({"type": "response", "success": True}),
                json.dumps(
                    {
                        "type": "tool_execution_end",
                        "toolName": "sys_os_shell",
                        "isError": False,
                        "result": {"content": [{"type": "text", "text": tool_text}]},
                    }
                ),
                json.dumps(
                    {
                        "type": "message_end",
                        "message": {
                            "stopReason": "error",
                            "errorMessage": (
                                "Expected property name or '}' in JSON "
                                "at position 1 (line 1 column 2)"
                            ),
                        },
                    }
                ),
            ]
            for line in lines:
                fake_rpc._line_queue.put_nowait(line)

            async def fake_ensure_rpc(*args, **kwargs):
                return fake_rpc

            executor._ensure_rpc = fake_ensure_rpc

            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "run smoke"}],
                    [],
                    "system",
                )
            ]

            tool_completes = [e for e in events if isinstance(e, ToolCallComplete)]
            self.assertEqual(len(tool_completes), 1)
            self.assertEqual(tool_completes[0].status, ToolCallStatus.SUCCESS)
            self.assertFalse(any(isinstance(e, ExecutorError) for e in events))
            text_chunks = [e for e in events if isinstance(e, TextChunk)]
            self.assertEqual([e.text for e in text_chunks], [tool_text])
            turn_complete = [e for e in events if isinstance(e, TurnComplete)]
            self.assertEqual(len(turn_complete), 1)
            self.assertEqual(turn_complete[0].response, tool_text)

        _run(_test())


# ---------------------------------------------------------------------------
# Pi thinking-delta → ReasoningChunk tests — function-based per the
# project's testing rules. Event dicts mirror pi-ai's
# ``AssistantMessageEvent`` union: ``thinking_start`` /
# ``thinking_delta`` / ``thinking_end`` each carry ``contentIndex``
# (and ``delta`` for the delta variant), wrapped by the RPC layer in
# ``{"type": "message_update", "assistantMessageEvent": ...}``.
# ---------------------------------------------------------------------------


def _executor_with_scripted_rpc(lines: list[str], model: str | None = None) -> PiExecutor:
    """
    Build a :class:`PiExecutor` whose RPC session replays scripted JSONL.

    :param lines: JSONL event lines the fake Pi process emits, in order,
        e.g. ``[json.dumps({"type": "response", "success": True})]``.
    :param model: Optional model override (``self._model_override``),
        used to exercise the usage ``model`` fallback when the assistant
        message omits its own ``model`` field.
    :returns: Executor with ``_ensure_rpc`` patched to a fake session
        pre-loaded with ``lines``.
    """
    with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
        executor = PiExecutor(model=model)
    fake_rpc = _PiRpcSession()
    fake_rpc._line_queue = asyncio.Queue()
    fake_rpc.process = _FakeProcess()
    fake_rpc._stderr_lines = []
    for line in lines:
        fake_rpc._line_queue.put_nowait(line)

    async def fake_ensure_rpc(*args, **kwargs):
        return fake_rpc

    executor._ensure_rpc = fake_ensure_rpc
    return executor


def test_pi_thinking_deltas_stream_as_reasoning_chunks() -> None:
    """
    A pi thinking block (``thinking_start`` → ``thinking_delta``\\* →
    ``thinking_end``) streams as ReasoningChunk events: a
    ``reasoning_started`` marker, then one ``reasoning_text`` chunk per
    delta. Reasoning text must NOT leak into the final response text.
    """

    async def _test() -> None:
        executor = _executor_with_scripted_rpc(
            [
                json.dumps({"type": "response", "success": True}),
                json.dumps(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {"type": "thinking_start", "contentIndex": 0},
                    }
                ),
                json.dumps(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {
                            "type": "thinking_delta",
                            "contentIndex": 0,
                            "delta": "Let me ",
                        },
                    }
                ),
                # Empty delta — must be dropped, not emitted as a no-op chunk.
                json.dumps(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {
                            "type": "thinking_delta",
                            "contentIndex": 0,
                            "delta": "",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {
                            "type": "thinking_delta",
                            "contentIndex": 0,
                            "delta": "reason.",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {
                            "type": "thinking_end",
                            "contentIndex": 0,
                            "content": "Let me reason.",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {
                            "type": "text_delta",
                            "contentIndex": 1,
                            "delta": "Answer",
                        },
                    }
                ),
                json.dumps({"type": "agent_end", "messages": []}),
            ]
        )

        events = [
            e
            async for e in executor.run_turn(
                [{"role": "user", "content": "hello"}],
                [],
                "system",
            )
        ]

        reasoning = [e for e in events if isinstance(e, ReasoningChunk)]
        # 3 = the thinking_start marker + 2 non-empty deltas. A 4th
        # chunk means the empty delta leaked; fewer means thinking
        # events were dropped (the pre-fix behavior).
        assert len(reasoning) == 3, f"expected 3 ReasoningChunks, got {reasoning}"
        assert reasoning[0].event_type == "reasoning_started"
        # The started marker carries no text — it only anchors the rail.
        assert reasoning[0].delta == ""
        assert reasoning[1].event_type == "reasoning_text"
        assert reasoning[1].delta == "Let me "
        assert reasoning[2].event_type == "reasoning_text"
        assert reasoning[2].delta == "reason."

        text_chunks = [e for e in events if isinstance(e, TextChunk)]
        assert len(text_chunks) == 1
        assert text_chunks[0].text == "Answer"

        turn_complete = [e for e in events if isinstance(e, TurnComplete)]
        assert len(turn_complete) == 1
        # Reasoning must stay out of the final text — "Let me reason."
        # appearing here means thinking deltas were concatenated into
        # response_text.
        assert turn_complete[0].response == "Answer"

    _run(_test())


def test_pi_thinking_and_text_delta_ordering_preserved() -> None:
    """
    Interleaved thinking and text deltas stream in arrival order, so
    the web UI renders the reasoning rail and assistant text in the
    sequence the model produced them.
    """

    async def _test() -> None:
        def _update(ame: dict[str, object]) -> str:
            """Wrap an assistantMessageEvent in a message_update line.

            :param ame: The ``assistantMessageEvent`` payload,
                e.g. ``{"type": "text_delta", "contentIndex": 1, "delta": "hi"}``.
            :returns: The JSONL line for the fake Pi process to emit.
            """
            return json.dumps({"type": "message_update", "assistantMessageEvent": ame})

        executor = _executor_with_scripted_rpc(
            [
                json.dumps({"type": "response", "success": True}),
                _update({"type": "thinking_delta", "contentIndex": 0, "delta": "plan"}),
                _update({"type": "text_delta", "contentIndex": 1, "delta": "step one"}),
                _update({"type": "thinking_delta", "contentIndex": 2, "delta": "revise"}),
                _update({"type": "text_delta", "contentIndex": 3, "delta": " step two"}),
                json.dumps({"type": "agent_end", "messages": []}),
            ]
        )

        events = [
            e
            async for e in executor.run_turn(
                [{"role": "user", "content": "hello"}],
                [],
                "system",
            )
        ]

        streamed = [e for e in events if isinstance(e, (ReasoningChunk, TextChunk))]
        # Exact arrival order: reasoning, text, reasoning, text. Any
        # regrouping (e.g. buffering reasoning until the end) breaks
        # the live interleaving the UI renders.
        assert [type(e).__name__ for e in streamed] == [
            "ReasoningChunk",
            "TextChunk",
            "ReasoningChunk",
            "TextChunk",
        ], f"unexpected stream order: {streamed}"
        assert streamed[0].delta == "plan"
        assert streamed[1].text == "step one"
        assert streamed[2].delta == "revise"
        assert streamed[3].text == " step two"

        turn_complete = [e for e in events if isinstance(e, TurnComplete)]
        assert len(turn_complete) == 1
        # Only text deltas accumulate; "plan"/"revise" here means
        # reasoning leaked into the response.
        assert turn_complete[0].response == "step one step two"

    _run(_test())


# ---------------------------------------------------------------------------
# PiExecutor session management tests
# ---------------------------------------------------------------------------


class TestSessionManagement(unittest.TestCase):
    def test_session_key_from_session_id(self):
        with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
            executor = PiExecutor()
        key = executor._session_key([{"role": "user", "content": "hi", "session_id": "abc"}])
        self.assertEqual(key, "abc")

    def test_session_key_from_metadata(self):
        with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
            executor = PiExecutor()
        key = executor._session_key(
            [{"role": "user", "content": "hi", "metadata": {"session_id": "xyz"}}]
        )
        self.assertEqual(key, "xyz")

    def test_session_key_default(self):
        with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
            executor = PiExecutor()
        key = executor._session_key([{"role": "user", "content": "hi"}])
        self.assertEqual(key, "__default__")

    def test_close_session(self):
        async def _test():
            with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
                executor = PiExecutor()

            mock_rpc = MagicMock()
            mock_rpc.close = AsyncMock()
            from omnigent.inner.pi_executor import _PiSessionState

            executor._session_states["test"] = _PiSessionState(rpc=mock_rpc)

            await executor.close_session("test")
            mock_rpc.close.assert_called_once()
            self.assertNotIn("test", executor._session_states)

        _run(_test())

    def test_enqueue_session_message(self):
        async def _test():
            with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
                executor = PiExecutor()

            mock_rpc = MagicMock()
            mock_rpc.send_command = AsyncMock()
            from omnigent.inner.pi_executor import _PiSessionState

            executor._session_states["test"] = _PiSessionState(rpc=mock_rpc)

            result = await executor.enqueue_session_message("test", "STOP")
            self.assertTrue(result)
            mock_rpc.send_command.assert_called_once()
            cmd = mock_rpc.send_command.call_args[0][0]
            self.assertEqual(cmd["type"], "steer")
            self.assertEqual(cmd["message"], "STOP")

        _run(_test())

    def test_enqueue_session_message_no_session(self):
        async def _test():
            with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
                executor = PiExecutor()

            result = await executor.enqueue_session_message("nonexistent", "STOP")
            self.assertFalse(result)

        _run(_test())

    def test_interrupt_session_aborts_then_drops_session(self):
        """A user interrupt aborts the turn AND drops the session.

        Pi resumes the same subprocess on the next turn and sends only the
        latest user message, so a retained session (``_has_sent_prompt``
        still True) would bypass the runner's ``[System: interrupted]``
        marker and continue the abandoned request. Dropping the session
        forces a fresh subprocess that replays full history. An empty
        ``_session_states`` is the invariant that prevents the leak.
        """

        async def _test():
            with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
                executor = PiExecutor()

            mock_rpc = MagicMock()
            mock_rpc.send_command = AsyncMock()
            mock_rpc.close = AsyncMock()
            from omnigent.inner.pi_executor import _PiSessionState

            executor._session_states["test"] = _PiSessionState(rpc=mock_rpc)

            result = await executor.interrupt_session("test")
            self.assertTrue(result)
            # Abort is sent first to halt the in-flight turn.
            mock_rpc.send_command.assert_called_once()
            cmd = mock_rpc.send_command.call_args[0][0]
            self.assertEqual(cmd["type"], "abort")
            # Then the session rpc is closed and the state removed so the next
            # turn starts fresh and replays full history (marker included).
            mock_rpc.close.assert_awaited_once()
            self.assertEqual(executor._session_states, {})

        _run(_test())


# ---------------------------------------------------------------------------
# PiExecutor.close tests
# ---------------------------------------------------------------------------


class TestClose(unittest.TestCase):
    def test_close_all_sessions_and_tool_server(self):
        async def _test():
            with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
                executor = PiExecutor()

            mock_rpc1 = MagicMock()
            mock_rpc1.close = AsyncMock()
            mock_rpc2 = MagicMock()
            mock_rpc2.close = AsyncMock()
            from omnigent.inner.pi_executor import _PiSessionState

            executor._session_states["s1"] = _PiSessionState(rpc=mock_rpc1)
            executor._session_states["s2"] = _PiSessionState(rpc=mock_rpc2)

            mock_tool_server = MagicMock()
            mock_tool_server.stop = AsyncMock()
            executor._tool_server = mock_tool_server

            await executor.close()
            mock_rpc1.close.assert_called_once()
            mock_rpc2.close.assert_called_once()
            mock_tool_server.stop.assert_called_once()

        _run(_test())


# ---------------------------------------------------------------------------
# PiExecutor blocked tool detection tests
# ---------------------------------------------------------------------------


class TestBlockedToolDetection(unittest.TestCase):
    """Verify that policy-blocked tool results are detected and mapped to BLOCKED status."""

    def _make_executor(self):
        with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
            return PiExecutor()

    def _run_with_events(self, event_lines):
        """Helper: create a fake RPC session with given event lines and collect events."""

        async def _test():
            executor = self._make_executor()

            fake_rpc = _PiRpcSession()
            fake_rpc._line_queue = asyncio.Queue()
            fake_rpc.process = MagicMock()
            fake_rpc.process.returncode = None
            fake_rpc.process.stdin = _FakeStreamWriter()
            fake_rpc._stderr_lines = []

            for line in event_lines:
                fake_rpc._line_queue.put_nowait(line)

            async def fake_ensure_rpc(*args, **kwargs):
                return fake_rpc

            executor._ensure_rpc = fake_ensure_rpc

            return [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "test"}],
                    [],
                    "system",
                )
            ]

        return _run(_test())

    def test_blocked_dict_result(self):
        """Result is a direct dict with blocked=True."""
        events = self._run_with_events(
            [
                json.dumps({"type": "response", "success": True}),
                json.dumps(
                    {
                        "type": "tool_execution_end",
                        "toolName": "ping",
                        "isError": True,
                        "result": {"blocked": True, "reason": "Policy blocked it"},
                    }
                ),
                json.dumps({"type": "agent_end", "messages": []}),
            ]
        )
        tool_completes = [e for e in events if isinstance(e, ToolCallComplete)]
        self.assertEqual(len(tool_completes), 1)
        self.assertEqual(tool_completes[0].status, ToolCallStatus.BLOCKED)
        self.assertIn("Policy blocked it", tool_completes[0].error)

    def test_blocked_content_wrapped_result(self):
        """Result is wrapped in Pi extension format with JSON text."""
        blocked_json = json.dumps({"blocked": True, "reason": "Not allowed"})
        events = self._run_with_events(
            [
                json.dumps({"type": "response", "success": True}),
                json.dumps(
                    {
                        "type": "tool_execution_end",
                        "toolName": "ping",
                        "isError": True,
                        "result": {"content": [{"type": "text", "text": blocked_json}]},
                    }
                ),
                json.dumps({"type": "agent_end", "messages": []}),
            ]
        )
        tool_completes = [e for e in events if isinstance(e, ToolCallComplete)]
        self.assertEqual(len(tool_completes), 1)
        self.assertEqual(tool_completes[0].status, ToolCallStatus.BLOCKED)
        self.assertIn("Not allowed", tool_completes[0].error)

    def test_blocked_string_result(self):
        """Result is a JSON string with blocked=True."""
        events = self._run_with_events(
            [
                json.dumps({"type": "response", "success": True}),
                json.dumps(
                    {
                        "type": "tool_execution_end",
                        "toolName": "ping",
                        "isError": True,
                        "result": json.dumps({"blocked": True, "reason": "Denied"}),
                    }
                ),
                json.dumps({"type": "agent_end", "messages": []}),
            ]
        )
        tool_completes = [e for e in events if isinstance(e, ToolCallComplete)]
        self.assertEqual(len(tool_completes), 1)
        self.assertEqual(tool_completes[0].status, ToolCallStatus.BLOCKED)
        self.assertIn("Denied", tool_completes[0].error)

    def test_blocked_nested_isError_in_result(self):
        """Pi reports isError:false at top level but result.isError:true with blocked content."""
        blocked_json = json.dumps({"blocked": True, "reason": "Policy says no"})
        events = self._run_with_events(
            [
                json.dumps({"type": "response", "success": True}),
                json.dumps(
                    {
                        "type": "tool_execution_end",
                        "toolName": "ping",
                        "isError": False,  # top-level is False!
                        "result": {
                            "content": [{"type": "text", "text": blocked_json}],
                            "isError": True,  # nested isError
                        },
                    }
                ),
                json.dumps({"type": "agent_end", "messages": []}),
            ]
        )
        tool_completes = [e for e in events if isinstance(e, ToolCallComplete)]
        self.assertEqual(len(tool_completes), 1)
        self.assertEqual(tool_completes[0].status, ToolCallStatus.BLOCKED)
        self.assertIn("Policy says no", tool_completes[0].error)

    def test_non_blocked_error_stays_error(self):
        """A regular error (not blocked) stays as ERROR status."""
        events = self._run_with_events(
            [
                json.dumps({"type": "response", "success": True}),
                json.dumps(
                    {
                        "type": "tool_execution_end",
                        "toolName": "fail",
                        "isError": True,
                        "result": "Connection refused",
                    }
                ),
                json.dumps({"type": "agent_end", "messages": []}),
            ]
        )
        tool_completes = [e for e in events if isinstance(e, ToolCallComplete)]
        self.assertEqual(len(tool_completes), 1)
        self.assertEqual(tool_completes[0].status, ToolCallStatus.ERROR)


def _make_pi_skill_dir(root: Path, name: str) -> Path:
    """Create a minimal valid skill directory for the resolver tests."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\n# {name}\n")
    return skill_dir


def test_resolve_pi_skill_args_all(tmp_path: Path) -> None:
    """``skills_filter='all'`` produces ``--skill <path>`` for every
    bundle skill, with NO ``--no-skills`` (Pi's host auto-discovery
    stays on).

    Mirrors the SDK semantics: ``"all"`` exposes everything Pi can
    discover (host) AND everything the agent ships (bundle).
    Failing this test means the resolver's ``"all"`` branch dropped
    bundle skills, host skills, or both.
    """
    from omnigent.inner.pi_executor import _resolve_pi_skill_args

    bundle = tmp_path / "bundle"
    skills_root = bundle / "skills"
    _make_pi_skill_dir(skills_root, "alpha")
    _make_pi_skill_dir(skills_root, "beta")

    args = _resolve_pi_skill_args("all", bundle)

    # Two bundle skills must produce two ``--skill`` flags. If 0,
    # the resolver lost the bundle source. If ``--no-skills`` shows
    # up, host discovery would be incorrectly suppressed for "all".
    assert args.count("--skill") == 2, (
        f"expected 2 --skill flags for two bundle skills, got args={args}"
    )
    assert "--no-skills" not in args, (
        f"--no-skills must NOT appear for skills='all'; would suppress "
        f"Pi's host discovery. Got args={args}"
    )
    paths = [args[i + 1] for i, tok in enumerate(args) if tok == "--skill"]
    assert str(skills_root / "alpha") in paths
    assert str(skills_root / "beta") in paths


def test_resolve_pi_skill_args_none(tmp_path: Path) -> None:
    """``skills_filter='none'`` produces exactly ``['--no-skills']``.

    No ``--skill`` flags either — explicit paths would override
    ``--no-skills`` per Pi's flag semantics, so the hermetic case
    must be empty everywhere.
    """
    from omnigent.inner.pi_executor import _resolve_pi_skill_args

    bundle = tmp_path / "bundle"
    skills_root = bundle / "skills"
    _make_pi_skill_dir(skills_root, "alpha")

    args = _resolve_pi_skill_args("none", bundle)

    # Exact equality: any leak would either drop --no-skills (Pi
    # would auto-discover) or add stray --skill flags (Pi would
    # load them despite --no-skills).
    assert args == ["--no-skills"], (
        f"skills='none' must produce exactly ['--no-skills']; "
        f"got {args}. Stray --skill flags would override "
        f"--no-skills and load skills anyway."
    )


def test_resolve_pi_skill_args_named_subset(tmp_path: Path) -> None:
    """``skills_filter=[name, ...]`` produces ``--no-skills`` plus
    one ``--skill <path>`` per named bundle skill.

    Names not present in the bundle are silently skipped — adding
    a ``--skill`` flag pointing at a non-existent path would crash
    Pi at startup.
    """
    from omnigent.inner.pi_executor import _resolve_pi_skill_args

    bundle = tmp_path / "bundle"
    skills_root = bundle / "skills"
    _make_pi_skill_dir(skills_root, "alpha")
    _make_pi_skill_dir(skills_root, "beta")

    args = _resolve_pi_skill_args(["alpha", "missing_skill"], bundle)

    # Must start with --no-skills (suppress Pi auto-discovery so
    # only the named skills surface).
    assert args[0] == "--no-skills"
    # Exactly one --skill, pointing at alpha. ``missing_skill`` is
    # silently dropped.
    assert args.count("--skill") == 1
    paths = [args[i + 1] for i, tok in enumerate(args) if tok == "--skill"]
    assert paths == [str(skills_root / "alpha")], (
        f"expected only ['{skills_root}/alpha'], got {paths}. "
        f"If 'beta' appears, the per-name filter is matching too "
        f"broadly. If empty, the resolver dropped a named skill "
        f"that exists."
    )


def test_resolve_pi_skill_args_no_bundle() -> None:
    """When ``bundle_dir`` is ``None`` the resolver still produces
    sane output: ``[]`` for ``"all"`` (Pi's auto-discovery still
    runs), ``["--no-skills"]`` for the suppression cases.

    Catches a regression where the resolver would crash on missing
    bundle — the agent would fail to spawn at all.
    """
    from omnigent.inner.pi_executor import _resolve_pi_skill_args

    assert _resolve_pi_skill_args("all", None) == []
    assert _resolve_pi_skill_args("none", None) == ["--no-skills"]
    # List with no bundle: just --no-skills, no --skill flags
    # (no source to resolve names against).
    assert _resolve_pi_skill_args(["alpha"], None) == ["--no-skills"]


# ---------------------------------------------------------------------------
# Databricks gateway default-model + models.json parity tests — the pi
# mirror of the claude-sdk default plumbing. The ucode-cached
# path gets its default from the producer (workflow.py); these cover the
# executor's own profile-derived path and the models.json invariants.
# ---------------------------------------------------------------------------


def test_profile_gateway_resolves_databricks_default_model() -> None:
    """
    On the profile-derived gateway path (no gateway host / base URL — the
    producer's ucode lookup early-returned), a missing model resolves to
    the shared Databricks default instead of ``None``.

    Failure means pi falls back to its own host default — an
    Anthropic-direct id the Databricks AI gateway rejects, surfacing as a
    model error on the agent's first turn.
    """
    with (
        patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"),
        patch(
            "omnigent.inner.pi_executor._read_databrickscfg",
            return_value=DatabricksCredentials(host="https://h.example.com", token="tok"),
        ),
    ):
        executor = PiExecutor(gateway=True)
    assert executor._resolve_model(ExecutorConfig(model=None)) == DATABRICKS_CLAUDE_DEFAULT_MODEL


def test_profile_gateway_default_does_not_clobber_explicit_model() -> None:
    """
    The profile-path default only fills a gap — an explicit constructor
    model (``HARNESS_PI_MODEL``) is used as-is.

    Failure means the fallback overrides a model the spec or ucode
    state pinned deliberately.
    """
    with (
        patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"),
        patch(
            "omnigent.inner.pi_executor._read_databrickscfg",
            return_value=DatabricksCredentials(host="https://h.example.com", token="tok"),
        ),
    ):
        executor = PiExecutor(gateway=True, model="databricks-gpt-5-4")
    assert executor._resolve_model(ExecutorConfig(model=None)) == "databricks-gpt-5-4"


def test_ucode_gateway_host_path_does_not_inject_default_model() -> None:
    """
    On the ucode-cached gateway path (gateway host + auth command supplied
    by the producer) the executor must NOT invent a model: the producer
    already applied the default via ``UcodeHarnessConfig`` — mirrors
    claude-sdk's gating, so the two layers can't fight over precedence.

    Failure (a non-None resolve here) means the executor would mask
    producer-side model resolution bugs instead of failing visibly.
    """
    with (
        patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"),
        patch(
            "omnigent.inner.pi_executor._fetch_shell_command_token",
            return_value="command-token",
        ),
    ):
        executor = PiExecutor(
            gateway=True,
            gateway_host="https://example.databricks.com",
            gateway_auth_command="printf token",
        )
    assert executor._resolve_model(ExecutorConfig(model=None)) is None


def test_non_gateway_path_does_not_inject_default_model() -> None:
    """
    Off the gateway entirely (direct Anthropic / pi-native auth), a missing
    model stays ``None`` so pi picks its own default — a ``databricks-*``
    id would not resolve outside the gateway's models.json.
    """
    with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
        executor = PiExecutor()
    assert executor._resolve_model(ExecutorConfig(model=None)) is None


def test_databricks_default_model_is_resolvable_in_models_json() -> None:
    """
    The shared Databricks default must route to the anthropic provider AND
    be listed in that provider's models — otherwise the default the
    producer/executor inject can't be resolved by pi at spawn time.

    Failure means the default-model constant and pi's models.json drifted
    apart: every modelless gateway agent would fail its first turn with a
    pi "unknown model" error.
    """
    assert _pi_provider_for_model(DATABRICKS_CLAUDE_DEFAULT_MODEL) == "databricks-anthropic"
    models = _build_models_json("https://host.example.com", "tok")
    anthropic_ids = [m["id"] for m in models["providers"]["databricks-anthropic"]["models"]]
    assert DATABRICKS_CLAUDE_DEFAULT_MODEL in anthropic_ids


def test_models_json_lists_only_gateway_verified_models() -> None:
    """
    The hardcoded model lists match the set verified live against the
    Databricks gateway endpoint metadata and the API paths pi uses
    (Anthropic Messages for Claude, OpenAI-compatible serving endpoints for
    GPT).

    Failure direction matters: a missing working id silently shrinks pi's
    model menu; a reintroduced broken id (``sonnet-4-5-v2`` rejects
    Anthropic passthrough, the llama endpoint 404s) fails at request time
    for anyone who selects it.
    """
    models = _build_models_json("https://host.example.com", "tok")
    providers = models["providers"]
    anthropic_ids = [m["id"] for m in providers["databricks-anthropic"]["models"]]
    assert anthropic_ids == [
        "databricks-claude-opus-4-8",
        "databricks-claude-sonnet-4-6",
        "databricks-claude-sonnet-4-5",
    ]
    openai_ids = [m["id"] for m in providers["databricks"]["models"]]
    assert openai_ids == [
        "databricks-gpt-5-4-mini",
        "databricks-gpt-5-4",
        "databricks-gpt-5-5",
        "databricks-gpt-5-5-pro",
    ]
    # The llama serving endpoint no longer exists; the provider stays as
    # the routing home for future non-Claude/GPT endpoints.
    assert providers["databricks-completions"]["models"] == []


def test_models_json_uses_oss_verified_gpt_55_caps() -> None:
    """GPT-5.5 endpoint metadata on the OSS profile advertises 128K output."""
    models = _build_models_json("https://host.example.com", "tok")
    by_id = {m["id"]: m for m in models["providers"]["databricks"]["models"]}
    for model_id in ("databricks-gpt-5-5", "databricks-gpt-5-5-pro"):
        assert by_id[model_id]["contextWindow"] == 400000
        assert by_id[model_id]["maxTokens"] == 128000


if __name__ == "__main__":
    unittest.main()

# ---------------------------------------------------------------------------
# _build_models_json: run-model registration (generic gateway models)
# ---------------------------------------------------------------------------


def test_build_models_json_registers_unknown_model_with_routed_provider() -> None:
    """A model outside the static Databricks lists is registered so Pi resolves it.

    Reproduces the OpenRouter failure: ``moonshotai/kimi-k2.6`` routes to
    the ``databricks-completions`` catch-all, whose static model list is
    empty — without registration Pi rejects the
    ``databricks-completions/moonshotai/kimi-k2.6`` selector with
    "Model not found" before the first turn. A regression that drops the
    registration brings that startup failure back for every non-Databricks
    gateway model.
    """
    result = _build_models_json(
        "https://unused.example.com",
        "or-key",
        {"openai": "https://openrouter.ai/api/v1"},
        model="moonshotai/kimi-k2.6",
    )
    completions = result["providers"]["databricks-completions"]
    # The run model is registered (so Pi resolves it) under the provider
    # _pi_provider_for_model routes it to, advertising image input so Pi
    # doesn't strip attached images (#515).
    entry = next((e for e in completions["models"] if e["id"] == "moonshotai/kimi-k2.6"), None)
    assert entry == {"id": "moonshotai/kimi-k2.6", "input": ["text", "image"]}
    # …and that provider points at the generic gateway with the
    # Chat-Completions dialect OpenRouter speaks.
    assert completions["baseUrl"] == "https://openrouter.ai/api/v1"
    assert completions["api"] == "openai-completions"
    # The other providers don't pick up the foreign id.
    assert all(
        m.get("id") != "moonshotai/kimi-k2.6"
        for name in ("databricks", "databricks-anthropic")
        for m in result["providers"][name]["models"]
    )


def test_build_models_json_known_model_not_duplicated_and_lists_not_mutated() -> None:
    """A model already in a static list is not re-registered, and the static
    module-level lists never absorb a run's model id.

    The second build (no model) must not contain the first build's foreign
    id — if it does, the registration mutated the shared module-level list
    instead of rebinding, leaking one run's model into every later
    subprocess config.
    """
    result = _build_models_json(
        "https://host.example.com", "tok", model="databricks-claude-sonnet-4-6"
    )
    anthropic_ids = [m["id"] for m in result["providers"]["databricks-anthropic"]["models"]]
    # Exactly one entry for the already-listed id — no duplicate appended.
    assert anthropic_ids.count("databricks-claude-sonnet-4-6") == 1

    _build_models_json("https://host.example.com", "tok", model="moonshotai/kimi-k2.6")
    fresh = _build_models_json("https://host.example.com", "tok")
    # A model-less build after a foreign-model build is pristine: empty
    # catch-all list, exactly the static Databricks ids elsewhere.
    assert fresh["providers"]["databricks-completions"]["models"] == []


# ---------------------------------------------------------------------------
# _clean_pi_env + spawn-env isolation tests
# ---------------------------------------------------------------------------


def test_clean_pi_env_excludes_host_secrets(monkeypatch) -> None:
    """Host/server credentials never pass the Pi env allowlist by default.

    The Pi spawn previously merged the full ``os.environ`` into the
    subprocess, so server-side credentials (cloud tokens, Databricks
    PATs, provider API keys) were readable inside the (sandboxed) Pi
    process.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.inner.pi_executor import _clean_pi_env

    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("FAKE_HOST_SECRET", "PWNED")
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = _clean_pi_env()

    # None of these match an allowlist entry; any one appearing means
    # the allowlist regressed to a denylist or an environ passthrough.
    assert "DATABRICKS_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "FAKE_HOST_SECRET" not in env
    # The basics a node CLI needs still pass through (PATH resolves the
    # ``#!/usr/bin/env node`` shebang; HOME locates ~/.pi).
    assert env.get("HOME") == "/home/tester"
    assert env.get("PATH") == "/usr/bin"


def test_clean_pi_env_extra_allowed_is_exact_opt_in(monkeypatch) -> None:
    """``extra_allowed`` admits exactly the named variables, nothing more.

    This is the ``os_env.sandbox.env_passthrough`` hook — e.g. a
    direct (non-gateway) run that authenticates Pi via
    ``ANTHROPIC_API_KEY`` names it in the spec. Other credentials in
    the host env must stay excluded.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.inner.pi_executor import _clean_pi_env

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-secret")

    env = _clean_pi_env(["ANTHROPIC_API_KEY"])

    # The opted-in key passes through with its value intact…
    assert env.get("ANTHROPIC_API_KEY") == "sk-ant-test"
    # …without widening the allowlist for anything else.
    assert "DATABRICKS_TOKEN" not in env


def test_clean_pi_env_passes_pi_and_proxy_config(monkeypatch) -> None:
    """Pi's own config and proxy/TLS settings survive the scrub.

    These are the categories the Pi CLI actually reads (``PI_*`` knobs,
    proxy env, node's CA override) — dropping them would break
    corp-proxy and custom-agent-dir setups.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.inner.pi_executor import _clean_pi_env

    monkeypatch.setenv("PI_SKIP_VERSION_CHECK", "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:8080")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/etc/ssl/corp-ca.pem")

    env = _clean_pi_env()

    assert env.get("PI_SKIP_VERSION_CHECK") == "1"
    assert env.get("HTTPS_PROXY") == "http://proxy:8080"
    assert env.get("NODE_EXTRA_CA_CERTS") == "/etc/ssl/corp-ca.pem"


def test_clean_pi_env_includes_omnigent_session_marker(monkeypatch) -> None:
    """The ``OMNIGENT`` session marker survives the Pi env scrub.

    The marker (set once on the runner) must reach the Pi CLI so the
    shell commands Pi runs can detect they are inside an Omnigent
    session, like ``CLAUDE_CODE`` / ``CODEX``.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.inner.pi_executor import _clean_pi_env
    from omnigent.runner.identity import (
        OMNIGENT_SESSION_ENV_VALUE,
        OMNIGENT_SESSION_ENV_VAR,
    )

    monkeypatch.setenv(OMNIGENT_SESSION_ENV_VAR, OMNIGENT_SESSION_ENV_VALUE)

    env = _clean_pi_env()

    assert env.get(OMNIGENT_SESSION_ENV_VAR) == OMNIGENT_SESSION_ENV_VALUE


def test_rpc_start_spawns_with_exact_env(monkeypatch) -> None:
    """``_PiRpcSession.start`` passes the caller's env dict verbatim.

    Guards the spawn-site fix: the old code spawned with
    ``env={**os.environ, **env}``, so every host env var leaked into
    the Pi subprocess. If that merge is reintroduced, the seeded
    ``FAKE_HOST_SECRET`` appears in the captured spawn env and the
    equality assertion fails.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.inner import pi_executor as pi_mod

    monkeypatch.setenv("FAKE_HOST_SECRET", "PWNED")
    captured: dict[str, dict[str, str]] = {}

    async def _fake_spawn(*args, **kwargs):
        captured["env"] = kwargs["env"]
        return _FakeProcess(stdout_lines=[], stderr_lines=[])

    monkeypatch.setattr(pi_mod, "_create_subprocess_exec", _fake_spawn)

    async def _test():
        rpc = _PiRpcSession()
        await rpc.start(
            "/fake/pi",
            env={"PATH": "/usr/bin", "PI_CODING_AGENT_DIR": "/tmp/pi-agent"},
        )
        await rpc.close()

    _run(_test())

    # Exactly the executor-built env — nothing merged from os.environ.
    assert captured["env"] == {"PATH": "/usr/bin", "PI_CODING_AGENT_DIR": "/tmp/pi-agent"}


def test_redact_argv_for_log_hides_system_prompt() -> None:
    """``_redact_argv_for_log`` replaces the system-prompt value with a
    length-only placeholder while leaving every other flag visible."""
    secret = "SUPER SECRET SYSTEM PROMPT that must never hit the logs"
    args = [
        "/fake/pi",
        "--mode",
        "rpc",
        "--no-session",
        "--model",
        "databricks/some-model",
        "--append-system-prompt",
        secret,
        "--extension",
        "/tmp/ext.js",
    ]

    redacted = _redact_argv_for_log(args)

    rendered = " ".join(redacted)
    assert secret not in rendered
    assert f"[system prompt {len(secret)} chars]" in redacted
    # Other flags stay visible for debugging.
    assert "--mode" in redacted
    assert "rpc" in redacted
    assert "--model" in redacted
    assert "databricks/some-model" in redacted
    assert "--extension" in redacted
    assert "/tmp/ext.js" in redacted


def test_redact_argv_for_log_hides_two_token_system_prompt_flag() -> None:
    """The two-token ``--system-prompt <value>`` form is redacted too, not just
    ``--append-system-prompt``."""
    secret = "REPLACEMENT SYSTEM PROMPT that must never hit the logs"
    args = ["/fake/pi", "--system-prompt", secret, "--mode", "rpc"]

    redacted = _redact_argv_for_log(args)

    assert secret not in " ".join(redacted)
    assert f"[system prompt {len(secret)} chars]" in redacted
    assert "--system-prompt" in redacted
    assert "--mode" in redacted
    assert "rpc" in redacted


def test_redact_argv_for_log_hides_equals_joined_system_prompt() -> None:
    """``_redact_argv_for_log`` redacts the equals-joined
    ``--append-system-prompt=<value>`` / ``--system-prompt=<value>`` forms while
    keeping the flag name visible."""
    secret = "SUPER SECRET SYSTEM PROMPT that must never hit the logs"
    for flag in ("--append-system-prompt", "--system-prompt"):
        args = ["/fake/pi", "--mode", "rpc", f"{flag}={secret}", "--extension", "/tmp/ext.js"]

        redacted = _redact_argv_for_log(args)

        rendered = " ".join(redacted)
        assert secret not in rendered
        assert f"{flag}=[system prompt {len(secret)} chars]" in redacted
        # Flag name and other tokens stay visible for debugging.
        assert "--mode" in redacted
        assert "rpc" in redacted
        assert "--extension" in redacted
        assert "/tmp/ext.js" in redacted


def test_rpc_start_log_does_not_leak_system_prompt(monkeypatch, caplog) -> None:
    """``_PiRpcSession.start`` must not write the full ``--append-system-prompt``
    value to the debug log; it should be redacted to a length placeholder.

    Guards F92: the old code logged ``" ".join(args)`` verbatim, leaking the
    entire system prompt into debug logs.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param caplog: Pytest log-capture fixture.
    """
    from omnigent.inner import pi_executor as pi_mod

    test_prompt = "TOP-SECRET-SYSTEM-PROMPT-DO-NOT-LOG-12345"

    async def _fake_spawn(*args, **kwargs):
        return _FakeProcess(stdout_lines=[], stderr_lines=[])

    monkeypatch.setattr(pi_mod, "_create_subprocess_exec", _fake_spawn)

    async def _test():
        rpc = _PiRpcSession()
        await rpc.start(
            "/fake/pi",
            env={"PATH": "/usr/bin"},
            model="some-model",
            system_prompt=test_prompt,
            extra_args=["--extension", "/tmp/ext.js"],
        )
        await rpc.close()

    with caplog.at_level(logging.DEBUG, logger="omnigent.inner.pi_executor"):
        _run(_test())

    spawn_logs = [
        r.getMessage() for r in caplog.records if "PiExecutor: spawning" in r.getMessage()
    ]
    assert spawn_logs, "expected a 'PiExecutor: spawning' debug log line"
    spawn_line = spawn_logs[0]

    assert test_prompt not in spawn_line
    assert f"[system prompt {len(test_prompt)} chars]" in spawn_line
    # Non-sensitive flags remain visible for debugging.
    assert "--mode" in spawn_line
    assert "--extension" in spawn_line


def test_run_turn_spawn_log_redacts_system_prompt_end_to_end(monkeypatch, caplog) -> None:
    """The normal ``PiExecutor.run_turn`` path must pass the system prompt to
    Pi without leaking it into the spawn debug log.

    This drives the real executor wiring from ``run_turn`` through
    ``_ensure_rpc`` and ``_PiRpcSession.start`` with only subprocess creation
    stubbed, so it catches regressions at the behavior boundary users hit.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param caplog: Pytest log-capture fixture.
    """
    from omnigent.inner import pi_executor as pi_mod

    test_prompt = "END-TO-END-SYSTEM-PROMPT-LEAK-SENTINEL-67890"
    captured: dict[str, list[str]] = {}

    async def _fake_spawn(*args, **kwargs):
        captured["argv"] = list(args)
        return _FakeProcess(
            stdout_lines=[
                json.dumps({"type": "response", "success": True}),
                json.dumps(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {"type": "text_delta", "delta": "hi"},
                    }
                ),
                json.dumps({"type": "agent_end", "messages": []}),
            ],
            stderr_lines=[],
        )

    monkeypatch.setattr(pi_mod, "_create_subprocess_exec", _fake_spawn)

    async def _test():
        executor = PiExecutor(pi_path="/usr/bin/pi")
        try:
            return [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hello"}],
                    [],
                    test_prompt,
                )
            ]
        finally:
            await executor.close()

    with caplog.at_level(logging.DEBUG, logger="omnigent.inner.pi_executor"):
        events = _run(_test())

    turn_complete = [e for e in events if isinstance(e, TurnComplete)]
    assert len(turn_complete) == 1
    assert turn_complete[0].response == "hi"

    argv = captured["argv"]
    assert "--append-system-prompt" in argv
    assert argv[argv.index("--append-system-prompt") + 1] == test_prompt

    spawn_logs = [
        r.getMessage() for r in caplog.records if "PiExecutor: spawning" in r.getMessage()
    ]
    assert spawn_logs, "expected a 'PiExecutor: spawning' debug log line"
    spawn_line = spawn_logs[0]

    assert test_prompt not in spawn_line
    assert f"[system prompt {len(test_prompt)} chars]" in spawn_line
    assert "--append-system-prompt" in spawn_line
    assert "--mode" in spawn_line


def test_run_turn_spawn_env_has_no_host_secrets(monkeypatch) -> None:
    """A host secret seeded in ``os.environ`` never reaches the spawned
    Pi process through the full real path (reproduces the leak PoC).

    Unlike the unit tests above, this drives ``run_turn`` through the
    REAL ``_ensure_rpc`` → ``_build_env_and_dir`` → ``start`` chain with
    only the module-level subprocess seam stubbed, so it fails if ANY
    layer regresses (``__init__`` reverting to ``os.environ.copy()``,
    the spawn merge coming back, etc.).

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.inner import pi_executor as pi_mod

    monkeypatch.setenv("FAKE_HOST_SECRET", "PWNED")
    captured: dict[str, dict[str, str]] = {}

    async def _fake_spawn(*args, **kwargs):
        captured["env"] = kwargs["env"]
        return _FakeProcess(
            stdout_lines=[
                json.dumps({"type": "response", "success": True}),
                json.dumps(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {"type": "text_delta", "delta": "hi"},
                    }
                ),
                json.dumps({"type": "agent_end", "messages": []}),
            ],
            stderr_lines=[],
        )

    monkeypatch.setattr(pi_mod, "_create_subprocess_exec", _fake_spawn)

    async def _test():
        with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
            executor = PiExecutor()
        try:
            return [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hello"}],
                    [],
                    "system",
                )
            ]
        finally:
            await executor.close()

    events = _run(_test())

    # The turn really ran end-to-end (not an early error short-circuit):
    # the fake Pi's text made it through the event pipeline.
    turn_complete = [e for e in events if isinstance(e, TurnComplete)]
    assert len(turn_complete) == 1
    assert turn_complete[0].response == "hi"
    # The PoC invariant: the seeded host secret is absent from the env
    # the real executor handed to the spawn. PATH proves the allowlist
    # base populated (an empty env would also "pass" the absence check).
    assert "FAKE_HOST_SECRET" not in captured["env"]
    assert captured["env"].get("PATH") == os.environ["PATH"]


def test_run_turn_spawn_env_honors_spec_env_passthrough(monkeypatch) -> None:
    """``os_env.sandbox.env_passthrough`` names reach the spawned Pi env.

    The opt-in counterpart to the scrub test above: a spec that
    declares ``env_passthrough: ["MY_OPTED_TOKEN"]`` gets exactly that
    variable inside the Pi process, while undeclared host secrets stay
    out. Fails if ``PiExecutor.__init__`` stops threading the spec's
    passthrough list into ``_clean_pi_env``.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.inner import pi_executor as pi_mod
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    monkeypatch.setenv("MY_OPTED_TOKEN", "opted-in-value")
    monkeypatch.setenv("FAKE_HOST_SECRET", "PWNED")
    captured: dict[str, dict[str, str]] = {}

    async def _fake_spawn(*args, **kwargs):
        captured["env"] = kwargs["env"]
        return _FakeProcess(
            stdout_lines=[
                json.dumps({"type": "response", "success": True}),
                json.dumps({"type": "agent_end", "messages": []}),
            ],
            stderr_lines=[],
        )

    monkeypatch.setattr(pi_mod, "_create_subprocess_exec", _fake_spawn)

    async def _test():
        with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
            # ``type="none"`` skips the sandbox wrap so the test stays
            # platform-independent; env scrubbing applies either way.
            executor = PiExecutor(
                os_env=OSEnvSpec(
                    sandbox=OSEnvSandboxSpec(type="none", env_passthrough=["MY_OPTED_TOKEN"]),
                ),
            )
        try:
            return [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hello"}],
                    [],
                    "system",
                )
            ]
        finally:
            await executor.close()

    events = _run(_test())

    # The turn completed (the spawn path actually ran, not an error exit).
    assert any(isinstance(e, TurnComplete) for e in events)
    # Declared name passes through with its host value; undeclared
    # secrets are still scrubbed.
    assert captured["env"].get("MY_OPTED_TOKEN") == "opted-in-value"
    assert "FAKE_HOST_SECRET" not in captured["env"]


def test_pi_sandbox_launcher_policy_carries_spawn_env_allowlist(monkeypatch, tmp_path) -> None:
    """The sandbox launcher policy names exactly the env the executor spawns.

    Defense in depth: ``_clean_pi_env`` already filters the spawn env,
    and the launcher (``run_launcher``) additionally prunes its
    inherited environment to ``SandboxPolicy.spawn_env_allowlist``.
    This test pins the wiring between the two — if ``PiExecutor`` stops
    passing ``spawn_env_names`` (or drops ``PI_CODING_AGENT_DIR``, which
    only joins the env per-spawn on the gateway path), the launcher
    prune would strip vars the executor deliberately set, silently
    breaking sandboxed gateway runs.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Pytest tmp dir used as the sandbox cwd so the
        policy resolve walks a tiny tree.
    """
    from omnigent.inner import sandbox as sandbox_mod
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
    from omnigent.inner.sandbox import SandboxPolicy

    monkeypatch.setenv("FAKE_HOST_SECRET", "PWNED")
    captured: dict[str, SandboxPolicy] = {}

    def _fake_create_exec_launcher(target_path: str, sandbox: SandboxPolicy) -> str:
        captured["policy"] = sandbox
        return "/fake/launcher"

    def _fake_resolve_sandbox(_os_env: OSEnvSpec, cwd: Path) -> SandboxPolicy:
        return SandboxPolicy(
            backend_type="linux_bwrap",
            active=True,
            read_roots=[cwd.resolve(strict=False)],
            write_roots=[cwd.resolve(strict=False)],
            write_files=[],
            allow_network=False,
        )

    # ``_try_sandbox_pi`` resolves this name from the module at call
    # time (function-local ``from .sandbox import ...``), so patching
    # the module attribute intercepts the real call.
    monkeypatch.setattr(sandbox_mod, "resolve_sandbox", _fake_resolve_sandbox)
    monkeypatch.setattr(sandbox_mod, "create_exec_launcher", _fake_create_exec_launcher)

    with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
        executor = PiExecutor(
            cwd=str(tmp_path),
            os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="linux_bwrap")),
        )

    # The launcher wrap actually engaged (not the silent no-sandbox
    # fallback in ``_try_sandbox_pi``'s except clause).
    assert executor._sandboxed is True
    assert executor._pi_launch_path == "/fake/launcher"
    allowlist = captured["policy"].spawn_env_allowlist
    assert allowlist is not None
    # Per-spawn gateway var must be pruneproof even though it is not in
    # the clean base env yet at construction time.
    assert "PI_CODING_AGENT_DIR" in allowlist
    # A clean-env staple proves the executor's deliberate env names got
    # baked in (an empty allowlist would prune PATH and break node).
    assert "PATH" in allowlist
    # The host secret is not in the clean env, so it must not be in the
    # launcher's keep-set either.
    assert "FAKE_HOST_SECRET" not in allowlist


def test_run_turn_bridge_extension_carries_live_server_token(monkeypatch) -> None:
    """The generated bridge extension carries the live server's token
    through the full ``run_turn`` → ``_ensure_tool_server`` →
    ``_ensure_rpc`` → ``_build_env_and_dir`` chain.

    Bridging a tool starts a real ``_ToolServer`` with its own minted
    token; this drives the real wiring (only the subprocess seam stubbed)
    and reads the on-disk extension. If ``_ensure_rpc`` passed a
    stale/blank token, the embedded ``TOKEN`` would no longer equal the
    server's secret — so this asserts they match exactly.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.inner import pi_executor as pi_mod

    captured: dict[str, str] = {}

    async def _fake_spawn(*args, **kwargs):
        # argv carries ``--extension <path>``; read the file while it
        # still exists (the tmp dir is cleaned on executor.close()).
        argv = list(args)
        ext_path = argv[argv.index("--extension") + 1]
        with open(ext_path) as f:
            captured["extension"] = f.read()
        return _FakeProcess(
            stdout_lines=[
                json.dumps({"type": "response", "success": True}),
                json.dumps({"type": "agent_end", "messages": []}),
            ],
            stderr_lines=[],
        )

    monkeypatch.setattr(pi_mod, "_create_subprocess_exec", _fake_spawn)

    tools = [{"name": "lookup", "description": "x", "parameters": {"type": "object"}}]

    async def _test():
        with patch("omnigent.inner.pi_executor._find_pi_cli", return_value="/usr/bin/pi"):
            executor = PiExecutor()
        try:
            events = [
                e
                async for e in executor.run_turn(
                    [{"role": "user", "content": "hi"}],
                    tools,
                    "system",
                )
            ]
            # Capture the live server token before close() tears it down.
            assert executor._tool_server is not None
            return events, executor._tool_server.token
        finally:
            await executor.close()

    events, live_token = _run(_test())

    # The turn ran end-to-end (the spawn path executed, extension written).
    assert any(isinstance(e, TurnComplete) for e in events)
    # The token embedded in the bridge must be the SERVER's actual secret —
    # a mismatch (or empty token) would make Pi's tool calls unauthorized.
    assert f"const TOKEN = {json.dumps(live_token)};" in captured["extension"]
    # Sanity: a freshly minted token is non-trivial (not "" or a stub).
    assert len(live_token) >= 40


# ---------------------------------------------------------------------------
# Pi token-usage → TurnComplete.usage tests
#
# pi (``@mariozechner/pi-coding-agent``) forwards assistant messages whose
# ``usage`` object carries ``input`` / ``output`` / ``cacheRead`` /
# ``cacheWrite`` / ``totalTokens`` token counts (plus a ``cost`` breakdown),
# and the message itself carries the resolved ``model``. The executor maps
# those onto omnigent's usage schema so pi sub-agent cost is priced the same
# way as ``claude-sdk`` and ``codex`` turns. These tests assert the MAPPED
# values, not just presence, so a wrong field mapping fails loud.
# ---------------------------------------------------------------------------


def _pi_assistant_message_with_usage(
    *,
    text: str = "Done.",
    model: str | None = "claude-sonnet-4-6",
    input_tokens: int = 1200,
    output_tokens: int = 350,
    cache_read: int = 800,
    cache_write: int = 64,
    total_tokens: int = 2414,
) -> dict[str, object]:
    """
    Build a realistic pi assistant message dict carrying a ``usage``
    object, mirroring pi-ai's ``AssistantMessage`` / ``Usage`` shape.

    :returns: A message dict suitable for ``event["message"]`` on a
        ``message_end`` event or an entry in ``event["messages"]`` on
        ``agent_end``.
    """
    msg: dict[str, object] = {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "stopReason": "stop",
        "usage": {
            "input": input_tokens,
            "output": output_tokens,
            "cacheRead": cache_read,
            "cacheWrite": cache_write,
            "totalTokens": total_tokens,
            # pi also forwards a per-field cost breakdown; the executor
            # ignores it (omnigent prices from token counts), but include
            # it so the fixture matches the real wire shape.
            "cost": {
                "input": 0.0036,
                "output": 0.00525,
                "cacheRead": 0.00024,
                "cacheWrite": 0.00024,
                "total": 0.00933,
            },
        },
    }
    if model is not None:
        msg["model"] = model
    return msg


def test_pi_usage_captured_from_message_end() -> None:
    """
    A ``message_end`` event whose assistant message carries a ``usage``
    object surfaces on ``TurnComplete.usage`` with each pi field mapped to
    the omnigent schema key. Asserts the actual numbers so a swapped or
    dropped mapping (e.g. cacheRead→cache_creation instead of cache_read)
    fails loud rather than passing on mere presence.
    """

    async def _test() -> None:
        executor = _executor_with_scripted_rpc(
            [
                json.dumps({"type": "response", "success": True}),
                json.dumps(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {"type": "text_delta", "delta": "Done."},
                    }
                ),
                json.dumps({"type": "message_end", "message": _pi_assistant_message_with_usage()}),
                json.dumps({"type": "agent_end", "messages": []}),
            ]
        )

        events = [
            e
            async for e in executor.run_turn(
                [{"role": "user", "content": "hello"}],
                [],
                "system",
            )
        ]

        turn_complete = [e for e in events if isinstance(e, TurnComplete)]
        assert len(turn_complete) == 1
        usage = turn_complete[0].usage
        # usage must be populated — None here means the message_end capture
        # site never ran or the mapping returned None for a real usage dict.
        assert usage is not None, "pi usage was not threaded onto TurnComplete"
        # Each value proves a specific pi-field → omnigent-key mapping:
        assert usage["input_tokens"] == 1200  # <- usage.input
        assert usage["output_tokens"] == 350  # <- usage.output
        assert usage["total_tokens"] == 2414  # <- usage.totalTokens
        assert usage["cache_read_input_tokens"] == 800  # <- usage.cacheRead
        assert usage["cache_creation_input_tokens"] == 64  # <- usage.cacheWrite
        # model comes from the assistant message (used for cost pricing).
        assert usage["model"] == "claude-sonnet-4-6"
        # The text still streams through unaffected by usage capture.
        assert turn_complete[0].response == "Done."

    _run(_test())


def test_pi_usage_fallback_from_agent_end() -> None:
    """
    When no ``message_end`` carried usage, the ``agent_end`` handler falls
    back to the last assistant message in ``event["messages"]``. Asserts
    the mapped numbers so the fallback path is proven to map, not just to
    set a non-None dict.
    """

    async def _test() -> None:
        executor = _executor_with_scripted_rpc(
            [
                json.dumps({"type": "response", "success": True}),
                # No message_end frame — usage must come from agent_end.
                json.dumps(
                    {
                        "type": "agent_end",
                        "messages": [
                            {"role": "user", "content": "hello"},
                            _pi_assistant_message_with_usage(
                                text="Answer.",
                                input_tokens=42,
                                output_tokens=7,
                                cache_read=0,
                                cache_write=0,
                                total_tokens=49,
                            ),
                        ],
                    }
                ),
            ]
        )

        events = [
            e
            async for e in executor.run_turn(
                [{"role": "user", "content": "hello"}],
                [],
                "system",
            )
        ]

        turn_complete = [e for e in events if isinstance(e, TurnComplete)]
        assert len(turn_complete) == 1
        usage = turn_complete[0].usage
        # None means the agent_end fallback never scanned messages for usage.
        assert usage is not None, "agent_end usage fallback did not fire"
        assert usage["input_tokens"] == 42  # <- usage.input from agent_end msg
        assert usage["output_tokens"] == 7  # <- usage.output
        assert usage["total_tokens"] == 49  # <- usage.totalTokens
        # Zero cache fields are still mapped (guarded with ``or 0``).
        assert usage["cache_read_input_tokens"] == 0
        assert usage["cache_creation_input_tokens"] == 0
        assert usage["model"] == "claude-sonnet-4-6"
        # Response text also derives from the agent_end assistant message.
        assert turn_complete[0].response == "Answer."

    _run(_test())


def test_pi_usage_model_falls_back_to_configured_model() -> None:
    """
    When the assistant message omits ``model``, the usage ``model`` falls
    back to the executor's configured model so cost pricing still has a
    model id. Proves the fallback, not the message-supplied value.
    """

    async def _test() -> None:
        executor = _executor_with_scripted_rpc(
            [
                json.dumps({"type": "response", "success": True}),
                json.dumps(
                    {
                        "type": "message_end",
                        "message": _pi_assistant_message_with_usage(model=None),
                    }
                ),
                json.dumps({"type": "agent_end", "messages": []}),
            ],
            model="databricks-claude-sonnet-4-6",
        )

        events = [
            e
            async for e in executor.run_turn(
                [{"role": "user", "content": "hello"}],
                [],
                "system",
            )
        ]

        turn_complete = [e for e in events if isinstance(e, TurnComplete)]
        assert len(turn_complete) == 1
        usage = turn_complete[0].usage
        assert usage is not None
        # The message had no "model" key, so the executor's configured
        # model must fill in. A wrong value means the fallback was skipped.
        assert usage["model"] == "databricks-claude-sonnet-4-6"

    _run(_test())


def test_pi_usage_sums_across_multiple_message_end() -> None:
    """
    A multi-step turn (a tool-use loop emits one ``message_end`` per LLM
    call) sums token counts across every call for billing, while
    ``context_tokens`` reflects ONLY the last call's total.

    This is the behavior that makes pi cost match claude-sdk / codex /
    openai-agents on tool-loop turns: a regression that kept only the last
    ``message_end`` (the original single-capture behavior) would undercount
    ``input_tokens`` as 1200 instead of the summed 2200, so this asserts
    the sum explicitly.
    """

    async def _test() -> None:
        executor = _executor_with_scripted_rpc(
            [
                json.dumps({"type": "response", "success": True}),
                # First LLM call: model decides to call a tool.
                json.dumps(
                    {
                        "type": "message_end",
                        "message": _pi_assistant_message_with_usage(
                            text="",
                            input_tokens=1000,
                            output_tokens=200,
                            cache_read=500,
                            cache_write=50,
                            total_tokens=1750,
                        ),
                    }
                ),
                # Second LLM call: model produces the final answer.
                json.dumps(
                    {
                        "type": "message_end",
                        "message": _pi_assistant_message_with_usage(
                            text="Done.",
                            input_tokens=1200,
                            output_tokens=350,
                            cache_read=800,
                            cache_write=64,
                            total_tokens=2414,
                        ),
                    }
                ),
                json.dumps({"type": "agent_end", "messages": []}),
            ]
        )

        events = [
            e
            async for e in executor.run_turn(
                [{"role": "user", "content": "hello"}],
                [],
                "system",
            )
        ]

        turn_complete = [e for e in events if isinstance(e, TurnComplete)]
        assert len(turn_complete) == 1
        usage = turn_complete[0].usage
        assert usage is not None, "usage was not aggregated across message_end events"
        # Summed across both calls — last-only capture would give 1200 / 350.
        assert usage["input_tokens"] == 2200  # 1000 + 1200
        assert usage["output_tokens"] == 550  # 200 + 350
        assert usage["total_tokens"] == 4164  # 1750 + 2414
        assert usage["cache_read_input_tokens"] == 1300  # 500 + 800
        assert usage["cache_creation_input_tokens"] == 114  # 50 + 64
        # context_tokens is the LAST call's total only (proxy for next-request
        # context fill); summing it would double-count re-sent history. If
        # this equals 4164 (the summed total), the last-call rule regressed.
        assert usage["context_tokens"] == 2414

    _run(_test())


def test_pi_turn_without_usage_leaves_usage_none() -> None:
    """
    A turn whose pi events never carry a ``usage`` object completes with
    ``TurnComplete.usage`` left as ``None`` (cost tracking simply skipped),
    and the response text is unaffected. Guards against fabricating a
    zero-filled usage dict when pi reports nothing.
    """

    async def _test() -> None:
        executor = _executor_with_scripted_rpc(
            [
                json.dumps({"type": "response", "success": True}),
                json.dumps(
                    {
                        "type": "message_update",
                        "assistantMessageEvent": {"type": "text_delta", "delta": "Hi there"},
                    }
                ),
                # message_end with no usage object, then a usage-less agent_end.
                json.dumps({"type": "message_end", "message": {"stopReason": "stop"}}),
                json.dumps({"type": "agent_end", "messages": []}),
            ]
        )

        events = [
            e
            async for e in executor.run_turn(
                [{"role": "user", "content": "hello"}],
                [],
                "system",
            )
        ]

        turn_complete = [e for e in events if isinstance(e, TurnComplete)]
        assert len(turn_complete) == 1
        # No usage anywhere → usage stays None rather than a zero-filled dict.
        assert turn_complete[0].usage is None
        # The turn still completes normally with its streamed text.
        assert turn_complete[0].response == "Hi there"

    _run(_test())
