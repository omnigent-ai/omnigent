"""Tests for OmpExecutor's omp-specific surface (not the pi-parity core).

The transport, tool bridge, policy gates, resume, and usage aggregation are
ported verbatim from :mod:`omnigent.inner.pi_executor` and keep that module's
contract; these tests pin what differs for omp:

* spawn argv (``omp`` binary, ``--mode rpc``) and CLI feature detection
  (``--auto-approve`` vs pi's ``--approve``, ``--skills`` filter vs pi's
  ``--skill <path>`` explicit load)
* the ``models.yml`` gateway writer (YAML, not ``models.json``)
* protocol frames pi never emits: the startup ``ready`` frame,
  non-terminal ``agent_end`` (``isTerminal: false``), and local-only prompt
* omp identity: binary discovery, sandbox roots, env allowlist
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

import omnigent.inner.omp_executor as omp_mod
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
from omnigent.inner.omp_executor import (
    OmpExecutor,
    _aggregate_omp_turn_usage,
    _build_models_yml,
    _clean_omp_env,
    _extract_omp_turn_usage,
    _find_omp_cli,
    _generate_extension_js,
    _omp_needs_responses_api,
    _omp_provider_for_model,
    _omp_supports_auto_approve,
    _omp_thinking_from_config,
    _OmpRpcSession,
    _OmpSessionState,
    _redact_argv_for_log,
    _resolve_omp_skill_args,
    _split_omp_prompt,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


class _FakeStreamWriter:
    def __init__(self):
        self.data: list[bytes] = []

    def write(self, chunk: bytes) -> None:
        self.data.append(chunk)

    async def drain(self) -> None:
        return None


def _make_executor(**kwargs: Any) -> OmpExecutor:
    with patch.object(omp_mod, "_omp_supports_auto_approve", return_value=False):
        return OmpExecutor(omp_path="/fake/omp", gateway=False, **kwargs)


def _scripted_session(lines: list[str]) -> _OmpRpcSession:
    """A fake RPC session replaying scripted stdout lines."""
    rpc = _OmpRpcSession()
    rpc._line_queue = asyncio.Queue()
    for line in lines:
        rpc._line_queue.put_nowait(line)
    rpc.process = MagicMock()
    rpc.process.returncode = None
    rpc.process.stdin = _FakeStreamWriter()
    rpc._stderr_lines = []
    return rpc


def _drive_turn(executor: OmpExecutor, rpc: _OmpRpcSession) -> list[Any]:
    async def fake_ensure_rpc(*args: Any, **kwargs: Any) -> _OmpRpcSession:
        return rpc

    executor._ensure_rpc = fake_ensure_rpc  # type: ignore[method-assign]

    async def _collect():
        return [
            event
            async for event in executor.run_turn(
                [{"role": "user", "content": "hello"}],
                [],
                "system",
            )
        ]

    return _run(_collect())


# ---------------------------------------------------------------------------
# Binary discovery + feature detection
# ---------------------------------------------------------------------------


def test_find_omp_cli_resolves_omp_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI discovery looks up ``omp``, not ``pi``."""
    monkeypatch.setattr(omp_mod.shutil, "which", lambda name: f"/bin/{name}")
    assert _find_omp_cli() == "/bin/omp"


def test_supports_auto_approve_reads_help_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--auto-approve`` is feature-detected via ``omp --help``."""

    def _ok(*args: Any, **kwargs: Any) -> Any:
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "  --auto-approve   Auto-approve all tool calls\n"
        return proc

    monkeypatch.setattr(omp_mod.subprocess, "run", _ok)
    assert _omp_supports_auto_approve("/fake/omp") is True


def test_supports_auto_approve_absent_without_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CLI without the flag (older omp) reports unsupported."""

    def _old(*args: Any, **kwargs: Any) -> Any:
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "  --approve   something else\n"
        return proc

    monkeypatch.setattr(omp_mod.subprocess, "run", _old)
    assert _omp_supports_auto_approve("/fake/omp") is False


def test_supports_auto_approve_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe error never blocks the spawn — the flag is just omitted."""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="omp", timeout=15)

    monkeypatch.setattr(omp_mod.subprocess, "run", _boom)
    assert _omp_supports_auto_approve("/fake/omp") is False


# ---------------------------------------------------------------------------
# Skills filter mapping
# ---------------------------------------------------------------------------


def test_skill_args_all_passes_nothing() -> None:
    """``"all"`` relies on omp auto-discovery (no explicit-load flag exists)."""
    assert _resolve_omp_skill_args("all", None) == []


def test_skill_args_none_disables_discovery() -> None:
    assert _resolve_omp_skill_args("none", None) == ["--no-skills"]


def test_skill_args_list_becomes_name_filter() -> None:
    """A named list maps to omp's comma-separated ``--skills`` allowlist."""
    assert _resolve_omp_skill_args(["a", "b"], None) == ["--skills=a,b"]


def test_skill_args_unknown_shape_falls_back_to_defaults() -> None:
    assert _resolve_omp_skill_args("bogus", None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Spawn argv
# ---------------------------------------------------------------------------


def test_spawn_argv_uses_omp_binary_and_rpc_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_OmpRpcSession.start`` emits the omp headless-RPC argv."""
    captured: dict[str, Any] = {}

    async def _fake_spawn(*args: Any, **kwargs: Any) -> Any:
        captured["argv"] = list(args)

        async def _empty_stream():
            return
            yield  # pragma: no cover - never yields

        proc = MagicMock()
        proc.stdout = _empty_stream()
        proc.stderr = None
        proc.stdin = _FakeStreamWriter()
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        return proc

    monkeypatch.setattr(omp_mod, "_create_subprocess_exec", _fake_spawn)

    async def _test() -> None:
        rpc = _OmpRpcSession()
        await rpc.start(
            "/fake/omp",
            env={"PATH": "/usr/bin"},
            model="probe-gw/probe-model",
            thinking="high",
            system_prompt="sys",
            extra_args=["--extension", "/tmp/ext.js"],
        )
        await rpc.close()

    _run(_test())

    argv = captured["argv"]
    assert argv[:4] == ["/fake/omp", "--mode", "rpc", "--no-session"]
    assert "--model" in argv and "probe-gw/probe-model" in argv
    assert argv[argv.index("--thinking") + 1] == "high"
    assert argv[argv.index("--append-system-prompt") + 1] == "sys"
    assert "--extension" in argv
    assert "--approve" not in argv


def test_constructor_emits_auto_approve_when_supported() -> None:
    """The trust pre-accept uses omp's flag name, never pi's ``--approve``."""
    with patch.object(omp_mod, "_omp_supports_auto_approve", return_value=True):
        executor = OmpExecutor(omp_path="/fake/omp", gateway=False)
    assert "--auto-approve" in executor._extra_args
    assert "--approve" not in executor._extra_args


# ---------------------------------------------------------------------------
# models.yml gateway writer
# ---------------------------------------------------------------------------


def test_build_models_yml_registers_gateway_providers() -> None:
    """The builder emits omp's ``models.yml`` provider schema as YAML."""
    config = _build_models_yml(
        "https://example.com",
        "token-123",
        {"claude": "https://example.com/anthropic", "openai": "https://example.com/openai"},
        model="my-model",
    )
    assert set(config["providers"]) >= {
        "databricks-anthropic",
        "databricks-completions",
    }
    anthropic = config["providers"]["databricks-anthropic"]
    assert anthropic["baseUrl"] == "https://example.com/anthropic"
    assert anthropic["apiKey"] == "token-123"
    assert anthropic["api"] == "anthropic-messages"
    # YAML round-trip: the file omp reads must parse back identically.
    assert yaml.safe_load(yaml.safe_dump(config, sort_keys=True)) == config
    # The explicit model is registered so its provider/model selector resolves.
    model_ids = [
        entry["id"] for provider in config["providers"].values() for entry in provider["models"]
    ]
    assert "my-model" in model_ids


# ---------------------------------------------------------------------------
# Extension bridge identity
# ---------------------------------------------------------------------------


def test_generate_extension_js_targets_omp() -> None:
    """The generated extension registers tools and carries the server token."""
    js = _generate_extension_js(
        54321,
        [{"name": "calculate", "description": "Do math", "parameters": {"type": "object"}}],
        "secret-token",
    )
    assert "pi.registerTool" in js
    assert "54321" in js
    assert "secret-token" in js
    assert "calculate" in js


def test_clean_omp_env_passes_agent_dir_but_not_api_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PI_CODING_AGENT_DIR`` (omp's agent root) passes; keys do not."""
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "/tmp/managed-agent")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-pwned")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-pwned")
    env = _clean_omp_env()
    assert env["PI_CODING_AGENT_DIR"] == "/tmp/managed-agent"
    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env


# ---------------------------------------------------------------------------
# Protocol deltas: frames pi never emits
# ---------------------------------------------------------------------------


def test_ready_frame_is_skipped() -> None:
    """The startup ``ready`` frame carries no turn content."""
    executor = _make_executor()
    rpc = _scripted_session(
        [
            json.dumps({"type": "ready", "protocolVersion": 1}),
            json.dumps(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "hi"},
                }
            ),
            json.dumps({"type": "agent_end", "messages": []}),
        ]
    )
    events = _drive_turn(executor, rpc)
    assert isinstance(events[0], TextChunk) and events[0].text == "hi"
    assert isinstance(events[-1], TurnComplete)
    assert events[-1].response == "hi"


def test_non_terminal_agent_end_keeps_draining() -> None:
    """``agent_end`` with ``isTerminal: false`` is maintenance, not completion."""
    executor = _make_executor()
    rpc = _scripted_session(
        [
            json.dumps({"type": "agent_end", "messages": [], "isTerminal": False}),
            json.dumps(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "late"},
                }
            ),
            json.dumps({"type": "agent_end", "messages": [], "isTerminal": True}),
        ]
    )
    events = _drive_turn(executor, rpc)
    assert isinstance(events[-1], TurnComplete)
    assert events[-1].response == "late"


def test_absent_is_terminal_still_completes() -> None:
    """Older runtimes omit ``isTerminal`` — the end is terminal."""
    executor = _make_executor()
    rpc = _scripted_session([json.dumps({"type": "agent_end", "messages": []})])
    events = _drive_turn(executor, rpc)
    assert isinstance(events[-1], TurnComplete)


def test_local_only_prompt_ack_completes_without_agent_end() -> None:
    """A ``prompt`` ack with ``agentInvoked: false`` ends the turn immediately."""
    executor = _make_executor()
    rpc = _scripted_session(
        [
            json.dumps(
                {
                    "type": "response",
                    "command": "prompt",
                    "success": True,
                    "data": {"agentInvoked": False},
                }
            ),
        ]
    )
    events = _drive_turn(executor, rpc)
    assert len(events) == 1
    assert isinstance(events[0], TurnComplete)
    assert events[0].response is None


def test_prompt_result_completes_without_agent_end() -> None:
    """A late ``prompt_result`` with ``agentInvoked: false`` ends the turn."""
    executor = _make_executor()
    rpc = _scripted_session(
        [
            json.dumps({"type": "prompt_result", "id": "turn_1", "agentInvoked": False}),
        ]
    )
    events = _drive_turn(executor, rpc)
    assert len(events) == 1
    assert isinstance(events[0], TurnComplete)
    assert events[0].response is None


def test_prompt_error_surfaces_executor_error() -> None:
    """A failed ``prompt`` command (e.g. no model selected) is a turn error."""
    executor = _make_executor()
    rpc = _scripted_session(
        [
            json.dumps(
                {
                    "type": "response",
                    "command": "prompt",
                    "success": False,
                    "error": "No model selected",
                }
            ),
        ]
    )
    events = _drive_turn(executor, rpc)
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "No model selected" in events[0].message


# ---------------------------------------------------------------------------
# Feature detection: token-boundary matching
# ---------------------------------------------------------------------------


def _help_proc(stdout: str, returncode: int = 0) -> Any:
    proc = MagicMock()
    proc.stdout = stdout
    proc.returncode = returncode
    return proc


def test_supports_auto_approve_ignores_prose_mention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``--plan-yolo`` help prose mentions "auto-approve" without the flag.

    A bare substring check would treat that prose as feature support and emit
    ``--auto-approve`` — a hard spawn error on CLIs that never had the flag —
    so detection requires the exact flag token.
    """
    monkeypatch.setattr(
        omp_mod.subprocess,
        "run",
        lambda *args, **kwargs: _help_proc(
            "  --plan-yolo  Force plan mode, auto-approve the plan on resolve\n"
        ),
    )
    assert _omp_supports_auto_approve("/fake/omp") is False


def test_supports_auto_approve_rejects_longer_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hypothetical ``--auto-approve-tools`` must not imply ``--auto-approve``."""
    monkeypatch.setattr(
        omp_mod.subprocess,
        "run",
        lambda *args, **kwargs: _help_proc("  --auto-approve-tools  Approve tool calls\n"),
    )
    assert _omp_supports_auto_approve("/fake/omp") is False


def test_supports_auto_approve_accepts_flag_with_trailing_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real flag line (``--auto-approve  Auto-approve all tool calls``) matches."""
    monkeypatch.setattr(
        omp_mod.subprocess,
        "run",
        lambda *args, **kwargs: _help_proc(
            "  --plan-yolo  Force plan mode, auto-approve the plan\n"
            "  --auto-approve  Auto-approve all tool calls (skip approval prompts)\n"
        ),
    )
    assert _omp_supports_auto_approve("/fake/omp") is True


def test_supports_auto_approve_requires_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Help text on a failing probe means nothing — fail closed to no-flag."""
    monkeypatch.setattr(
        omp_mod.subprocess,
        "run",
        lambda *args, **kwargs: _help_proc("  --auto-approve  Auto-approve\n", returncode=1),
    )
    assert _omp_supports_auto_approve("/fake/omp") is False


# ---------------------------------------------------------------------------
# Model → provider routing
# ---------------------------------------------------------------------------


def test_provider_for_model_routes_families() -> None:
    """Claude/GPT/system.ai ids land on their gateway providers."""
    assert _omp_provider_for_model("databricks-claude-sonnet-4-6") == "databricks-anthropic"
    assert _omp_provider_for_model("databricks-gpt-5-codex") == "databricks-openai"
    assert _omp_provider_for_model("my-llama-3") == "databricks-completions"


def test_provider_for_model_system_ai_split() -> None:
    """System models split on Responses capability (GLM/kimi vs plain)."""
    assert _omp_provider_for_model("system.ai.glm-5") == "databricks-openai"
    assert _omp_provider_for_model("system.ai.llama-4") == "databricks-mlflow"


def test_provider_for_model_generic_wire() -> None:
    """A generic gateway's configured wire picks the provider, not the id."""
    assert (
        _omp_provider_for_model("anything", generic_openai_wire_api="responses")
        == "databricks-openai"
    )
    assert (
        _omp_provider_for_model("anything", generic_openai_wire_api="chat")
        == "databricks-completions"
    )


def test_needs_responses_api_catalog_authoritative() -> None:
    """Catalog wire metadata beats id-substring guessing."""
    from omnigent.models.model_metadata import ModelWireAPI

    assert (
        _omp_needs_responses_api(
            "system.ai.quiet-llama", frozenset({ModelWireAPI.OPENAI_RESPONSES})
        )
        is True
    )
    assert (
        _omp_needs_responses_api("databricks-gpt-legacy", frozenset({ModelWireAPI.OPENAI_CHAT}))
        is False
    )
    # Unknown GPT metadata fails toward Responses (the tool-capable surface).
    assert _omp_needs_responses_api("databricks-gpt-odd") is True


# ---------------------------------------------------------------------------
# models.yml routing variants
# ---------------------------------------------------------------------------


def _catalog_entry(model_id: str, wire_apis: frozenset) -> Any:
    from omnigent.models.model_catalog import ModelEntry
    from omnigent.models.model_metadata import ModelMetadata

    return ModelEntry(id=model_id, family="openai", metadata=ModelMetadata(wire_apis=wire_apis))


def test_build_models_yml_routes_catalog_models() -> None:
    """Catalog ids land under the provider their wire metadata selects."""
    from omnigent.models.model_metadata import ModelWireAPI

    catalog = [
        _catalog_entry("claude-x", frozenset({ModelWireAPI.ANTHROPIC_MESSAGES})),
        _catalog_entry("gpt-x", frozenset({ModelWireAPI.OPENAI_RESPONSES})),
        _catalog_entry("gpt-old", frozenset({ModelWireAPI.OPENAI_CHAT})),
        _catalog_entry("system.ai.kimi-z", frozenset({ModelWireAPI.OPENAI_RESPONSES})),
        _catalog_entry("system.ai.llama-z", frozenset({ModelWireAPI.OPENAI_CHAT})),
    ]
    config = _build_models_yml("https://h.example.com/", "tok", catalog_models=catalog)
    providers = config["providers"]
    assert [e["id"] for e in providers["databricks-anthropic"]["models"]] == ["claude-x"]
    assert [e["id"] for e in providers["databricks-openai"]["models"]] == [
        "gpt-x",
        "system.ai.kimi-z",
    ]
    assert [e["id"] for e in providers["databricks"]["models"]] == ["gpt-old"]
    assert [e["id"] for e in providers["databricks-mlflow"]["models"]] == ["system.ai.llama-z"]
    # Host trailing slashes never leak into provider URLs.
    assert providers["databricks-openai"]["baseUrl"].endswith("/ai-gateway/codex/v1")


def test_build_models_yml_generic_provider_uses_one_base_url() -> None:
    """Non-Databricks gateways (LiteLLM) reuse their base URL on every path."""
    config = _build_models_yml(
        "https://unused.example.com",
        "tok",
        {"openai": "https://proxy.example.com/v1", "claude": "https://proxy.example.com/v1"},
        model="proxy-model",
        openai_wire_api="chat",
    )
    providers = config["providers"]
    assert providers["databricks"]["baseUrl"] == "https://proxy.example.com/v1"
    assert providers["databricks-openai"]["baseUrl"] == "https://proxy.example.com/v1"
    # Chat wire + generic provider → completions provider, Bearer auth header.
    routed = providers["databricks-completions"]["models"]
    assert [e["id"] for e in routed] == ["proxy-model"]
    assert providers["databricks-completions"]["authHeader"] is True
    entry = routed[0]
    # Dynamically registered models assert image input so omp never silently
    # drops attached images (the vision-guard checks ``input`` for "image").
    assert entry["input"] == ["text", "image"]


def test_build_models_yml_marks_reasoning_models() -> None:
    """A dynamically registered reasoning id carries ``reasoning: true``."""
    config = _build_models_yml("https://h.example.com", "tok", model="deepseek-r1")
    providers = config["providers"]
    entry = next(
        e
        for provider in providers.values()
        for e in provider["models"]
        if e["id"] == "deepseek-r1"
    )
    assert entry["reasoning"] is True


def test_build_models_yml_no_model_registers_nothing() -> None:
    """``model=None`` leaves every provider list empty for omp's own default."""
    config = _build_models_yml("https://h.example.com", "tok")
    assert all(provider["models"] == [] for provider in config["providers"].values())


def test_build_models_yml_skips_unsupported_models() -> None:
    """Ids ``unsupported_in_pi`` rejects never reach any provider list."""
    import omnigent.inner.omp_executor as mod

    catalog = [_catalog_entry("some-model", frozenset())]
    with (
        patch.object(mod, "unsupported_in_pi", return_value=True),
        patch.object(
            mod,
            "pi_model_json_entry",
            side_effect=AssertionError("must not build skipped entries"),
        ),
    ):
        config = _build_models_yml("https://h.example.com", "tok", catalog_models=catalog)
    assert all(provider["models"] == [] for provider in config["providers"].values())


# ---------------------------------------------------------------------------
# Usage extraction + aggregation
# ---------------------------------------------------------------------------


def _assistant_msg(**overrides: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "role": "assistant",
        "model": "databricks-anthropic/wire-model",
        "usage": {
            "input": 100,
            "output": 50,
            "cacheRead": 1000,
            "cacheWrite": 200,
            "totalTokens": 1350,
        },
    }
    msg.update(overrides)
    return msg


def test_extract_usage_maps_wire_shape() -> None:
    """omp's camelCase ``usage`` maps onto the turn-pricing schema."""
    usage = _extract_omp_turn_usage(_assistant_msg(), "fallback-model")
    assert usage is not None
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 50
    assert usage["total_tokens"] == 1350
    assert usage["cache_read_input_tokens"] == 1000
    assert usage["cache_creation_input_tokens"] == 200
    assert usage["model"] == "databricks-anthropic/wire-model"


def test_extract_usage_falls_back_to_configured_model() -> None:
    """A message without ``model`` prices against the executor's model."""
    msg = _assistant_msg()
    del msg["model"]
    usage = _extract_omp_turn_usage(msg, "fallback-model")
    assert usage is not None
    assert usage["model"] == "fallback-model"


def test_extract_usage_rejects_non_assistant_or_missing() -> None:
    """Non-assistant messages, missing usage, and non-dicts yield ``None``."""
    assert _extract_omp_turn_usage({"role": "user", "usage": {"input": 1}}, "m") is None
    assert _extract_omp_turn_usage({"role": "assistant"}, "m") is None
    assert _extract_omp_turn_usage({"role": "assistant", "usage": [1]}, "m") is None
    assert _extract_omp_turn_usage("not-a-dict", "m") is None
    # Missing counters default to zero rather than raising.
    usage = _extract_omp_turn_usage({"role": "assistant", "usage": {}}, "m")
    assert usage is not None
    assert usage["input_tokens"] == 0


def test_aggregate_usage_sums_calls_last_context() -> None:
    """Token counts sum across a tool-loop's calls; context is the last call."""
    first = _extract_omp_turn_usage(_assistant_msg(), "m")
    second_msg = _assistant_msg(
        usage={"input": 1300, "output": 60, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 1360}
    )
    second = _extract_omp_turn_usage(second_msg, "m")
    assert first is not None and second is not None
    turn = _aggregate_omp_turn_usage([first, second], "m")
    assert turn is not None
    assert turn["input_tokens"] == 1400
    assert turn["output_tokens"] == 110
    assert turn["context_tokens"] == 1360


def test_aggregate_usage_empty_or_zero_is_none() -> None:
    """No captures (or all-zero captures) leave the turn unpriced."""
    assert _aggregate_omp_turn_usage([], "m") is None
    zero = _extract_omp_turn_usage({"role": "assistant", "usage": {}}, "m")
    assert zero is not None
    assert _aggregate_omp_turn_usage([zero], "m") is None


# ---------------------------------------------------------------------------
# Prompt splitting (multimodal blocks)
# ---------------------------------------------------------------------------


def test_split_prompt_joins_text_blocks() -> None:
    """Text blocks join into the ``message`` with no images."""
    message, images = _split_omp_prompt(
        [
            {"type": "input_text", "text": "look at this"},
            {"type": "output_text", "text": "prior reply"},
        ]
    )
    assert message == "look at this\nprior reply"
    assert images == []


def test_split_prompt_routes_data_uri_images() -> None:
    """Inline data-URI images become native ``images`` entries, not text."""
    import base64

    payload = base64.b64encode(b"fakepng").decode()
    message, images = _split_omp_prompt(
        [
            {"type": "input_text", "text": "describe"},
            {"type": "input_image", "image_url": f"data:image/png;base64,{payload}"},
        ]
    )
    assert message == "describe"
    assert images == [{"type": "image", "data": payload, "mimeType": "image/png"}]


def test_split_prompt_rejects_remote_image() -> None:
    """omp forwards images inline — a URL it cannot fetch fails loudly."""
    with pytest.raises(ValueError, match="data URI"):
        _split_omp_prompt([{"type": "input_image", "image_url": "https://x/y.png"}])


def test_split_prompt_inlines_text_files_skips_binary() -> None:
    """Text attachments inline; binary ones skip (omp has no file channel)."""
    import base64

    text_payload = base64.b64encode(b"file contents here").decode()
    bin_payload = base64.b64encode(b"\x00\x01").decode()
    message, images = _split_omp_prompt(
        [
            {"type": "input_file", "file_data": f"data:text/plain;base64,{text_payload}"},
            {"type": "input_file", "file_data": f"data:image/png;base64,{bin_payload}"},
            {"type": "input_file", "file_data": "already inline text"},
        ]
    )
    assert message == "file contents here\nalready inline text"
    assert images == []


def test_split_prompt_rejects_unknown_block() -> None:
    """A new block shape fails the turn rather than vanishing silently."""
    with pytest.raises(ValueError, match="Unsupported content block type"):
        _split_omp_prompt([{"type": "input_audio", "audio_url": "data:audio/x;base64,AAA"}])


# ---------------------------------------------------------------------------
# Argv redaction
# ---------------------------------------------------------------------------


def test_redact_argv_for_log_hides_system_prompt() -> None:
    """System-prompt values never reach logs in either argv form."""
    redacted = _redact_argv_for_log(
        ["omp", "--mode", "rpc", "--append-system-prompt", "supersecret", "--tools", "read"]
    )
    assert redacted == [
        "omp",
        "--mode",
        "rpc",
        "--append-system-prompt",
        "[system prompt 11 chars]",
        "--tools",
        "read",
    ]
    redacted_eq = _redact_argv_for_log(["omp", "--append-system-prompt=topsecret"])
    assert redacted_eq == ["omp", "--append-system-prompt=[system prompt 9 chars]"]


# ---------------------------------------------------------------------------
# run_turn: tool events, errors, EOF, late failures, unknown frames
# ---------------------------------------------------------------------------


def _drive(
    executor: OmpExecutor,
    rpc: _OmpRpcSession,
    *,
    tools: list | None = None,
    config: Any = None,
    messages: list | None = None,
) -> list[Any]:
    async def fake_ensure_rpc(*args: Any, **kwargs: Any) -> _OmpRpcSession:
        return rpc

    executor._ensure_rpc = fake_ensure_rpc  # type: ignore[method-assign]

    async def _collect():
        return [
            event
            async for event in executor.run_turn(
                messages if messages is not None else [{"role": "user", "content": "hello"}],
                tools if tools is not None else [],
                "system",
                config,
            )
        ]

    return _run(_collect())


def _agent_end(messages: list | None = None, **fields: Any) -> str:
    import json as _json

    event: dict[str, Any] = {"type": "agent_end", "messages": messages or []}
    event.update(fields)
    return _json.dumps(event)


def test_tool_call_success_round_trip() -> None:
    """Tool start/end events surface as request + success completion."""
    import json as _json

    executor = _make_executor()
    rpc = _scripted_session(
        [
            _json.dumps(
                {"type": "tool_execution_start", "toolName": "calculate", "args": {"x": 1}}
            ),
            _json.dumps(
                {
                    "type": "tool_execution_end",
                    "toolName": "calculate",
                    "isError": False,
                    "result": {"value": 3},
                }
            ),
            _agent_end(),
        ]
    )
    events = _drive(executor, rpc)
    assert isinstance(events[0], ToolCallRequest)
    assert events[0].name == "calculate"
    assert events[0].args == {"x": 1}
    assert isinstance(events[1], ToolCallComplete)
    assert events[1].status == ToolCallStatus.SUCCESS
    assert events[1].result == {"value": 3}
    assert isinstance(events[2], TurnComplete)


def test_tool_call_non_dict_args_become_empty() -> None:
    """Malformed tool args never crash the turn — they degrade to ``{}``."""
    import json as _json

    executor = _make_executor()
    rpc = _scripted_session(
        [
            _json.dumps({"type": "tool_execution_start", "toolName": "t", "args": [1, 2]}),
            _json.dumps({"type": "tool_execution_end", "toolName": "t", "result": "ok"}),
            _agent_end(),
        ]
    )
    events = _drive(executor, rpc)
    assert isinstance(events[0], ToolCallRequest)
    assert events[0].args == {}


def test_tool_call_error_status() -> None:
    """A natively errored tool completes as ERROR with the result text."""
    import json as _json

    executor = _make_executor()
    rpc = _scripted_session(
        [
            _json.dumps(
                {
                    "type": "tool_execution_end",
                    "toolName": "bash",
                    "isError": True,
                    "result": "boom",
                }
            ),
            _agent_end(),
        ]
    )
    events = _drive(executor, rpc)
    assert isinstance(events[0], ToolCallComplete)
    assert events[0].status == ToolCallStatus.ERROR
    assert events[0].error == "boom"


def test_tool_call_blocked_forms() -> None:
    """Policy-blocked results map to BLOCKED in every envelope the bridge emits."""
    import json as _json

    cases = [
        # Direct dict from the tool server.
        {"blocked": True, "reason": "denied-direct"},
        # Wrapped in MCP content text by the extension.
        {
            "content": [
                {"type": "text", "text": _json.dumps({"blocked": True, "reason": "denied-wrap"})}
            ]
        },
        # Bare JSON string.
        _json.dumps({"blocked": True, "reason": "denied-str"}),
    ]
    for result in cases:
        executor = _make_executor()
        rpc = _scripted_session(
            [
                _json.dumps(
                    {
                        "type": "tool_execution_end",
                        "toolName": "read",
                        "isError": True,
                        "result": result,
                    }
                ),
                _agent_end(),
            ]
        )
        events = _drive(executor, rpc)
        assert isinstance(events[0], ToolCallComplete)
        assert events[0].status == ToolCallStatus.BLOCKED
        assert "denied" in (events[0].error or "")


def test_tool_error_inside_result_dict_counts() -> None:
    """``result.isError`` alone (no top-level flag) still marks ERROR."""
    import json as _json

    executor = _make_executor()
    rpc = _scripted_session(
        [
            _json.dumps(
                {
                    "type": "tool_execution_end",
                    "toolName": "edit",
                    "result": {"isError": True, "detail": "nope"},
                }
            ),
            _agent_end(),
        ]
    )
    events = _drive(executor, rpc)
    assert isinstance(events[0], ToolCallComplete)
    assert events[0].status == ToolCallStatus.ERROR


def test_message_end_error_drains_to_agent_end() -> None:
    """An errored LLM call reports only after the terminal ``agent_end`` drains."""
    import json as _json

    executor = _make_executor()
    rpc = _scripted_session(
        [
            _json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "error",
                        "errorMessage": "provider exploded",
                    },
                }
            ),
            _agent_end(),
        ]
    )
    events = _drive(executor, rpc)
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "provider exploded" in events[0].message


def test_message_end_aborted_fails_fast() -> None:
    """An aborted call ends the turn immediately (no drain wait)."""
    import json as _json

    executor = _make_executor()
    rpc = _scripted_session(
        [
            _json.dumps(
                {
                    "type": "message_end",
                    "message": {"role": "assistant", "stopReason": "aborted"},
                }
            ),
        ]
    )
    events = _drive(executor, rpc)
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)


def test_message_end_usage_sums_into_turn() -> None:
    """Per-call ``message_end`` usage aggregates onto the TurnComplete."""
    import json as _json

    executor = _make_executor()
    rpc = _scripted_session(
        [
            _json.dumps(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "hi"},
                }
            ),
            _json.dumps({"type": "message_end", "message": _assistant_msg()}),
            _agent_end(),
        ]
    )
    events = _drive(executor, rpc)
    complete = events[-1]
    assert isinstance(complete, TurnComplete)
    assert complete.response == "hi"
    assert complete.usage is not None
    assert complete.usage["input_tokens"] == 100


def test_eof_without_response_is_error_with_stderr() -> None:
    """A dead process with no text surfaces stderr for diagnosis."""
    executor = _make_executor()
    rpc = _scripted_session([])
    rpc._stderr_lines = ["FATAL: something broke"]
    with patch.object(omp_mod, "_TURN_STDOUT_IDLE_TIMEOUT_S", 0.01):
        events = _drive(executor, rpc)
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "something broke" in events[0].message


def test_late_prompt_scheduling_error_fails_turn() -> None:
    """Upstream may report async prompt-scheduling failure on the prompt id."""
    import json as _json

    executor = _make_executor()
    rpc = _scripted_session(
        [
            _json.dumps({"type": "response", "command": "prompt", "success": True}),
            _json.dumps(
                {
                    "type": "response",
                    "command": "prompt",
                    "success": False,
                    "error": "session is streaming",
                }
            ),
        ]
    )
    events = _drive(executor, rpc)
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "session is streaming" in events[0].message


def test_ack_invoked_true_waits_for_agent_end() -> None:
    """``agentInvoked: true`` is not a completion — the turn ends at ``agent_end``."""
    import json as _json

    executor = _make_executor()
    rpc = _scripted_session(
        [
            _json.dumps(
                {
                    "type": "response",
                    "command": "prompt",
                    "success": True,
                    "data": {"agentInvoked": True},
                }
            ),
            _json.dumps(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "late"},
                }
            ),
            _agent_end(),
        ]
    )
    events = _drive(executor, rpc)
    assert isinstance(events[-1], TurnComplete)
    assert events[-1].response == "late"


def test_future_frames_are_ignored() -> None:
    """Frames the executor does not consume never wedge the loop."""
    import json as _json

    executor = _make_executor()
    rpc = _scripted_session(
        [
            _json.dumps({"type": "ready", "protocolVersion": 1}),
            _json.dumps({"type": "extension_ui_request", "id": "w1", "method": "setWidget"}),
            _json.dumps({"type": "available_commands_update", "commands": []}),
            _json.dumps({"type": "rpc_frame_error", "error": "overflow elsewhere"}),
            _json.dumps({"type": "turn_start"}),
            _json.dumps({"type": "notice", "level": "info", "message": "hi"}),
            _json.dumps({"type": "thinking_level_changed"}),
            "not json at all",
            _json.dumps(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "ok"},
                }
            ),
            _agent_end(),
        ]
    )
    events = _drive(executor, rpc)
    assert isinstance(events[-1], TurnComplete)
    assert events[-1].response == "ok"


def test_thinking_deltas_stay_out_of_response() -> None:
    """Reasoning streams as ``ReasoningChunk`` and never pollutes the reply."""
    import json as _json

    executor = _make_executor()
    rpc = _scripted_session(
        [
            _json.dumps(
                {"type": "message_update", "assistantMessageEvent": {"type": "thinking_start"}}
            ),
            _json.dumps(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "thinking_delta", "delta": "hmm"},
                }
            ),
            _json.dumps(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "done"},
                }
            ),
            _agent_end(),
        ]
    )
    events = _drive(executor, rpc)
    assert isinstance(events[0], ReasoningChunk)
    assert isinstance(events[1], ReasoningChunk)
    assert isinstance(events[2], TextChunk)
    assert isinstance(events[3], TurnComplete)
    assert events[1].delta == "hmm"
    assert events[-1].response == "done"


def test_agent_end_text_fallback() -> None:
    """Unstreamed turns recover text from the ``agent_end`` message list."""
    executor = _make_executor()
    rpc = _scripted_session(
        [
            _agent_end(
                [
                    {"role": "user", "content": "q"},
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "fallback reply"}],
                    },
                ]
            ),
        ]
    )
    events = _drive(executor, rpc)
    assert isinstance(events[-1], TurnComplete)
    assert events[-1].response == "fallback reply"


def test_invalid_effort_fails_turn_without_spawn() -> None:
    """An unsupported effort is a turn error, never a doomed subprocess."""
    executor = _make_executor()
    rpc = _scripted_session([])
    config = ExecutorConfig(extra={"reasoning_effort": "bogus-level"})
    events = _drive(executor, rpc, config=config)
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "bogus-level" in events[0].message
    # No prompt was ever sent — the turn died before session setup.
    assert rpc.process.stdin.data == []


# ---------------------------------------------------------------------------
# Thinking resolution + application
# ---------------------------------------------------------------------------


def test_thinking_from_config_maps_efforts() -> None:
    """Canonical efforts translate to omp's vocabulary; clears resolve to None."""
    assert _omp_thinking_from_config(None) is None
    assert _omp_thinking_from_config(ExecutorConfig()) is None
    assert _omp_thinking_from_config(ExecutorConfig(extra={"reasoning_effort": "off"})) is None
    assert _omp_thinking_from_config(ExecutorConfig(extra={"reasoning_effort": "high"})) == "high"
    assert _omp_thinking_from_config(ExecutorConfig(extra={"reasoning_effort": "none"})) == "off"
    assert _omp_thinking_from_config(ExecutorConfig(extra={"reasoning_effort": "ultra"})) == "max"
    with pytest.raises(ValueError, match="bogus"):
        _omp_thinking_from_config(ExecutorConfig(extra={"reasoning_effort": "bogus"}))


def _sent_commands(rpc: _OmpRpcSession) -> list[dict]:
    import json as _json

    return [_json.loads(chunk.decode()) for chunk in rpc.process.stdin.data]


def test_apply_thinking_skips_when_cleared() -> None:
    """``thinking=None`` leaves a live session untouched (no probe, no RPC)."""
    executor = _make_executor()
    rpc = _scripted_session([])
    state = _OmpSessionState()
    with patch.object(
        executor, "_available_thinking_levels", side_effect=AssertionError("no probe")
    ):
        _run(executor._apply_thinking_level(state, rpc, None, spawned=False))
    assert _sent_commands(rpc) == []


def test_apply_thinking_spawned_same_level_sends_nothing() -> None:
    """A fresh spawn already carries ``--thinking`` — only the probe runs."""
    executor = _make_executor()
    rpc = _scripted_session([])
    state = _OmpSessionState(applied_thinking="high")
    with patch.object(executor, "_available_thinking_levels", return_value=None):
        _run(executor._apply_thinking_level(state, rpc, "high", spawned=True))
    assert _sent_commands(rpc) == []


def test_apply_thinking_live_change_sends_level() -> None:
    """A changed level on a live session issues ``set_thinking_level``."""
    executor = _make_executor()
    rpc = _scripted_session([])
    state = _OmpSessionState(applied_thinking="low")
    with patch.object(executor, "_available_thinking_levels", return_value=None):
        _run(executor._apply_thinking_level(state, rpc, "high", spawned=False))
    commands = _sent_commands(rpc)
    assert commands == [{"type": "set_thinking_level", "level": "high", "id": "thinking_high"}]
    assert state.applied_thinking == "high"


def test_apply_thinking_clamps_to_supported_rung() -> None:
    """An unsupported rung clamps down and warns instead of 400ing the turn."""
    executor = _make_executor()
    rpc = _scripted_session([])
    state = _OmpSessionState(applied_thinking="low")
    with patch.object(executor, "_available_thinking_levels", return_value=["low"]):
        _run(executor._apply_thinking_level(state, rpc, "high", spawned=False))
    commands = _sent_commands(rpc)
    assert commands == [{"type": "set_thinking_level", "level": "low", "id": "thinking_low"}]
    # Records the request so a repeat turn does not re-warn.
    assert state.applied_thinking == "high"


def test_available_levels_probe_failure_means_unclamped() -> None:
    """omp answers the probe with ``success: false`` — levels stay ``None``."""
    import json as _json

    executor = _make_executor()
    rpc = _scripted_session(
        [
            _json.dumps(
                {
                    "type": "response",
                    "command": "get_available_thinking_levels",
                    "success": False,
                    "error": "Unknown command: get_available_thinking_levels",
                }
            )
        ]
    )
    assert _run(executor._available_thinking_levels(rpc)) is None


# ---------------------------------------------------------------------------
# Session messaging: steer, interrupt, close
# ---------------------------------------------------------------------------


def test_enqueue_steers_live_session() -> None:
    """Steering text reaches the in-flight turn via the ``steer`` command."""
    executor = _make_executor()
    rpc = _scripted_session([])
    executor._session_states["k"] = _OmpSessionState(rpc=rpc)
    assert _run(executor.enqueue_session_message("k", "wait, use tabs")) is True
    assert _sent_commands(rpc) == [{"type": "steer", "message": "wait, use tabs"}]


def test_enqueue_without_session_is_false() -> None:
    """Steering with no live session (or dead process) reports failure."""
    executor = _make_executor()
    assert _run(executor.enqueue_session_message("missing", "hi")) is False
    rpc = _scripted_session([])
    rpc.process = None
    executor._session_states["dead"] = _OmpSessionState(rpc=rpc)
    assert _run(executor.enqueue_session_message("dead", "hi")) is False


def test_interrupt_aborts_and_drops_session() -> None:
    """Interrupt aborts the turn and drops state so the next turn replays history."""
    from unittest.mock import AsyncMock

    executor = _make_executor()
    rpc = _scripted_session([])
    rpc.process.wait = AsyncMock(return_value=0)
    executor._session_states["k"] = _OmpSessionState(rpc=rpc)
    stdin = rpc.process.stdin
    assert _run(executor.interrupt_session("k")) is True
    import json as _json

    assert [_json.loads(chunk.decode()) for chunk in stdin.data] == [
        {"type": "abort", "id": "abort"}
    ]
    assert "k" not in executor._session_states


def test_interrupt_without_session_is_false() -> None:
    """Interrupting nothing is a no-op ``False``, not an error."""
    executor = _make_executor()
    assert _run(executor.interrupt_session("missing")) is False


# ---------------------------------------------------------------------------
# _build_env_and_dir: bridge, tools allowlist, retry settings, gateway files
# ---------------------------------------------------------------------------


def _tool(name: str) -> dict:
    return {"name": name, "description": f"{name} tool", "parameters": {"type": "object"}}


def test_env_and_dir_writes_bridge_and_allowlists_read() -> None:
    """Bridged tools register via ``--extension`` and re-enable under ``--tools``."""
    executor = _make_executor()
    config = executor._build_env_and_dir([_tool("calculate")], 54321, "tok", None)
    try:
        ext = next(a for a in config.extra_args if a.endswith("omnigent_tools.mjs"))
        with open(ext) as f:
            js = f.read()
        assert "calculate" in js
        assert "tok" in js
        tools_idx = config.extra_args.index("--tools")
        allowlisted = config.extra_args[tools_idx + 1].split(",")
        # Bridged names plus native ``read`` (skill-index injection needs it).
        assert "calculate" in allowlisted
        assert "read" in allowlisted
        assert "--no-tools" in config.extra_args
    finally:
        import shutil

        shutil.rmtree(config.tmp_dir, ignore_errors=True)


def test_env_and_dir_skills_none_omits_read() -> None:
    """``skills: none`` suppresses discovery — ``read`` stays out of ``--tools``."""
    executor = _make_executor(skills_filter="none")
    assert "--no-skills" in executor._extra_args
    config = executor._build_env_and_dir([_tool("calculate")], 54321, "tok", None)
    try:
        tools_idx = config.extra_args.index("--tools")
        assert config.extra_args[tools_idx + 1].split(",") == ["calculate"]
    finally:
        import shutil

        shutil.rmtree(config.tmp_dir, ignore_errors=True)


def test_env_and_dir_no_tools_means_no_bridge() -> None:
    """A tool-less turn spawns no extension and no ``--tools`` flag."""
    executor = _make_executor()
    config = executor._build_env_and_dir([], None, None, None)
    try:
        assert not [a for a in config.extra_args if a.endswith(".mjs")]
        assert "--tools" not in config.extra_args
    finally:
        import shutil

        shutil.rmtree(config.tmp_dir, ignore_errors=True)


def test_env_and_dir_port_without_token_is_loud() -> None:
    """A port with no token would bridge unauthenticated — refuse instead."""
    executor = _make_executor()
    with pytest.raises(ValueError, match="tool_server_token"):
        executor._build_env_and_dir([_tool("calculate")], 54321, None, None)


def test_env_and_dir_merges_retry_into_project_settings(tmp_path) -> None:
    """Retry budgets merge into ``<cwd>/.omp/settings.json``, keeping user keys."""
    import json as _json

    settings_path = tmp_path / ".omp" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(_json.dumps({"custom": True}))
    executor = _make_executor(cwd=str(tmp_path))
    config = executor._build_env_and_dir([], None, None, None)
    try:
        merged = _json.loads(settings_path.read_text())
        assert merged["custom"] is True
        assert merged["retry"]["enabled"] is True
        assert merged["retry"]["maxRetries"] >= 0
    finally:
        import shutil

        shutil.rmtree(config.tmp_dir, ignore_errors=True)


def _make_gateway_executor(**kwargs: Any) -> OmpExecutor:
    """Gateway executor with Databricks credentials stubbed (no network)."""
    from omnigent.inner.databricks_executor import DatabricksCredentials

    with (
        patch.object(omp_mod, "_omp_supports_auto_approve", return_value=False),
        patch.object(
            omp_mod,
            "_read_databrickscfg",
            return_value=DatabricksCredentials(host="https://h.example.com", token="tok"),
        ),
    ):
        return OmpExecutor(omp_path="/fake/omp", gateway=True, **kwargs)


def test_env_and_dir_gateway_writes_models_yml() -> None:
    """Gateway runs relocate the agent dir and point omp at ``models.yml``."""
    import yaml as _yaml

    executor = _make_gateway_executor()
    config = executor._build_env_and_dir([], None, None, "my-claude-x")
    try:
        assert config.env["PI_CODING_AGENT_DIR"] == config.tmp_dir
        models_path = f"{config.tmp_dir}/models.yml"
        with open(models_path) as f:
            models = _yaml.safe_load(f)
        assert models["providers"]["databricks-anthropic"]["apiKey"] == "tok"
        routed = [e["id"] for provider in models["providers"].values() for e in provider["models"]]
        assert "my-claude-x" in routed
    finally:
        import shutil

        shutil.rmtree(config.tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# _ensure_rpc: model qualification, reuse, respawn
# ---------------------------------------------------------------------------


def test_ensure_rpc_qualifies_gateway_model() -> None:
    """Gateway models launch as ``provider/model`` selectors omp can resolve."""
    from unittest.mock import AsyncMock

    executor = _make_gateway_executor()
    starter = AsyncMock()
    with (
        patch.object(omp_mod._OmpRpcSession, "start", starter),
        patch.object(OmpExecutor, "_load_gateway_model_wire_apis", new=AsyncMock(return_value={})),
    ):
        rpc = _run(executor._ensure_rpc("s", "sys", "my-claude-x", [], None))
    assert starter.call_count == 1
    assert starter.call_args.kwargs["model"] == "databricks-anthropic/my-claude-x"
    assert rpc._tmp_dir is not None
    import shutil

    shutil.rmtree(rpc._tmp_dir, ignore_errors=True)


def test_ensure_rpc_passes_through_qualified_selector() -> None:
    """An already-qualified selector is not stacked to ``provider/provider/id``."""
    from unittest.mock import AsyncMock

    executor = _make_gateway_executor()
    starter = AsyncMock()
    with (
        patch.object(omp_mod._OmpRpcSession, "start", starter),
        patch.object(OmpExecutor, "_load_gateway_model_wire_apis", new=AsyncMock(return_value={})),
    ):
        _run(executor._ensure_rpc("s", "sys", "databricks-anthropic/my-claude-x", [], None))
    assert starter.call_args.kwargs["model"] == "databricks-anthropic/my-claude-x"


def test_ensure_rpc_reroutes_mismatched_qualifier() -> None:
    """A qualifier for the wrong provider defers to routing, not the typo."""
    from unittest.mock import AsyncMock

    executor = _make_gateway_executor()
    starter = AsyncMock()
    with (
        patch.object(omp_mod._OmpRpcSession, "start", starter),
        patch.object(OmpExecutor, "_load_gateway_model_wire_apis", new=AsyncMock(return_value={})),
    ):
        _run(executor._ensure_rpc("s", "sys", "databricks/my-claude-x", [], None))
    assert starter.call_args.kwargs["model"] == "databricks-anthropic/databricks/my-claude-x"


def test_ensure_rpc_reuses_live_session() -> None:
    """Same prompt + model reuses the process instead of respawning."""
    from unittest.mock import AsyncMock

    executor = _make_executor()
    starter = AsyncMock()
    with patch.object(omp_mod._OmpRpcSession, "start", starter):
        first = _run(executor._ensure_rpc("s", "sys", None, [], None))
        # The mocked ``start`` never assigns a process; simulate the live one
        # or the reuse check (``process.returncode is None``) respawns.
        first.process = MagicMock()
        first.process.returncode = None
        first.process.wait = AsyncMock(return_value=0)
        second = _run(executor._ensure_rpc("s", "sys", None, [], None))
    assert first is second
    assert starter.call_count == 1
    _run(first.close())


def test_ensure_rpc_respawns_on_model_change() -> None:
    """A model change drops the old process and starts a fresh one."""
    from unittest.mock import AsyncMock

    executor = _make_executor()
    starter = AsyncMock()
    with patch.object(omp_mod._OmpRpcSession, "start", starter):
        first = _run(executor._ensure_rpc("s", "sys", "model-a", [], None))
        second = _run(executor._ensure_rpc("s", "sys", "model-b", [], None))
    assert first is not second
    assert starter.call_count == 2
    assert first.process is None  # closed on respawn


# ---------------------------------------------------------------------------
# _resolve_model: overrides, gateway strip, discovery
# ---------------------------------------------------------------------------


def test_resolve_model_prefers_request_override() -> None:
    """Per-request ``/model`` beats the spec default."""
    executor = _make_executor(model="spec-model")
    assert _run(executor._resolve_model(ExecutorConfig(model="req-model"))) == "req-model"
    assert _run(executor._resolve_model(None)) == "spec-model"


def test_resolve_model_strips_bracket_suffix_on_gateway() -> None:
    """``[1m]`` context hints die before the gateway (it rejects them)."""
    executor = _make_gateway_executor(model="my-claude-x[1m]")
    assert _run(executor._resolve_model(None)) == "my-claude-x"


def test_resolve_model_keeps_bracket_suffix_off_gateway() -> None:
    """Direct-provider runs pass the id through untouched."""
    executor = _make_executor(model="my-claude-x[1m]")
    assert _run(executor._resolve_model(None)) == "my-claude-x[1m]"


def test_resolve_model_discovers_databricks_default() -> None:
    """No pinned model on the profile path resolves the live Claude default."""
    from unittest.mock import AsyncMock

    executor = _make_gateway_executor()
    with patch.object(omp_mod, "run_sync_on_thread", new=AsyncMock(return_value="live-claude")):
        assert _run(executor._resolve_model(None)) == "live-claude"


def test_resolve_model_rejects_non_string_discovery() -> None:
    """A corrupt discovery result fails loudly instead of spawning blind."""
    from unittest.mock import AsyncMock

    executor = _make_gateway_executor()
    with patch.object(omp_mod, "run_sync_on_thread", new=AsyncMock(return_value=123)):
        with pytest.raises(TypeError, match="non-string"):
            _run(executor._resolve_model(None))


# ---------------------------------------------------------------------------
# _OmpRpcSession.request: correlation + line requeue
# ---------------------------------------------------------------------------


def test_request_requeues_unrelated_lines_in_order() -> None:
    """Lines arriving before the awaited response are deferred, not dropped."""
    import json as _json

    rpc = _scripted_session(
        [
            _json.dumps(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "x"},
                }
            ),
            _json.dumps({"type": "response", "command": "other", "success": True}),
            _json.dumps(
                {
                    "type": "response",
                    "command": "wanted",
                    "success": True,
                    "data": {"level": "high"},
                }
            ),
        ]
    )

    async def _call():
        return await rpc.request({"type": "wanted", "id": "w1"}, "wanted", timeout=1.0)

    response = _run(_call())
    assert response is not None
    assert response["data"] == {"level": "high"}
    # The prompt command went out on stdin; deferred lines came back in order.
    assert _run(rpc.read_line(timeout=1.0)) is not None
    assert _run(rpc.read_line(timeout=1.0)) is not None
