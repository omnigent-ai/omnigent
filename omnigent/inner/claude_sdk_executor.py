"""ClaudeSDKExecutor: run agents using the Claude Agent SDK.

Uses the ``claude-agent-sdk`` Python package to run Claude Code as the
underlying agent harness.  Omnigent tools are bridged into the SDK session
as MCP tools so Claude can call them alongside its built-in capabilities.

The SDK manages its own internal agent loop (tool calls, retries, context).
This executor translates the SDK message stream into Omnigent ExecutorEvents
and builds up the session History from observed tool-use blocks.

Requirements:
    pip install claude-agent-sdk          # optional dependency

Environment (direct Anthropic):
    ANTHROPIC_API_KEY – API key for Claude

Environment (Databricks-hosted Claude via native Anthropic Messages API):
    DATABRICKS_CONFIG_PROFILE – optional Databricks profile selector
    ~/.databrickscfg          – host + token profile for workspace access
    (or ~/.databrickscfg with a profile containing host + token)

    The executor builds ANTHROPIC_BASE_URL plus an invocation-local
    apiKeyHelper setting from Databricks credentials so Claude Code can
    refresh auth through ``databricks auth token`` while routing through
    the Databricks gateway.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import pathlib
import shutil
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Protocol, TypeAlias, cast

from omnigent._platform import stable_user_id
from omnigent.inner import _proc
from omnigent.inner.bundle_skills import ensure_bundle_plugin_manifest
from omnigent.llms._usage_observer import notify_from_dict as _notify_usage_from_dict
from omnigent.onboarding.databricks_config import DATABRICKS_CLAUDE_DEFAULT_MODEL
from omnigent.reasoning_effort import CLAUDE_EFFORTS, validate_effort
from omnigent.spec.types import RetryPolicy

from ._subprocess_lifecycle import close_anyio_subprocess_transport
from .claude_gateway_shim import DATABRICKS_CLAUDE_ADAPTIVE_THINKING_PREFIXES, ClaudeGatewayShim
from .datamodel import OSEnvSandboxSpec, OSEnvSpec
from .executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    ToolSpec,
    TurnComplete,
    classify_tool_result,
)
from .sandbox import (
    create_exec_launcher,
    resolve_sandbox,
    with_additional_read_roots,
    with_additional_write_files,
    with_additional_write_roots,
)

logger = logging.getLogger(__name__)

# Default auth-token refresh cadence (ms) for the vendor-neutral gateway
# transport when ``HARNESS_CLAUDE_SDK_GATEWAY_AUTH_REFRESH_INTERVAL_MS`` is
# unset. Not Databricks-specific: the same fallback applies to any gateway
# producer (Databricks AI gateway or a generic key/gateway provider).
_GATEWAY_AUTH_REFRESH_MS = 900_000

# ---------------------------------------------------------------------------
# TypeAliases for Omnigent JSON-shaped boundary values. The SDK exchanges
# heterogeneous dicts at the transport and tool boundaries — named aliases
# here keep the executor ``object``-free while isolating the justified
# ``explicit-any`` boundary to a single place, mirroring the peer
# ``openai_agents_sdk_executor`` / ``databricks_executor`` conventions.
# ---------------------------------------------------------------------------

# Parsed tool arguments / tool result dict — JSON-shaped bags exchanged
# with the Omnigent tool executor and the SDK's MCP bridge.
ToolArgs: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]
ToolResult: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# MCP response payload (``content`` + optional ``isError``) returned to the
# Claude SDK from each MCP tool handler.
McpResponse: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# Tool executor callable wired in by ``omnigent.Session``.
ToolExecutor: TypeAlias = Callable[[str, ToolArgs], Awaitable[ToolResult]]

# Elicitation handler wired in by :class:`ExecutorAdapter`. Kept SDK-agnostic
# so the adapter does not import ``claude_agent_sdk`` types.
ElicitationHandler: TypeAlias = Callable[  # type: ignore[explicit-any]
    [str, ToolArgs],
    Awaitable[bool],
]

# Opaque SDK artifacts whose concrete shape we don't touch directly:
# - ``SdkMcpTool``: returned by ``sdk.tool(...)`` decorator and passed back
#   to ``sdk.create_sdk_mcp_server(tools=...)`` without field access.
# - ``ClaudeAgentOptions``: the SDK's dataclass; fields set via attribute
#   assignment after construction rather than typed kwargs.
SdkMcpTool: TypeAlias = Any  # type: ignore[explicit-any]
SdkOptions: TypeAlias = Any  # type: ignore[explicit-any]


# ---------------------------------------------------------------------------
# SDK-private reach Protocols.
#
# ``claude_agent_sdk.*`` is listed as ``ignore_missing_imports`` in mypy
# config, so every SDK-typed value mypy sees is ``Any``. We recover types
# locally with Protocols for the handful of public and private attributes
# this executor touches.
#
# The private reaches (``_query``, ``_transport``, ``_process``,
# ``_stderr_task`` / ``_stderr_task_group``, etc.) are necessary to tear
# down the CLI subprocess tree when the SDK's own ``disconnect()`` path is unsafe
# (different event loop / task) or hangs. The SDK does not expose a
# supported equivalent, so we treat the private attributes as part of
# our integration contract and document them here.
# ---------------------------------------------------------------------------


class _Process(Protocol):
    """Subset of ``anyio.abc.Process`` / ``asyncio.subprocess.Process``.

    These fields are standard on both process abstractions — the SDK's
    transport uses an anyio process but the shape matches the asyncio one
    for the attributes we touch.
    """

    pid: int | None
    returncode: int | None

    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    async def wait(self) -> int: ...


class _CancelScope(Protocol):
    def cancel(self) -> None: ...


class _TaskGroup(Protocol):
    cancel_scope: _CancelScope


class _TaskHandle(Protocol):
    """Private view of the SDK's detached stderr-reader task.

    Current ``claude-agent-sdk`` (>=0.2.x) runs the stderr reader as a
    single task exposed as ``_stderr_task`` with a ``cancel()`` method;
    older revs used an anyio task group (``_stderr_task_group``). The
    executor probes both shapes during force-close, so both are typed
    optional on ``_ClaudeTransport`` below.
    """

    def cancel(self) -> None: ...


class _ClaudeQuery(Protocol):
    """Private view of ``claude_agent_sdk._internal.query.Query``.

    ``_closed`` is the SDK's "stop accepting messages" flag. ``_tg`` was a
    per-query task group in older SDK revs — absent in current revs but
    still probed so this executor handles both shapes.
    """

    _closed: bool
    _tg: _TaskGroup | None


class _Stream(Protocol):
    """Structural view of an anyio text stream. Only ``aclose`` is actually
    available on the real ``TextReceiveStream`` / ``TextSendStream``; the
    ``close`` / ``transport`` attributes probed during teardown are
    historical belt-and-suspenders cleanup and no-op on the current SDK.
    """

    async def aclose(self) -> None: ...


class _ClaudeTransport(Protocol):
    """Private view of ``SubprocessCLITransport`` internals we tear down.

    Kept minimal — only the attributes ``_force_close_client`` touches.
    """

    _process: _Process | None
    _stdout_stream: _Stream | None
    _stdin_stream: _Stream | None
    _stderr_stream: _Stream | None
    _stderr_task: _TaskHandle | None
    _stderr_task_group: _TaskGroup | None
    _ready: bool


class _ClaudeClient(Protocol):
    """Structural view of ``claude_agent_sdk.ClaudeSDKClient``.

    Covers the public methods the executor calls plus the two private
    attributes it clears during a force-close. Test doubles (see
    ``tests/test_claude_sdk_executor.py``) satisfy this Protocol
    structurally via ``SimpleNamespace`` / custom classes.
    """

    _query: _ClaudeQuery | None
    _transport: _ClaudeTransport | None

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def query(self, prompt: str, session_id: str = ...) -> None: ...
    async def set_model(self, model: str | None) -> None: ...
    async def interrupt(self) -> None: ...

    # receive_response yields heterogeneous SDK message objects — kept
    # as ``Any`` so the caller can ``isinstance``-narrow onto the SDK's
    # real types via ``_ClaudeSDK`` below without needing a union here.
    def receive_response(self) -> AsyncIterator[Any]: ...  # type: ignore[explicit-any]


class _StreamEventObj(Protocol):
    """Structural view of ``claude_agent_sdk.StreamEvent``."""

    event: dict[str, Any]  # type: ignore[explicit-any]  # SDK declares this as dict[str, Any]


class _AssistantMessageObj(Protocol):
    """Structural view of ``claude_agent_sdk.AssistantMessage``."""

    # Each content block is one of the SDK block classes; isinstance-narrowed
    # at the read sites below.
    content: list[Any]  # type: ignore[explicit-any]
    # The model the SDK actually used for this message, e.g.
    # ``"claude-opus-4-8"``. The only place the executor learns the concrete
    # model when the spec pins none and the gateway resolves it internally.
    model: str | None


class _UserMessageObj(Protocol):
    """Structural view of ``claude_agent_sdk.UserMessage``."""

    content: str | list[Any]  # type: ignore[explicit-any]


class _ResultMessageObj(Protocol):
    """Structural view of ``claude_agent_sdk.ResultMessage``."""

    result: str | None
    usage: dict[str, Any] | None  # type: ignore[explicit-any]


class _SystemMessageObj(Protocol):
    """Structural view of ``claude_agent_sdk.SystemMessage``."""

    subtype: str
    data: dict[str, Any]  # type: ignore[explicit-any]


class _TextBlockObj(Protocol):
    text: str


class _ToolUseBlockObj(Protocol):
    id: str
    name: str
    input: ToolArgs


class _ToolResultBlockObj(Protocol):
    tool_use_id: str
    content: str | list[dict[str, Any]] | None  # type: ignore[explicit-any]
    is_error: bool | None


class _ClaudeSDK(Protocol):
    """Structural view of the ``claude_agent_sdk`` module.

    Tests swap in a fake with matching attributes, so we mirror what the
    executor actually pulls off the module. The ``*Message`` / ``*Block``
    attributes are declared as ``type`` so they can be used both as
    ``isinstance`` second args and as Protocol-implementing factories.
    """

    # Factories / callables the executor invokes. ``Callable[..., X]``
    # expands to an implicit ``Any`` arg spec under
    # ``disallow_any_explicit`` — the SDK's construction kwargs are opaque
    # at our boundary so that's the right abstraction level here.
    ClaudeSDKClient: Callable[..., _ClaudeClient]  # type: ignore[explicit-any]
    ClaudeAgentOptions: Callable[..., Any]  # type: ignore[explicit-any]
    tool: Callable[..., Any]  # type: ignore[explicit-any]
    create_sdk_mcp_server: Callable[..., Any]  # type: ignore[explicit-any]

    # Classes used as isinstance second args. Declared as ``type`` so the
    # checker accepts them in isinstance() while the real attributes are
    # the SDK's concrete classes. Test doubles assign plain ``type``
    # objects which satisfy this shape.
    AssistantMessage: type
    UserMessage: type
    SystemMessage: type
    ResultMessage: type
    TextBlock: type
    ToolUseBlock: type
    ToolResultBlock: type


_CONNECT_TIMEOUT_SECONDS = 60.0
_QUERY_START_TIMEOUT_SECONDS = 30.0
# When the response stream is quiet for this long we emit a warning,
# but keep waiting — a long-running native tool can legitimately block
# the stream far longer than any fixed deadline.
_STREAM_IDLE_WARN_SECONDS = 600.0

# ── Multimodal content block conversion ──────────────────────


def _parse_data_uri(uri: str) -> tuple[str, str]:
    """
    Parse a ``data:`` URI into ``(media_type, base64_data)``.

    :param uri: A data URI, e.g.
        ``"data:image/png;base64,iVBOR..."``.
    :returns: Tuple of ``(media_type, base64_payload)``.
    :raises ValueError: If the URI is not a valid ``data:`` URI.
    """
    if not uri.startswith("data:"):
        raise ValueError(f"Not a data URI: {uri[:40]!r}")
    header, _, payload = uri[5:].partition(",")
    media_type = header.replace(";base64", "")
    return media_type, payload


def _to_anthropic_content_blocks(
    blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Convert Responses API content blocks to Anthropic Messages
    API content block format.

    Mapping:

    - ``input_text`` / ``output_text`` → ``{"type": "text", ...}``
    - ``input_image`` (with ``image_url`` data URI) →
      ``{"type": "image", "source": {"type": "base64", ...}}``
    - ``input_file`` (with ``file_data`` data URI) →
      ``{"type": "document", "source": {"type": "base64", ...}}``

    :param blocks: Responses API content block dicts.
    :returns: Anthropic API content block dicts.
    """
    result: list[dict[str, Any]] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type in ("input_text", "output_text", "text"):
            result.append({"type": "text", "text": block["text"]})
        elif block_type == "input_image":
            image_url = block.get("image_url")
            if not isinstance(image_url, str) or not image_url:
                raise ValueError(
                    "input_image block is missing the 'image_url' field. "
                    "Upload the image via the session files API and reference "
                    "it by file_id so the content resolver can inline it."
                )
            if not image_url.startswith("data:"):
                raise ValueError(
                    "input_image block has a URL instead of a data URI. "
                    "Upload the image via the session files API and reference "
                    "it by file_id instead."
                )
            media_type, data = _parse_data_uri(image_url)
            result.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": data,
                    },
                }
            )
        elif block_type == "input_file":
            file_data = block.get("file_data")
            if not isinstance(file_data, str) or not file_data:
                raise ValueError(
                    "input_file block is missing the 'file_data' field. "
                    "Upload the file via the session files API and reference "
                    "it by file_id so the content resolver can inline it."
                )
            if not file_data.startswith("data:"):
                raise ValueError(
                    "input_file block has a URL instead of a data URI. "
                    "Upload the file via the session files API and reference "
                    "it by file_id instead."
                )
            media_type, data = _parse_data_uri(file_data)
            if media_type == "application/pdf":
                # Anthropic's base64 document source only accepts PDF.
                result.append(
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": data,
                        },
                    }
                )
            else:
                # All other text files (markdown, plain text, code, etc.)
                # must use Anthropic's "text" source type with decoded content.
                text_content = base64.b64decode(data).decode("utf-8", errors="replace")
                result.append(
                    {
                        "type": "document",
                        "source": {
                            "type": "text",
                            "media_type": "text/plain",
                            "data": text_content,
                        },
                    }
                )
    return result


async def _multimodal_message_iter(
    content_blocks: list[dict[str, Any]],
    *,
    session_id: str,
) -> AsyncIterator[dict[str, Any]]:
    """
    Yield a single structured user message dict for the Claude
    SDK's ``AsyncIterable[dict]`` query path.

    The SDK transport writes each yielded dict as a JSONL line to
    the CLI's stdin. The CLI forwards the content blocks to the
    Anthropic Messages API, which supports multimodal input.

    :param content_blocks: Anthropic API content block dicts
        (output of :func:`_to_anthropic_content_blocks`).
    :param session_id: The SDK session identifier.
    :yields: A single message dict.
    """
    yield {
        "type": "user",
        "message": {"role": "user", "content": content_blocks},
        "parent_tool_use_id": None,
        "session_id": session_id,
    }


# Diagnostic knob: when set (any truthy value), skip wrapping the CLI
# via ``create_exec_launcher``. Used to isolate whether the silent
# connect hang is sandbox-related vs. inside the binary itself.
_NO_SANDBOX_ENV = "OMNIGENT_CLAUDE_SDK_NO_SANDBOX"


def _sandbox_disabled_by_env() -> bool:
    """``True`` when the diagnostic bypass env var is set to a truthy
    value. Emits a WARNING on activation so CI output unambiguously
    confirms the bypass was in effect for a given run.
    """
    if os.environ.get(_NO_SANDBOX_ENV):
        logger.warning(
            "Sandbox bypass active (%s is set); skipping create_exec_launcher.",
            _NO_SANDBOX_ENV,
        )
        return True
    return False


def _terminate_process_tree(process: _Process | None) -> None:
    _proc.terminate_tree(process)


def _kill_process_tree(process: _Process | None) -> None:
    _proc.kill_tree(process)


@contextmanager
def _unset_env_var(name: str) -> Iterator[None]:
    """
    Temporarily remove an env var from ``os.environ`` for the duration of
    the ``with`` block, then restore it (or leave it absent if it was not
    set before).

    Used around the claude-cli subprocess spawn to strip ``CLAUDECODE``
    when our own Python process is itself running under Claude Code — the
    child cli otherwise reports a "nested session" error. The SDK builds
    the child env as ``{**os.environ, **options.env, ...}``, so the only
    way to *remove* (not just override with ``""``) a key is to unset it
    in ``os.environ`` during the spawn.

    :param name: Env var name to remove for the block, e.g. ``"CLAUDECODE"``.
    :yields: Nothing; restores ``os.environ[name]`` on exit if it was set.
    """
    previous = os.environ.pop(name, None)
    try:
        yield
    finally:
        if previous is not None:
            os.environ[name] = previous


_CLOSE_ATTR: str = "close"
_TRANSPORT_ATTR: str = "transport"
_ACLOSE_ATTR: str = "aclose"


def _call_optional_method(obj: Any, name: str) -> None:  # type: ignore[explicit-any]
    """Call ``obj.<name>()`` if it exists and is callable, swallowing errors.

    Uses a runtime attribute name so this stays out of the
    ``getattr(..., "<literal>", ...)`` lint's crosshairs while still giving
    mypy a known shape (``Any`` at the boundary — the caller's concrete
    types don't declare the sync ``close`` hook we probe here).
    """
    method = getattr(obj, name, None)
    if callable(method):
        with suppress(Exception):
            method()


def _best_effort_close(resource: _Stream | _Process) -> None:
    """Invoke a best-effort synchronous close on an SDK-internal handle.

    The current SDK exposes ``aclose`` (async) on streams and a no-``close``
    anyio ``Process``; older revs and test doubles may still ship a sync
    ``close`` method. We probe for it via ``hasattr``-style helpers and
    swallow any failures — this runs only on the force-close teardown path
    where the alternative is leaking the handle.
    """
    _call_optional_method(resource, _CLOSE_ATTR)
    transport_obj = getattr(resource, _TRANSPORT_ATTR, None)
    if transport_obj is not None:
        _call_optional_method(transport_obj, _CLOSE_ATTR)


# Default model for the Databricks-profile gateway path (no gateway base URL
# supplied directly), used when no spec/cfg model is set. On the ucode-cached
# path the Omnigent producer resolves the model instead (see workflow.py).
_DATABRICKS_CLAUDE_DEFAULT_MODEL = DATABRICKS_CLAUDE_DEFAULT_MODEL

_CLAUDE_API_KEY_HELPER_ENV_KEY = "OMNIGENT_CLAUDE_API_KEY_HELPER"


@dataclass
class _ClaudeClientState:
    client: _ClaudeClient
    model: str | None
    loop: asyncio.AbstractEventLoop | None = None
    task: asyncio.Task[None] | None = None


@dataclass(frozen=True)
class PreparedClaudeCli:
    """Result of wrapping the Claude CLI in an Omnigent sandbox.

    :param cli_path: Path the SDK should exec for the Claude CLI.  May be the
        original system CLI or a generated wrapper script that applies the
        sandbox before exec-ing the real binary.  ``None`` when no CLI is
        available (no system ``claude`` on PATH).
    :param enable_native_tools: ``True`` when the sandbox is active and it is
        safe for the SDK to enable the built-in native OS tools (Bash, Read,
        Edit, …).  ``False`` when the sandbox cannot be applied (e.g. network
        denied, unsupported platform) — the caller should then leave native
        tools disabled.
    """

    cli_path: str | None
    enable_native_tools: bool


def _ensure_sdk() -> ModuleType:
    """Import and return the claude_agent_sdk module, raising a clear error if missing."""
    try:
        import claude_agent_sdk

        return claude_agent_sdk
    except ImportError as exc:
        raise ImportError(
            "ClaudeSDKExecutor requires the 'claude-agent-sdk' package. "
            "Install it with: pip install claude-agent-sdk"
        ) from exc


def _build_mcp_tools(
    tool_schemas: list[ToolSpec],
    tool_executor: ToolExecutor | None,
) -> list[SdkMcpTool]:
    """Build SdkMcpTool objects from Omnigent tool schemas.

    Each tool is backed by a handler that calls the Omnigent tool_executor
    callback, which routes through the Session's tool registry (and thus
    respects policies, history recording, etc.).
    """
    sdk = cast(_ClaudeSDK, _ensure_sdk())

    mcp_tools: list[SdkMcpTool] = []
    for schema in tool_schemas:
        raw_name = schema.get("name")
        raw_desc = schema.get("description")
        # ``sdk.tool()`` requires ``str`` for name/description — the SDK
        # itself does not accept ``None``. Omnigent tool schemas always
        # carry a ``name`` (see ``Tool.tool_schema``); fall back to ``""``
        # only for the description, which is legitimately optional.
        tname: str = raw_name if isinstance(raw_name, str) else ""
        tdesc: str = raw_desc if isinstance(raw_desc, str) else ""
        tparams = schema.get("parameters", {"type": "object", "properties": {}})

        def _make_handler(tool_name: str) -> Callable[[ToolArgs], Awaitable[McpResponse]]:
            async def handler(args: ToolArgs) -> McpResponse:
                if tool_executor is None:
                    return {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {"error": f"No tool executor for '{tool_name}'"}
                                ),
                            }
                        ],
                    }
                try:
                    # ``tool_executor`` is declared as ``Awaitable[ToolResult]``
                    # so we always await. The isinstance guard below preserves
                    # the pre-refactor safety net for unexpected non-dict
                    # runtime returns without confusing the type checker.
                    raw = await tool_executor(tool_name, args)
                    result: ToolResult = raw if isinstance(raw, dict) else {"result": raw}
                    response: McpResponse = {
                        "content": [{"type": "text", "text": json.dumps(result)}],
                    }
                    if result.get("blocked") is True or (
                        "error" in result and result.get("error")
                    ):
                        response["isError"] = True
                    return response
                except Exception as exc:  # noqa: BLE001 — tool handler converts any error to MCP error response
                    return {
                        "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
                        "isError": True,
                    }

            return handler

        decorated = sdk.tool(tname, tdesc, tparams)(_make_handler(tname))
        mcp_tools.append(decorated)
    return mcp_tools


def _augment_system_prompt_for_omnigent_mcp_tools(
    system_prompt: str,
    tool_schemas: list[ToolSpec],
) -> str:
    """
    Add Claude SDK-specific MCP tool-name guidance to the system prompt.

    Omnigent schemas use bare names such as ``sys_session_send``. The
    Claude SDK exposes tools from our in-process MCP server to the model
    as ``mcp__omnigent__<bare_name>``. Bundled agent prompts and skills use
    bare names because other executors call those directly, so the SDK needs
    a bridge note to stop the model from trying a non-existent bare tool first.
    """
    tool_names = [
        name for schema in tool_schemas if isinstance((name := schema.get("name")), str) and name
    ]
    if not tool_names:
        return system_prompt

    examples = [name for name in ("sys_session_send", "sys_session_create") if name in tool_names]
    if examples:
        example_text = "; ".join(
            f"use `mcp__omnigent__{name}` when instructions say `{name}`" for name in examples
        )
        note = (
            "Claude SDK tool naming: Omnigent tools are exposed as MCP tools. "
            f"{example_text}. For any other Omnigent tool, use "
            "`mcp__omnigent__<tool_name>` rather than the bare name."
        )
    else:
        note = (
            "Claude SDK tool naming: Omnigent tools are exposed as MCP tools. "
            "When instructions mention a bare Omnigent tool name, invoke "
            "`mcp__omnigent__<tool_name>` rather than the bare name."
        )

    if not system_prompt:
        return note
    return f"{system_prompt.rstrip()}\n\n{note}"


def _find_system_claude() -> str | None:
    """Find a system-installed ``claude`` CLI binary on PATH.

    Returns the absolute path, or None if not found.  Prefers the system
    install over the SDK's bundled CLI because the bundled version may be
    older and send beta flags the Databricks gateway doesn't support.
    """
    return shutil.which("claude")


def _resolve_gateway_env(
    profile: str | None = None,
    *,
    host_override: str | None = None,
    base_url_override: str | None = None,
    auth_command_override: str | None = None,
    auth_refresh_interval_ms: int | None = None,
) -> dict[str, str]:
    """Build Claude Code gateway env from the gateway transport values.

    The vendor-neutral gateway transport is a base URL + a bearer-token
    command + a refresh TTL. When the gateway base URL and auth command are
    supplied directly (the generic-provider producer, or ucode), they are
    used verbatim. When only a Databricks profile is supplied (no override
    values), the Databricks-specific fallback derives both from
    ``~/.databrickscfg``:
      1. ~/.databrickscfg profile credentials
      2. ~/.databrickscfg (explicit profile, DEFAULT, or first valid section)
    Returns an empty dict if no credentials are available.

    The bearer token itself is not returned. Claude Code receives an
    invocation-local ``apiKeyHelper`` setting and refresh TTL instead, so
    the CLI can periodically re-run the auth command during long sessions
    instead of inheriting a one-hour token snapshot.

    :param profile: Optional Databricks profile name from
        ``~/.databrickscfg`` (used only on the profile-derivation fallback).
    :param host_override: Gateway workspace host origin, e.g.
        ``"https://example.databricks.com"``. When set, skips
        ``~/.databrickscfg`` host lookup and requires the gateway base URL
        and auth command values.
    :param base_url_override: When set, use this as ``ANTHROPIC_BASE_URL``
        instead of deriving it from the profile host.  Populated from
        ``HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL``.
    :param auth_command_override: Shell command that prints a bearer token,
        e.g. ``"databricks auth token --host ..."`` or ``"printf %s sk-..."``.
    :param auth_refresh_interval_ms: Refresh TTL in milliseconds, e.g.
        ``900000``.
    :returns: Environment values plus an internal apiKeyHelper command
        consumed by :meth:`ClaudeSDKExecutor.run_turn`, or ``{}`` when
        no credentials are available.
    :raises OSError: If a gateway host is present but missing the
        corresponding base URL or auth command.
    """
    host = host_override.rstrip("/") if host_override else None
    if host is None and base_url_override is not None and auth_command_override is not None:
        # Generic-provider gateway: explicit base_url + auth command,
        # no Databricks host or profile required (e.g. ApiKeyAuth with
        # a mock LLM server URL).
        return {
            "ANTHROPIC_BASE_URL": base_url_override,
            "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": str(
                auth_refresh_interval_ms or _GATEWAY_AUTH_REFRESH_MS
            ),
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
            _CLAUDE_API_KEY_HELPER_ENV_KEY: auth_command_override,
        }
    if host is None:
        try:
            from .databricks_executor import _read_databrickscfg

            creds = _read_databrickscfg(profile)
        except ImportError:
            creds = None

        if creds is None:
            return {}
        host = creds.host.rstrip("/")
        base_url = (
            base_url_override if base_url_override is not None else f"{host}/ai-gateway/anthropic"
        )
        auth_command = (
            auth_command_override
            if auth_command_override is not None
            else _databricks_claude_auth_command(host, profile)
        )
    else:
        if base_url_override is None:
            raise OSError(
                "ClaudeSDKExecutor(gateway=True) with a gateway workspace host "
                "requires HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL."
            )
        if auth_command_override is None:
            raise OSError(
                "ClaudeSDKExecutor(gateway=True) with a gateway workspace host "
                "requires HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND."
            )
        base_url = base_url_override
        auth_command = auth_command_override

    return {
        "ANTHROPIC_BASE_URL": base_url,
        "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": str(
            auth_refresh_interval_ms or _GATEWAY_AUTH_REFRESH_MS
        ),
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        _CLAUDE_API_KEY_HELPER_ENV_KEY: auth_command,
    }


def _databricks_claude_auth_command(host: str, profile: str | None = None) -> str:
    """Return the legacy Databricks CLI auth helper command for Claude.

    :param host: Databricks workspace host, e.g.
        ``"https://example.databricks.com"``.
    :param profile: Optional ``~/.databrickscfg`` profile name, e.g.
        ``"oss"``. Preferred over ``--host`` when known: two profiles can
        share one host, which makes ``databricks auth token --host`` fail
        ("Use --profile to specify which profile") → empty token → 401.
        ``--profile`` is always unambiguous.
    :returns: Shell command that prints a bearer token.
    """
    # --profile is unambiguous; --host fails when two profiles share a host.
    selector = f"--profile {json.dumps(profile)}" if profile else f"--host {json.dumps(host)}"
    # `--force-refresh` proactively refreshes a still-valid cached token
    # (guards against a mid-session 401 on long gateway connections) but
    # only exists in Databricks CLI >= v0.296.0. Probe `--help` and pass it
    # only when supported: older CLIs reject the unknown flag → empty token
    # → silent 401. Plain `auth token` still auto-refreshes expired tokens.
    return (
        'if [ -n "${DATABRICKS_BEARER:-}" ]; then '
        'printf "%s\\n" "$DATABRICKS_BEARER"; '
        "else force=''; "
        "if databricks auth token --help 2>&1 | grep -q force-refresh; "
        "then force=--force-refresh; fi; "
        "env -u DATABRICKS_CONFIG_PROFILE "
        f"databricks auth token {selector} "
        "$force --output json | jq -r '.access_token'; fi"
    )


def _parse_optional_int(value: str | None) -> int | None:
    """Parse an optional integer env-var value.

    :param value: Raw env-var value, e.g. ``"900000"``.
    :returns: Parsed integer, or ``None`` when unset or invalid.
    """
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        logger.warning("Ignoring invalid integer value %r", value)
        return None


def _claude_internal_write_roots() -> list[pathlib.Path]:
    """Writable roots the Claude CLI needs for its own local session state."""

    roots = [
        pathlib.Path.home() / ".claude" / "backups",
        pathlib.Path.home() / ".claude" / "plugins",
        pathlib.Path.home() / ".claude" / "session-env",
        pathlib.Path.home() / ".claude" / "sessions",
        pathlib.Path.home() / ".npm" / "_logs",
        pathlib.Path(tempfile.gettempdir()) / f"claude-{stable_user_id()}",
    ]
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    return roots


def _claude_internal_write_files() -> list[pathlib.Path]:
    """Exact files the Claude CLI updates outside its writable roots."""

    path = pathlib.Path.home() / ".claude.json"
    return [path] if path.exists() else []


def prepare_claude_cli_path(
    real_cli_path: str | None,
    spec: OSEnvSpec | None,
) -> PreparedClaudeCli:
    """Wrap the Claude CLI in the agent's configured sandbox when possible.

    :param real_cli_path: Absolute path to the system-installed Claude CLI
        binary, or ``None`` when no CLI is available.
    :param spec: The agent's ``os_env`` spec.  Only ``caller_process`` specs
        with a compatible sandbox are eligible for wrapping.
    :returns: A :class:`PreparedClaudeCli` naming the effective CLI path and
        whether native tools should be enabled.
    """

    if real_cli_path is None or spec is None or spec.type != "caller_process":
        return PreparedClaudeCli(cli_path=real_cli_path, enable_native_tools=False)

    if _sandbox_disabled_by_env():
        return PreparedClaudeCli(cli_path=real_cli_path, enable_native_tools=False)

    sandbox_spec = spec.sandbox or OSEnvSandboxSpec()
    if sandbox_spec.type == "none":
        return PreparedClaudeCli(cli_path=real_cli_path, enable_native_tools=True)

    cwd = pathlib.Path(spec.cwd or os.getcwd()).resolve(strict=False)
    sandbox = resolve_sandbox(spec, cwd)
    if not sandbox.active:
        return PreparedClaudeCli(cli_path=real_cli_path, enable_native_tools=False)
    if not sandbox.allow_network:
        # The Claude CLI itself must reach the provider, so we cannot run the
        # whole native-tool process tree inside a network-denying sandbox.
        return PreparedClaudeCli(cli_path=real_cli_path, enable_native_tools=False)

    sandbox = with_additional_read_roots(sandbox, _claude_internal_write_roots())
    sandbox = with_additional_write_roots(sandbox, _claude_internal_write_roots())
    sandbox = with_additional_write_files(sandbox, _claude_internal_write_files())
    return PreparedClaudeCli(
        cli_path=create_exec_launcher(real_cli_path, sandbox),
        enable_native_tools=True,
    )


def prepare_tight_cli_process_path(
    real_cli_path: str | None,
    *,
    cwd: str | None = None,
) -> str | None:
    """Wrap the Claude CLI in a tight default sandbox without enabling tools."""

    if real_cli_path is None:
        return None

    if _sandbox_disabled_by_env():
        return real_cli_path

    # Skip silently on non-Linux: the implicit default sandbox here is
    # ``linux_bwrap`` and ``resolve_sandbox`` would raise
    # NotImplementedError / OSError on every macOS / Windows run. The
    # operator's only recourse is to either accept the no-op or
    # set ``os_env.sandbox.type='none'`` explicitly — both
    # already produce the same behavior we land on here, so
    # logging a warning every run is just noise (and breaks
    # tests that assert ``stderr_is_clean``).
    if sys.platform != "linux":
        return real_cli_path

    spec = OSEnvSpec(
        type="caller_process",
        cwd=cwd,
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            write_paths=[],
            allow_network=True,
        ),
    )
    try:
        resolved_cwd = pathlib.Path(cwd or os.getcwd()).resolve(strict=False)
        sandbox = resolve_sandbox(spec, resolved_cwd)
    except (OSError, NotImplementedError) as exc:
        logger.warning(
            "Could not apply default local CLI sandbox; continuing without it: %s",
            exc,
        )
        return real_cli_path

    if not sandbox.active:
        return real_cli_path
    sandbox = with_additional_write_roots(sandbox, _claude_internal_write_roots())
    sandbox = with_additional_write_files(sandbox, _claude_internal_write_files())
    return create_exec_launcher(real_cli_path, sandbox)


@dataclass(frozen=True)
class _ResolvedSkills:
    """
    Pair of SDK options derived from a single ``skills_filter``
    value: ``ClaudeAgentOptions.skills`` and
    ``ClaudeAgentOptions.setting_sources``.

    Both are needed because the SDK's ``_apply_skills_defaults``
    auto-defaults ``setting_sources`` to ``["user", "project"]``
    whenever ``skills`` is non-None — including when ``skills=[]``.
    That auto-default loads ``~/.claude/skills/`` and the cwd's
    ancestor ``.claude/skills/`` chain into the system prompt
    listing even when the ``Skill`` tool itself is suppressed.
    Hermetic agents need to explicitly override
    ``setting_sources=[]`` to actually hide host skills from the
    model's view of its own skill listing.

    :param skills: Value for ``ClaudeAgentOptions.skills``:
        ``"all"`` / list of names / empty list for hermetic mode.
    :param setting_sources: Value for
        ``ClaudeAgentOptions.setting_sources``: ``None`` to let
        the SDK pick its default (``["user", "project"]``), or
        an explicit list (e.g. ``[]`` for hermetic mode where we
        don't want any scope-based discovery).
    """

    skills: str | list[str]
    setting_sources: list[str] | None


def _resolve_skills_option(
    skills_filter: str | list[str],
) -> _ResolvedSkills | None:
    """
    Translate the spec's ``skills_filter`` into the pair of SDK
    options ``ClaudeAgentOptions.skills`` and
    ``ClaudeAgentOptions.setting_sources``.

    Three meaningful filter values produce three distinct SDK
    configurations:

    - ``"all"`` → ``skills="all"``, ``setting_sources=None`` (SDK
      auto-defaults to ``["user", "project"]``). All host skills
      from ``~/.claude/skills/`` and ``<cwd>/.claude/skills/``
      (walking up the cwd tree) appear in the model's listing.
    - ``"none"`` → ``skills=[]``, ``setting_sources=[]``. Both
      the ``Skill`` tool listing AND the scope-based discovery
      are suppressed: no host skills appear in the system
      prompt or as invokable. Bundled skills (loaded via
      ``--plugin-dir``) are unaffected by ``setting_sources``
      and remain visible.
    - ``list[str]`` → ``skills=[names]``, ``setting_sources=None``.
      Only the named subset is in the model's listing; the SDK's
      auto-default still loads user and project sources for
      CLAUDE.md and other settings.

    :param skills_filter: ``"all"`` / ``"none"`` / list of skill
        names from :class:`AgentSpec.skills_filter`.
    :returns: The :class:`_ResolvedSkills` pair, or ``None`` when
        *skills_filter* is malformed — the caller falls back to
        ``"all"`` semantics.
    """
    if skills_filter == "all":
        return _ResolvedSkills(skills="all", setting_sources=None)
    if skills_filter == "none":
        # Empty ``skills`` suppresses the listing AND empty
        # ``setting_sources`` skips the SDK's auto-default that
        # would otherwise load ``~/.claude/skills/`` for the
        # system prompt anyway.
        return _ResolvedSkills(skills=[], setting_sources=[])
    if isinstance(skills_filter, list):
        return _ResolvedSkills(skills=list(skills_filter), setting_sources=None)
    return None


class ClaudeSDKExecutor(Executor):
    """Execute agent turns using the Claude Agent SDK.

    The SDK runs Claude Code's full agent loop internally.  Omnigent tools
    are registered as MCP tools so the model can call them.  Built-in OS tools
    (Bash, Read, Edit, …) are only enabled when the agent's ``os_env`` flag
    is set.  Even without ``os_env``, Omnigent tries to place the Claude CLI
    itself in a tight default sandbox on supported Linux hosts.

    Unlike DatabricksExecutor, the SDK manages its own tool-call loop.  This
    executor yields events reconstructed from the SDK message stream:
    - ToolCallRequest for each ToolUseBlock (for history building)
    - TextChunk for streaming text
    - TurnComplete with the final result

    Multi-turn: call ``run_turn()`` repeatedly.  The executor maintains a
    persistent ``ClaudeSDKClient`` that preserves conversation context across
    turns by keeping a live Claude SDK client per Omnigent session.

    Gateway support: pass ``gateway=True`` to route through a vendor-neutral
    gateway (base URL + bearer-token command + model). The Databricks AI
    gateway is one producer of this transport (credentials resolved from
    ~/.databrickscfg via ``databricks_profile``); generic ``key`` / ``gateway``
    providers are another (base URL + auth command supplied directly).
    """

    def __init__(
        self,
        *,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,
        model: str | None = None,
        permission_mode: str = "auto",
        gateway: bool = False,
        databricks_profile: str | None = None,
        gateway_host: str | None = None,
        base_url_override: str | None = None,
        gateway_auth_command: str | None = None,
        gateway_auth_refresh_interval_ms: str | None = None,
        retry_policy: RetryPolicy | None = None,
        bundle_dir: pathlib.Path | None = None,
        agent_name: str | None = None,
        skills_filter: str | list[str] = "all",
        api_key_helper: str | None = None,
    ) -> None:
        """Create a ClaudeSDKExecutor.

        Args:
            cwd: Working directory for Claude Code.
            os_env: If set, enable built-in OS tools (Bash, Read, Edit, …)
                and align them with the provided OS environment. When the
                spec's sandbox is enabled, Omnigent wraps the Claude CLI
                in the same sandbox. If omitted, Omnigent still tries to
                sandbox the Claude CLI process itself on supported Linux
                hosts, but does not enable native OS tools.
            model: Override the model name.
            permission_mode: SDK permission mode (default: auto
                so the agent runs autonomously with background safety checks).
            gateway: If True, route through a vendor-neutral gateway
                (base URL + bearer-token command + model). Enables the
                gateway path regardless of which producer fed it (the
                Databricks AI gateway or a generic provider).
            databricks_profile: Databricks-specific config profile from
                ~/.databrickscfg, e.g. ``"<your-profile>"``.  Only used by the
                Databricks producer path (deriving base URL / auth command
                from the profile when ucode did not supply them, and for
                ``databricks auth token`` refresh). ``None`` falls back to the
                SDK's own profile resolution (``DATABRICKS_CONFIG_PROFILE``
                then the first valid section of ``~/.databrickscfg``).
            gateway_host: Gateway workspace host origin, e.g.
                ``"https://example.databricks.com"``.  Set from
                ``HARNESS_CLAUDE_SDK_GATEWAY_HOST`` (written by the AP
                workflow layer). When set, skips profile host lookup and
                requires the gateway base URL and auth command values.
            base_url_override: Override the Anthropic base URL instead of
                constructing it from the Databricks profile host.  Set from
                ``HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL`` (written by the AP
                workflow layer). Required whenever ``gateway_host`` is set.
            gateway_auth_command: Shell command that prints a bearer token,
                e.g.
                ``"databricks auth token --host https://example.databricks.com ..."``
                or ``"printf %s sk-or-..."``. Set from
                ``HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND``.
            gateway_auth_refresh_interval_ms: Refresh TTL as a string,
                e.g. ``"900000"``. Set from
                ``HARNESS_CLAUDE_SDK_GATEWAY_AUTH_REFRESH_INTERVAL_MS``.
            bundle_dir: Materialized agent-bundle root, when the agent
                ships its own ``skills/`` directory. Used to expose
                bundled skills to Claude via ``--plugin-dir <bundle>``
                (the SDK's plugin convention loads SKILL.md files from
                ``<plugin>/skills/<name>/``). ``None`` for agents
                without a bundled-skill directory — the harness skips
                the plugin-dir wiring.
            agent_name: Optional agent display name. When *bundle_dir*
                is set, used to write a one-line ``.claude-plugin/
                plugin.json`` manifest so bundled skills get clean
                ``<agent-name>:<skill-name>`` namespaced labels in
                Claude's skill listing (instead of being labeled by
                the bundle's tmp-dir basename).
            skills_filter: Host-skill filter (``"all"`` / ``"none"`` /
                ``list[str]``). Maps to the SDK's ``skills`` option:
                ``"all"`` → enable every host-discovered skill,
                ``"none"`` → empty list (no host skills exposed),
                list of names → only the named skills. Bundled
                skills loaded via ``bundle_dir`` are subject to the
                same listing filter (so ``"none"`` hides every skill
                from the model, bundled or host); agents that want
                bundled skills always visible while opting out of
                host skills should set this to a list naming their
                bundled skills explicitly. Defaults to ``"all"``.
            api_key_helper: Shell command the Claude CLI will invoke to
                retrieve a bearer token, e.g.
                ``"printf %s sk-ant-..."`` (set by the harness when
                ``executor.auth: {type: api_key, …}`` is declared).
                Injected into ``_extra_env`` as
                :data:`_CLAUDE_API_KEY_HELPER_ENV_KEY` so it reaches
                the SDK's ``settings.apiKeyHelper`` option at turn time.
        """
        # Fail loud: a ``databricks-*`` model requires the gateway transport.
        if not gateway and model is not None and model.startswith("databricks-"):
            raise ValueError(
                f"Model {model!r} is a Databricks-hosted model but gateway "
                "routing is disabled (gateway=False). "
                "Set executor.profile in the agent spec, or configure a "
                "Databricks provider with `omnigent setup`, to route through "
                "the Databricks Anthropic gateway."
            )
        self._cwd = cwd
        self._os_env_spec = os_env
        self._os_env = os_env is not None
        self._model_override = model
        self._permission_mode = permission_mode
        self._gateway = gateway
        self._databricks_profile = databricks_profile
        self._gateway_host = gateway_host.rstrip("/") if gateway_host else None
        self._base_url_override = base_url_override
        self._gateway_auth_command = gateway_auth_command
        self._gateway_auth_refresh_interval_ms = _parse_optional_int(
            gateway_auth_refresh_interval_ms
        )
        self._bundle_dir = bundle_dir
        self._agent_name = agent_name
        self._skills_filter = skills_filter
        # Write the bundle's plugin manifest now (idempotent) so that
        # ``--plugin-dir <bundle>`` produces clean
        # ``<agent-name>:<skill-name>`` labels in Claude's skill
        # listing instead of the auto-derived tmpdir basename.
        if self._bundle_dir is not None:
            try:
                ensure_bundle_plugin_manifest(self._bundle_dir, self._agent_name)
            except OSError as exc:
                logger.warning(
                    "could not write bundle plugin manifest at %s: %s",
                    self._bundle_dir,
                    exc,
                )
        self._tool_executor: ToolExecutor | None = None
        # Elicitation handler wired in by ExecutorAdapter. When set
        # (and permission_mode is not bypassPermissions), each tool
        # call is gated by an async approve/deny round-trip through
        # the Omnigent elicitation system rather than silently allowed.
        # ``None`` until the adapter installs it on first use.
        self._elicitation_handler: ElicitationHandler | None = None
        # Live Claude SDK clients keyed by Omnigent session id.
        self._clients: dict[str, _ClaudeClientState] = {}
        # Session keys whose Claude harness process crashed and must not be reused.
        self._crashed_sessions: dict[str, str] = {}

        # Prefer system-installed claude over the SDK's bundled CLI.
        # The bundled CLI may be older and send beta flags that the
        # Databricks gateway doesn't support.
        self._cli_wrapper_path: str | None = None
        self._cli_path: str | None = _find_system_claude()
        if self._cli_path:
            if self._os_env_spec is not None:
                prepared = prepare_claude_cli_path(
                    self._cli_path,
                    self._os_env_spec,
                )
                self._os_env = prepared.enable_native_tools
                if prepared.cli_path != self._cli_path:
                    self._cli_wrapper_path = prepared.cli_path
                    self._cli_path = prepared.cli_path
            else:
                wrapped_cli = prepare_tight_cli_process_path(
                    self._cli_path,
                    cwd=self._cwd,
                )
                if wrapped_cli != self._cli_path:
                    self._cli_wrapper_path = wrapped_cli
                    self._cli_path = wrapped_cli
            if self._os_env_spec is not None and self._cwd is None:
                self._cwd = self._os_env_spec.cwd
            logger.info("Using system claude CLI: %s", self._cli_path)
        else:
            logger.info("No system claude found; SDK will use bundled CLI")

        # True when the gateway transport was derived from a ~/.databrickscfg
        # profile (no gateway host or base URL supplied directly). Gates the
        # Databricks-specific default model in :meth:`run_turn`; the neutral
        # generic-provider gateway path leaves this False so it never selects
        # a ``databricks-*`` model.
        self._gateway_uses_databricks_profile = bool(
            gateway and self._gateway_host is None and base_url_override is None
        )

        # Lazily-started local proxy that restores request fields the
        # Claude CLI strips on the gateway path (thinking.display).
        # Started on the first gateway turn — __init__ has no event loop.
        self._gateway_shim: ClaudeGatewayShim | None = None

        # Eagerly resolve the gateway transport env so errors surface at
        # construction time.
        self._extra_env: dict[str, str] = {}
        if gateway:
            self._extra_env = _resolve_gateway_env(
                databricks_profile,
                host_override=self._gateway_host,
                base_url_override=base_url_override,
                auth_command_override=self._gateway_auth_command,
                auth_refresh_interval_ms=self._gateway_auth_refresh_interval_ms,
            )
            if not self._extra_env:
                raise OSError(
                    "ClaudeSDKExecutor(gateway=True) requires gateway credentials "
                    "from the gateway base URL / auth command or a valid "
                    "~/.databrickscfg profile."
                )

        # Retry policy → Anthropic SDK env vars passed to the Claude
        # CLI subprocess. ``ANTHROPIC_MAX_RETRIES`` and
        # ``ANTHROPIC_REQUEST_TIMEOUT_SECONDS`` are speculative — the
        # CLI's retry budget isn't publicly documented as env-tunable.
        # See ``RetryPolicy.claude_cli.env()``.
        self._retry_policy = retry_policy if retry_policy is not None else RetryPolicy()
        self._extra_env.update(self._retry_policy.claude_cli.env())

        # api_key_helper: shell command the Claude CLI invokes to retrieve a
        # bearer token (``executor.auth: {type: api_key, …}`` path).  Must
        # be injected here (not read from os.environ) because
        # ``_get_or_create_client`` strips ``ANTHROPIC_API_KEY`` from
        # os.environ before connecting, and the executor reads
        # ``_CLAUDE_API_KEY_HELPER_ENV_KEY`` from ``_extra_env`` only.
        if api_key_helper:
            self._extra_env[_CLAUDE_API_KEY_HELPER_ENV_KEY] = api_key_helper

    def __del__(self) -> None:
        if getattr(self, "_cli_wrapper_path", None):
            with suppress(Exception):
                pathlib.Path(self._cli_wrapper_path).unlink(missing_ok=True)

    async def _route_options_through_gateway_shim(self, options: SdkOptions) -> None:
        """
        Point a new client's ``ANTHROPIC_BASE_URL`` at the local shim.

        On the gateway path the Claude CLI strips ``thinking.display``
        from its requests (experimental betas are disabled there),
        which silences opus thinking; the shim restores the field. See
        the :mod:`~omnigent.inner.claude_gateway_shim` module
        docstring for the full failure chain. No-op off the gateway
        path.

        :param options: SDK options about to be passed to
            ``ClaudeSDKClient``; ``options.env["ANTHROPIC_BASE_URL"]``
            is rewritten in place to the shim's loopback URL.
        :raises RuntimeError: If the gateway path produced options
            without an ``ANTHROPIC_BASE_URL`` — a config bug that
            would silently bypass the shim.
        """
        if not self._gateway:
            return
        env = getattr(options, "env", None)
        if not isinstance(env, dict) or "ANTHROPIC_BASE_URL" not in env:
            raise RuntimeError(
                "ClaudeSDKExecutor(gateway=True) built SDK options without "
                "env['ANTHROPIC_BASE_URL']; cannot route through the gateway shim."
            )
        if self._gateway_shim is None:
            self._gateway_shim = ClaudeGatewayShim(upstream_base_url=env["ANTHROPIC_BASE_URL"])
        await self._gateway_shim.start()
        env["ANTHROPIC_BASE_URL"] = self._gateway_shim.base_url

    async def _get_or_create_client(
        self,
        sdk: _ClaudeSDK,
        *,
        session_key: str,
        options: SdkOptions,
        model: str | None,
    ) -> _ClaudeClient:
        state = self._clients.get(session_key)
        if state is None:
            await self._route_options_through_gateway_shim(options)
            # Tee CLI stderr so the connect timeout error carries the
            # tail; ``_on_stderr`` alone only logs at DEBUG.
            connect_stderr: list[str] = []
            original_stderr = getattr(options, "stderr", None)

            def _tee_stderr(line: str) -> None:
                connect_stderr.append(line)
                if original_stderr is not None:
                    original_stderr(line)

            options.stderr = _tee_stderr
            client = sdk.ClaudeSDKClient(options)
            try:
                # CLAUDECODE must be absent (not just empty) in the child
                # env — otherwise the claude cli reports a nested-session
                # error. The SDK merges ``os.environ`` with ``options.env``,
                # so we unset in ``os.environ`` for the spawn window.
                #
                # ANTHROPIC_API_KEY is also stripped so the CLI uses its
                # subscription auth rather than a developer API key that
                # would charge separately. Safe even in Databricks mode:
                # ``options.settings`` explicitly sets apiKeyHelper and
                # ``options.env`` sets the Databricks base URL, so the
                # Claude CLI does not need an inherited Anthropic key.
                with _unset_env_var("CLAUDECODE"), _unset_env_var("ANTHROPIC_API_KEY"):
                    await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT_SECONDS)
            except asyncio.TimeoutError as exc:
                await self._force_close_client(client)
                tail = "\n".join(line.rstrip() for line in connect_stderr[-40:])
                detail = tail or "(no CLI stderr captured)"
                logger.warning(
                    "Claude SDK connect timed out after %ds; CLI stderr tail:\n%s",
                    int(_CONNECT_TIMEOUT_SECONDS),
                    detail,
                )
                raise TimeoutError(
                    f"Claude SDK client connect timed out after "
                    f"{int(_CONNECT_TIMEOUT_SECONDS)}s. CLI stderr tail:\n{detail}"
                ) from exc
            except Exception as exc:
                # The CLI may exit immediately (e.g. ``unknown option``) before
                # the SDK's async stderr reader has flushed all output.  A brief
                # yield lets pending reader callbacks deliver remaining lines so
                # the error message is useful rather than just "Command failed
                # with exit code 1; Check stderr output for details".
                await asyncio.sleep(0.1)
                await self._force_close_client(client)
                tail = "\n".join(line.rstrip() for line in connect_stderr[-40:])
                detail = tail or "(no CLI stderr captured)"
                logger.error(
                    "Claude SDK connect failed: %s\nCLI stderr tail:\n%s",
                    exc,
                    detail,
                )
                raise RuntimeError(
                    f"Claude SDK connect failed: {exc}\nCLI stderr:\n{detail}"
                ) from exc
            finally:
                # Restore on both paths so post-connect stderr flows
                # directly to the original callback and ``connect_stderr``
                # can be GC'd instead of growing for the session lifetime.
                options.stderr = original_stderr
            current_task: asyncio.Task[None] | None = cast(
                "asyncio.Task[None] | None", asyncio.current_task()
            )
            state = _ClaudeClientState(
                client=client,
                model=model,
                loop=asyncio.get_running_loop(),
                task=current_task,
            )
            self._clients[session_key] = state
            return client

        if state.model != model:
            await state.client.set_model(model)
            state.model = model
        return state.client

    async def close_session(self, session_key: str) -> None:
        self._crashed_sessions.pop(session_key, None)
        await self._close_live_client(session_key)

    async def _close_live_client(self, session_key: str) -> None:
        state = self._clients.pop(session_key, None)
        if state is None:
            return
        try:
            current_loop = asyncio.get_running_loop()
            current_task = asyncio.current_task()
        except RuntimeError:
            current_loop = None
            current_task = None

        same_loop = state.loop is None or current_loop is state.loop
        same_task = state.task is None or current_task is state.task
        if not (same_loop and same_task):
            logger.warning(
                "Force-closing Claude SDK client for session %s from a different event loop/task",
                session_key,
            )
            await self._force_close_client(state.client)
            return
        try:
            await state.client.disconnect()
        except RuntimeError as exc:
            if "different task" not in str(exc):
                raise
            logger.warning(
                "Force-closing Claude SDK client for session %s from a different task",
                session_key,
            )
            await self._force_close_client(state.client)
            return
        # SDK's disconnect() doesn't close the asyncio transport;
        # call _force_close_client to flip transport._closed before
        # the loop tears down.
        await self._force_close_client(state.client)

    async def close(self) -> None:
        session_keys = list(self._clients)
        for session_key in session_keys:
            await self.close_session(session_key)
        if self._gateway_shim is not None:
            await self._gateway_shim.aclose()
            self._gateway_shim = None

    async def interrupt_session(self, session_key: str) -> bool:
        state = self._clients.get(session_key)
        if state is None:
            return False
        # Interrupt is best-effort and fast; a failure just falls through to
        # the close below.
        try:
            await asyncio.wait_for(state.client.interrupt(), timeout=0.5)
        except Exception as exc:  # noqa: BLE001 — interrupt is best-effort
            logger.warning(
                "Claude SDK interrupt failed for session %s: %s",
                session_key,
                exc,
            )
        # Always drop the live session after an interrupt. Its transcript
        # still holds the abandoned prompt, and resumed turns send only the
        # latest user message (see _build_prompt), so the runner's
        # "[System: interrupted]" marker would never reach the model. Closing
        # forces the next turn to rebuild full history (marker included) in a
        # fresh session — the abandoned request is then visible-but-superseded
        # rather than silently continued.
        try:
            await self.close_session(session_key)
            return True
        except Exception as exc:  # noqa: BLE001 — close failures surface via False return
            logger.warning(
                "Claude SDK session close after interrupt failed for session %s: %s",
                session_key,
                exc,
            )
            return False

    async def enqueue_session_message(
        self,
        session_key: str,
        content: str | Message,
    ) -> bool:
        state = self._clients.get(session_key)
        if state is None:
            return False
        try:
            if isinstance(content, str):
                prompt = content
            else:
                prompt = json.dumps(content, ensure_ascii=True)
            await state.client.query(prompt, session_id=session_key)
            return True
        except Exception as exc:  # noqa: BLE001 — enqueue returns False on any SDK failure
            logger.warning(
                "Claude SDK live message enqueue failed for session %s: %s",
                session_key,
                exc,
            )
            return False

    @staticmethod
    async def _force_close_client(client: _ClaudeClient) -> None:
        # getattr defensively: runs on the success path too, so must
        # no-op against test fakes that only stub `disconnect`.
        query = getattr(client, "_query", None)
        if query is not None:
            query._closed = True
            tg = getattr(query, "_tg", None)
            if tg is not None:
                with suppress(Exception):
                    tg.cancel_scope.cancel()

        transport = getattr(client, "_transport", None)
        if transport is not None:
            # The SDK's stderr reader changed shape across revs: current
            # claude-agent-sdk (>=0.2.x) exposes a single ``_stderr_task``
            # TaskHandle with ``cancel()``; older revs an anyio
            # ``_stderr_task_group``. Probe both via getattr so a force-close
            # never raises AttributeError out of lifespan shutdown (which
            # crashed the runner on session stop).
            stderr_task = getattr(transport, "_stderr_task", None)
            if stderr_task is not None:
                with suppress(Exception):
                    stderr_task.cancel()
            else:
                stderr_tg = getattr(transport, "_stderr_task_group", None)
                if stderr_tg is not None:
                    with suppress(Exception):
                        stderr_tg.cancel_scope.cancel()

            for stream in (
                transport._stdout_stream,
                transport._stdin_stream,
                transport._stderr_stream,
            ):
                if stream is not None:
                    _best_effort_close(stream)

            process = transport._process
            if process is not None:
                if process.returncode is None:
                    _terminate_process_tree(process)
                    try:
                        with suppress(Exception):
                            await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        _kill_process_tree(process)
                        with suppress(Exception):
                            await process.wait()
                    except asyncio.CancelledError:
                        _kill_process_tree(process)
                        with suppress(Exception):
                            await process.wait()

                _best_effort_close(process)
                # The SDK's `transport._process` is an anyio Process;
                # `_best_effort_close` can't reach its asyncio
                # transport (no sync `close`). Close it explicitly.
                close_anyio_subprocess_transport(process)

            transport._process = None
            transport._stdout_stream = None
            transport._stdin_stream = None
            transport._stderr_stream = None
            if getattr(transport, "_stderr_task", None) is not None:
                transport._stderr_task = None
            if getattr(transport, "_stderr_task_group", None) is not None:
                transport._stderr_task_group = None
            transport._ready = False

        client._query = None
        client._transport = None

    def supports_streaming(self) -> bool:
        return True

    def supports_tool_calling(self) -> bool:
        return True

    def handles_tools_internally(self) -> bool:
        return True

    def supports_live_message_queue(self) -> bool:
        return True

    def supports_tool_boundary_interrupt(self) -> bool:
        return True

    def max_context_tokens(self) -> int | None:
        return None  # SDK manages its own context

    def _session_key(self, messages: list[Message]) -> str:
        for msg in reversed(messages):
            session_id = msg.get("session_id")
            if session_id:
                return str(session_id)
            metadata = msg.get("metadata", {})
            if isinstance(metadata, dict) and metadata.get("session_id"):
                return str(metadata["session_id"])
        return "default"

    async def _can_use_tool_for_permission(
        self,
        tool_name: str,
        tool_input: ToolArgs,
        perm_ctx: Any,  # type: ignore[explicit-any]  # ToolPermissionContext — avoid hard sdk import
    ) -> Any:  # type: ignore[explicit-any]  # PermissionResult
        """
        Route a Claude SDK permission request through the Omnigent elicitation system.

        Installed as ``options.can_use_tool`` when ``permission_mode`` is
        not ``"bypassPermissions"`` and an elicitation handler has been wired
        in by :class:`omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter`.
        Called by the SDK before each tool invocation.

        :param tool_name: Name of the tool Claude wants to call,
            e.g. ``"Bash"``.
        :param tool_input: Arguments dict for the tool call,
            e.g. ``{"command": "ls -la"}``.
        :param perm_ctx: :class:`claude_agent_sdk.ToolPermissionContext`
            carrying ``tool_use_id`` (the SDK-assigned id for this specific
            invocation), ``agent_id`` (non-None inside sub-agents), and
            ``suggestions`` (permission hints from the CLI). Used here for
            diagnostic logging so unexpected permission requests are traceable.
        :returns: :class:`claude_agent_sdk.PermissionResultAllow` when the
            user approves, or :class:`claude_agent_sdk.PermissionResultDeny`
            with a ``message`` when they deny.
        """
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        tool_use_id: str | None = getattr(perm_ctx, "tool_use_id", None)
        logger.debug(
            "permission request: tool=%s tool_use_id=%s",
            tool_name,
            tool_use_id,
        )
        if self._elicitation_handler is None:
            # No handler installed — grant by default. This branch is
            # unreachable in normal operation (run_turn only sets
            # can_use_tool when _elicitation_handler is not None), but
            # is included for safety if the SDK ever calls the callback
            # after the handler was cleared.
            return PermissionResultAllow()
        allowed = await self._elicitation_handler(tool_name, tool_input)
        if allowed:
            return PermissionResultAllow()
        return PermissionResultDeny(message="Denied via Omnigent elicitation")

    async def _evaluate_tool_call_policy(
        self,
        tool_name: str,
        tool_input: ToolArgs,
    ) -> Any:  # type: ignore[explicit-any]  # PermissionResult | None
        """
        Run a pre-execution TOOL_CALL policy evaluation for one tool call.

        This is the policy half of the ``can_use_tool`` gate. It exists
        so connector-native MCP tools — ones injected by the Claude Agent
        SDK / claude.ai connector layer (e.g. ``mcp__github__*``,
        ``mcp__atlassian__*``) that are NOT part of the agent spec's
        ``mcp_servers`` and execute INSIDE the CLI subprocess — get
        evaluated against Omnigent TOOL_CALL-phase policies BEFORE they
        run. Without this gate those calls bypass policy entirely (the
        executor only OBSERVES them in the message stream, which posts no
        policy event).

        Double-evaluation guard: Omnigent's OWN tools are exposed as the
        single ``omnigent`` SDK MCP server (the model sees
        ``mcp__omnigent__*``). When the model calls one, the SDK wrapper
        routes it back through Omnigent's dispatch bridge
        (``_stable_tool_executor`` -> ``TurnContext.dispatch_tool`` ->
        ``action_required``), and the runner re-dispatches it via
        ``ProxyMcpManager``, which enforces TOOL_CALL + TOOL_RESULT
        policies server-side before forwarding to ``/mcp/execute``
        (see ``omnigent/runner/app.py`` "All tool calls go through AP:/mcp
        ... which enforces TOOL_CALL + TOOL_RESULT policies server-side").
        Spec-declared MCP tools are surfaced through that same
        ``mcp__omnigent__*`` server, so they are covered there too.
        Evaluating ``mcp__omnigent__*`` here as well would double-count
        the same call (and could double-charge a cost-budget checkpoint),
        so we SKIP that prefix and only gate the connector-native /
        out-of-band tools the dispatch path never sees.

        :param tool_name: Full SDK tool name, e.g.
            ``"mcp__github__issue_write"``.
        :param tool_input: Arguments dict for the tool call.
        :returns: :class:`claude_agent_sdk.PermissionResultDeny` when a
            policy denies the call. ``POLICY_ACTION_ASK`` is normally
            collapsed to a hard ALLOW/DENY by the server-side
            ``/policies/evaluate`` route. If a raw ASK reaches this callback
            (for example from a read-only evaluation path), this hook runs the
            existing Omnigent elicitation handler before returning
            :class:`claude_agent_sdk.PermissionResultAllow` or DENY; without
            a handler it fails closed. Returns ``None`` when the call should
            be allowed to proceed (no policy evaluator wired, an
            ``mcp__omnigent__*`` tool already gated on the dispatch path, or
            an ALLOW / no-match verdict). Returning ``None`` lets the caller
            fall through to its remaining gate logic (elicitation) without
            forcing an allow.
        """
        _policy_eval = getattr(self, "_policy_evaluator", None)
        if _policy_eval is None:
            return None
        # Omnigent's own tools are already TOOL_CALL-gated server-side via
        # the dispatch bridge / ProxyMcpManager — don't evaluate them twice.
        if tool_name.startswith("mcp__omnigent__"):
            return None
        _verdict = await _policy_eval(
            "PHASE_TOOL_CALL",
            {"name": tool_name, "arguments": tool_input},
        )
        _action = getattr(_verdict, "action", None)
        if _action in ("POLICY_ACTION_ALLOW", "POLICY_ACTION_UNSPECIFIED"):
            # ALLOW / no-match — fall through (caller decides whether to also
            # run the human-consent elicitation gate).
            return None
        if _action == "POLICY_ACTION_ASK":
            from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

            reason = _verdict.reason or "Approval required by Omnigent TOOL_CALL policy"
            if self._elicitation_handler is None:
                logger.warning(
                    "TOOL_CALL policy ASK had no elicitation handler; denying tool=%s reason=%s",
                    tool_name,
                    reason,
                )
                return PermissionResultDeny(message=reason)
            logger.info("TOOL_CALL policy requested approval tool=%s reason=%s", tool_name, reason)
            if await self._elicitation_handler(tool_name, tool_input):
                return PermissionResultAllow()
            return PermissionResultDeny(message=reason)
        if _action == "POLICY_ACTION_DENY":
            from claude_agent_sdk import PermissionResultDeny

            reason = _verdict.reason or "Denied by Omnigent TOOL_CALL policy"
            logger.info("TOOL_CALL policy denied tool=%s reason=%s", tool_name, reason)
            return PermissionResultDeny(message=reason)
        from claude_agent_sdk import PermissionResultDeny

        reason = f"Unexpected Omnigent TOOL_CALL policy verdict: {_action!r}"
        logger.warning("TOOL_CALL policy failed closed tool=%s reason=%s", tool_name, reason)
        return PermissionResultDeny(message=reason)

    async def _can_use_tool_gate(
        self,
        tool_name: str,
        tool_input: ToolArgs,
        perm_ctx: Any,  # type: ignore[explicit-any]  # ToolPermissionContext
    ) -> Any:  # type: ignore[explicit-any]  # PermissionResult
        """
        Unified ``options.can_use_tool`` callback for the claude-sdk path.

        Composes two independent gates, in order:

        1. **TOOL_CALL policy** (always, when a ``_policy_evaluator`` is
           wired): a hard DENY short-circuits to
           :class:`~claude_agent_sdk.PermissionResultDeny`. This runs in
           EVERY permission mode — including ``bypassPermissions`` — so
           connector-native MCP tools can't slip past policy. ALLOW /
           no-match falls through with no human interaction, preserving
           ``bypassPermissions`` ergonomics for un-gated tools.
        2. **Human-consent elicitation** (only when the permission mode is
           NOT ``bypassPermissions`` and an elicitation handler is wired):
           the pre-existing per-tool approval prompt. Under
           ``bypassPermissions`` this step is skipped entirely, so the
           model still acts autonomously for anything policy allows.

        :param tool_name: Full SDK tool name, e.g. ``"Bash"`` or
            ``"mcp__github__issue_write"``.
        :param tool_input: Arguments dict for the tool call.
        :param perm_ctx: :class:`claude_agent_sdk.ToolPermissionContext`.
        :returns: A :class:`~claude_agent_sdk.PermissionResult`.
        """
        from claude_agent_sdk import PermissionResultAllow

        policy_result = await self._evaluate_tool_call_policy(tool_name, tool_input)
        if policy_result is not None:
            # Hard DENY from policy — block before execution.
            return policy_result
        # Policy allowed (or no policy gate). Under bypassPermissions we
        # never prompt; otherwise defer to the elicitation gate.
        if self._permission_mode == "bypassPermissions" or self._elicitation_handler is None:
            return PermissionResultAllow()
        return await self._can_use_tool_for_permission(tool_name, tool_input, perm_ctx)

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """Run one turn via the Claude Agent SDK.

        The SDK receives the latest user message as a prompt and runs its full
        agent loop (which may include multiple internal tool calls).  We
        observe the message stream and yield ExecutorEvents for the Session
        to record in History.
        """
        sdk = cast(_ClaudeSDK, _ensure_sdk())
        from claude_agent_sdk.types import StreamEvent as _StreamEvent

        cfg = config or ExecutorConfig()

        session_key = self._session_key(messages)
        crashed_reason = self._crashed_sessions.get(session_key)
        if crashed_reason is not None:
            yield ExecutorError(
                message=(
                    "Claude SDK session crashed and cannot continue in this Session. "
                    f"Start a new Session. Cause: {crashed_reason}"
                )
            )
            return
        prompt = self._build_prompt(
            messages,
            resume_session=session_key in self._clients,
        )
        if not prompt:
            # Resumed sessions can have nothing new to say; signal turn
            # completion with no assistant text instead of an empty string.
            yield TurnComplete(response=None)
            return

        # Build MCP tools from Omnigent tool schemas
        mcp_tools = _build_mcp_tools(tools, self._tool_executor) if tools else []

        # Create MCP server config for Omnigent tools. The SDK's
        # ``McpServerConfig`` union is opaque to us — we pass through
        # whatever ``create_sdk_mcp_server`` returns.
        mcp_servers: dict[str, Any] = {}  # type: ignore[explicit-any]
        if mcp_tools:
            mcp_servers["omnigent"] = sdk.create_sdk_mcp_server(
                name="omnigent",
                version="1.0.0",
                tools=mcp_tools,
            )
            system_prompt = _augment_system_prompt_for_omnigent_mcp_tools(
                system_prompt,
                tools,
            )

        # Build allowed_tools list.  OS-environment operations route
        # through Omnigent ``sys_os_*`` MCP tools rather than the
        # SDK's native Bash/Read/Edit/Write — MCP tools flow through
        # the scaffold's ``dispatch_tool`` path, giving the runner
        # visibility, timeouts, and error recovery.
        #
        # In ``auto`` and ``bypassPermissions`` modes, pre-approve all
        # MCP tools so the agent can act autonomously without a per-call
        # human-consent gate.  ``auto`` still runs background safety
        # checks; ``bypassPermissions`` skips all gates entirely.
        # In any other mode (``default``, ``acceptEdits``, etc.), leave
        # ``allowed_tools`` empty so every tool call goes through the
        # SDK's ``can_use_tool`` callback — which routes to the AP
        # elicitation system when an elicitation handler is wired in.
        # When ``allowed_tools`` is empty the SDK omits ``--allowedTools``
        # entirely, letting Claude's normal permission flow apply.
        allowed_tools: list[str] = []
        if self._permission_mode in ("auto", "bypassPermissions"):
            # Allow all Omnigent MCP tools (no per-call human gate needed)
            for schema in tools:
                raw_tname = schema.get("name")
                # Claude SDK's ``allowed_tools`` requires concrete strings;
                # Omnigent tool schemas always carry a name (see
                # ``Tool.tool_schema``), but defend against malformed specs
                # by skipping unnamed entries rather than producing a
                # bogus ``mcp__omnigent__`` allow-entry.
                if not isinstance(raw_tname, str) or not raw_tname:
                    continue
                allowed_tools.append(f"mcp__omnigent__{raw_tname}")

        # cfg.model > spec model > Databricks default (only on the
        # Databricks-profile gateway path) > None (lets the SDK pick its own
        # default). The neutral generic-provider gateway path never falls back
        # to a ``databricks-*`` model: the Omnigent producer always resolves a
        # concrete model (spec > provider default > catalog default) before
        # spawning, so no ``databricks-*`` default is injected there.
        model = cfg.model or self._model_override
        if model is None and self._gateway_uses_databricks_profile:
            model = _DATABRICKS_CLAUDE_DEFAULT_MODEL

        # Build env: Databricks gateway settings derived from profile-backed
        # creds. CLAUDECODE removal happens around the subprocess spawn in
        # ``_get_or_create_client`` via ``_unset_env_var`` — setting it to
        # ``""`` here would still leave an empty key in the child env.
        env = dict(self._extra_env)
        api_key_helper = env.pop(_CLAUDE_API_KEY_HELPER_ENV_KEY, None)
        settings_payload = (
            json.dumps({"apiKeyHelper": api_key_helper}, separators=(",", ":"))
            if api_key_helper
            else None
        )

        # Capture stderr from the CLI subprocess for diagnostics
        stderr_lines: list[str] = []

        def _on_stderr(line: str) -> None:
            stderr_lines.append(line)
            logger.debug("Claude CLI stderr: %s", line)

        # Build options.
        #
        # ``skills="all"`` makes the Claude Agent SDK auto-configure
        # the ``Skill`` tool in ``allowed_tools`` and default
        # ``setting_sources`` to ``["user", "project"]`` so the CLI
        # discovers user-installed skills under ``~/.claude/skills/``
        # and project-local skills under ``<cwd>/.claude/skills/``.
        # See ``claude_agent_sdk._internal.transport.subprocess_cli.
        # _apply_skills_defaults`` for the auto-derivation.
        #
        # ``tools`` is the model's BASE tool set; ``allowed_tools``
        # is just a permission filter on it. ``skills="all"`` only
        # adds ``Skill`` to ``allowed_tools`` — to actually expose
        # the tool to the model we have to put it in ``tools`` too.
        # Without this, the SDK passes ``--tools ""`` to the CLI
        # which ZEROS the base set, and the agent never sees a
        # ``Skill`` tool even with ``--allowedTools=Skill`` set
        # (the live regression that makes the agent answer "I
        # don't have a Skill tool exposed in this session").
        #
        # ``--bare`` (formerly in ``extra_args``) is intentionally
        # NOT passed: bare mode skips CLAUDE.md auto-discovery,
        # plugin sync, and auto-memory — exactly the host config
        # users expect to leak through to a ``claude-sdk`` harness
        # they explicitly opted into. ``no-session-persistence``
        # stays because omnigent owns conversation persistence
        # via its own conversation store.
        # OS-environment tools are provided via Omnigent ``sys_os_*``
        # MCP tools (declared via ``os_env`` in the spec), not the
        # SDK's native Bash/Read/Edit/Write.  Only the Skill tool
        # needs to be in the SDK's base set.
        base_tools: list[str] = ["Skill"]
        # Translate the spec's host-skill filter into the SDK
        # options. Falls back to ``"all"`` semantics when the
        # field is malformed (the parser already validates, so
        # this is belt-and-suspenders).
        resolved = _resolve_skills_option(self._skills_filter) or _ResolvedSkills(
            skills="all", setting_sources=None
        )
        # Bundle skills are exposed via the SDK's plugin mechanism.
        # The bundle's ``<bundle>/skills/<name>/SKILL.md`` files are
        # discovered as plugin skills (no ``.claude/`` prefix needed
        # under the plugin convention — see plugin discovery test in
        # tests/inner/test_claude_sdk_executor.py). The plugin's
        # ``name`` (and thus the skill-listing prefix) comes from
        # the manifest written at construction time.
        bundle_plugins: list[Any] = []  # type: ignore[explicit-any]  # SdkPluginConfig is a TypedDict — typed Any here to keep the import lazy
        if self._bundle_dir is not None:
            bundle_plugins.append({"type": "local", "path": str(self._bundle_dir)})
        options_kwargs: dict[str, Any] = {  # type: ignore[explicit-any]  # ClaudeAgentOptions accepts mixed-typed kwargs (str / list / dict / callable / etc.)
            "tools": base_tools,
            "system_prompt": system_prompt or None,
            "mcp_servers": mcp_servers if mcp_servers else {},
            "allowed_tools": allowed_tools,
            "permission_mode": self._permission_mode,
            "max_turns": cfg.extra.get("max_turns"),
            "env": env,
            "settings": settings_payload,
            "stderr": _on_stderr,
            "include_partial_messages": True,
            "include_hook_events": True,
            "skills": resolved.skills,
            "plugins": bundle_plugins,
            "extra_args": {"no-session-persistence": None},
            "max_buffer_size": 10 * 1024 * 1024,
        }
        # Only forward ``setting_sources`` when explicitly set.
        # ``None`` lets the SDK apply its default
        # (``["user", "project"]`` when ``skills`` is non-None).
        # An empty list — produced by ``"none"`` — is forwarded
        # verbatim so the SDK doesn't auto-default it back to
        # ``["user", "project"]``, which would re-load host
        # skills into the model's system prompt despite
        # ``skills=[]`` (the live regression that prompted this
        # branch).
        if resolved.setting_sources is not None:
            options_kwargs["setting_sources"] = resolved.setting_sources
        try:
            reasoning_effort = validate_effort(
                cfg.extra.get("reasoning_effort"), "Claude Agent SDK", CLAUDE_EFFORTS
            )
        except ValueError as exc:
            yield ExecutorError(message=str(exc), retryable=False)
            return
        if reasoning_effort is not None:
            options_kwargs["effort"] = reasoning_effort
        # Databricks opus/fable endpoints reject thinking.type="enabled"
        # (HTTP 400); they require "adaptive" instead (those tiers are
        # adaptive-only). Sonnet is fine, so scope to those tiers only.
        # This is Databricks-specific: it is gated on the ``databricks-
        # claude-opus-`` / ``databricks-claude-fable-`` model prefixes, so
        # it never fires for a generic gateway model.
        if (
            self._gateway
            and isinstance(model, str)
            and model.startswith(DATABRICKS_CLAUDE_ADAPTIVE_THINKING_PREFIXES)
            and "thinking" not in options_kwargs
        ):
            # display="summarized" so Opus 4.7+ / Fable streams thinking text
            # (their default is "omitted").
            options_kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
        options = sdk.ClaudeAgentOptions(**options_kwargs)
        if self._cli_path:
            options.cli_path = self._cli_path
        if model:
            options.model = model
        if self._cwd:
            options.cwd = self._cwd
        # Install the unified can_use_tool gate. It runs the TOOL_CALL
        # policy evaluation in EVERY permission mode — including
        # ``bypassPermissions`` — so connector-native MCP tools
        # (``mcp__github__*``, ``mcp__atlassian__*``) that execute inside
        # the CLI subprocess are evaluated against Omnigent TOOL_CALL
        # policy before they run, instead of bypassing policy entirely.
        #
        # The gate is no-friction when nothing matches: a policy ALLOW /
        # no-match returns allow with no human prompt, so
        # ``bypassPermissions`` ergonomics are preserved for un-gated
        # tools. The human-consent elicitation half of the gate still only
        # fires for non-bypass modes (see ``_can_use_tool_gate``).
        #
        # Install whenever EITHER a policy evaluator OR an elicitation
        # handler is wired. Previously this only installed for non-bypass
        # modes, which is why default ``claude-sdk`` sessions (which
        # default to ``bypassPermissions``) had no per-tool TOOL_CALL gate.
        if (
            getattr(self, "_policy_evaluator", None) is not None
            or self._elicitation_handler is not None
        ):
            options.can_use_tool = self._can_use_tool_gate

        # Log the full configuration for debugging
        logger.info(
            "ClaudeSDKExecutor: model=%s, gateway=%s, base_url=%s, tools=%d, thinking=%r",
            model or "(default)",
            self._gateway,
            env.get("ANTHROPIC_BASE_URL", "(not set)"),
            len(tools),
            options_kwargs.get("thinking"),
        )

        # Run the query, streaming events as they arrive.
        # The SDK manages its own tool-call loop internally (via MCP).
        # We observe the message stream and yield ExecutorEvents so the
        # CLI can render text chunks and tool-call progress in real time.
        response_text = ""
        turn_usage: dict[str, Any] | None = None  # type: ignore[explicit-any]
        # The concrete model the SDK reports on its assistant messages, e.g.
        # ``"claude-opus-4-8"``. Captured from the stream because the resolved
        # config ``model`` is ``None`` when the spec pins none and the gateway
        # picks a default internally — this is then forwarded in ``turn_usage``
        # so the server can price the turn. ``None`` until an assistant message
        # carrying a model arrives.
        observed_model: str | None = None
        system_diagnostics: list[str] = []
        terminal_error: str | None = None
        compaction_occurred: bool = False
        claude_session_id: str | None = None

        # Track in-flight tool calls so we can emit ToolCallComplete
        # with the tool name and duration when results arrive.
        pending_tools: dict[str, tuple[str, float]] = {}  # id → (name, start_mono)

        # Track whether we've received any StreamEvent messages.
        # When True, we skip text/tool events from AssistantMessage to
        # avoid double-emitting (the SDK sends both StreamEvents AND
        # the complete AssistantMessage for the same content).
        got_stream_events = False

        # Per-API-call prompt usage from the most recent ``message_start``
        # stream event. A single user turn can drive MANY internal API
        # calls (the SDK's tool loop), and ``ResultMessage.usage`` reports
        # the CUMULATIVE total across all of them — so summing its cache
        # buckets over-counts context, because each iteration re-sends the
        # whole (growing) conversation and those prompt tokens get tallied
        # once per call. For context-window fill we want only the LAST
        # call's prompt size: that single prompt already contains the full
        # conversation that carries into the next turn. We capture each
        # ``message_start``'s ``message.usage`` here and keep the latest,
        # mirroring how the openai-agents executor uses ``raw_responses[-1]``
        # for ``context_tokens``. ``None`` until the first call starts.
        last_call_usage: dict[str, Any] | None = None  # type: ignore[explicit-any]

        client = await self._get_or_create_client(
            sdk,
            session_key=session_key,
            options=options,
            model=model,
        )

        # ── LLM_REQUEST policy evaluation ────────────────────────
        # If the executor adapter installed a ``_policy_evaluator``
        # callback, call it with the request data so the Omnigent server
        # can evaluate LLM_REQUEST policies before the LLM call.
        _policy_eval = getattr(self, "_policy_evaluator", None)
        if _policy_eval is not None:
            # Extract the user prompt text for PII scanning.
            _last_user_msg = ""
            if isinstance(prompt, str):
                _last_user_msg = prompt[:500]
            elif isinstance(prompt, list):
                _parts = [
                    b.get("text", "")
                    for b in prompt
                    if isinstance(b, dict) and b.get("type") in ("text", "input_text")
                ]
                _last_user_msg = " ".join(_parts)[:500]
            _req_data: dict[str, Any] = {
                "model": model,
                "messages_count": len(prompt) if isinstance(prompt, list) else 1,
                "tools_count": len(tools),
                "system_prompt_preview": (system_prompt[:200] if system_prompt else ""),
                "last_user_message": _last_user_msg,
            }
            _req_verdict = await _policy_eval("PHASE_LLM_REQUEST", _req_data)
            if _req_verdict.action == "POLICY_ACTION_DENY":
                _deny_reason = _req_verdict.reason or "no reason given"
                yield ExecutorError(message=f"LLM call denied by policy: {_deny_reason}")
                return

        try:
            try:
                sdk_prompt: str | AsyncIterator[dict[str, Any]]
                if isinstance(prompt, list):
                    # Multimodal content blocks — send as a
                    # structured message via the SDK's dict path.
                    sdk_prompt = _multimodal_message_iter(prompt, session_id=session_key)
                else:
                    sdk_prompt = prompt
                await asyncio.wait_for(
                    client.query(sdk_prompt, session_id=session_key),
                    timeout=_QUERY_START_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    f"Claude SDK query start timed out after {int(_QUERY_START_TIMEOUT_SECONDS)}s"
                ) from exc
            message_stream = client.receive_response()
            try:
                while True:
                    next_task = asyncio.ensure_future(anext(message_stream))
                    idle_seconds = 0.0
                    try:
                        while True:
                            done, _ = await asyncio.wait(
                                {next_task}, timeout=_STREAM_IDLE_WARN_SECONDS
                            )
                            if next_task in done:
                                break
                            idle_seconds += _STREAM_IDLE_WARN_SECONDS
                            logger.warning(
                                "Claude SDK response stream has been idle for "
                                "%ds (session %s); still waiting.",
                                int(idle_seconds),
                                session_key,
                            )
                    except BaseException:
                        next_task.cancel()
                        with suppress(BaseException):
                            await next_task
                        raise
                    try:
                        message = next_task.result()
                    except StopAsyncIteration:
                        break
                    if isinstance(message, _StreamEvent):
                        got_stream_events = True
                        stream_evt = cast(_StreamEventObj, message)
                        evt = stream_evt.event
                        evt_type = evt.get("type")

                        if evt_type == "message_start":
                            # Each ``message_start`` opens one API call and
                            # carries that call's prompt-side usage
                            # (input + cache buckets). Keep the latest so
                            # ``context_tokens`` reflects the final call's
                            # prompt — the true context size — instead of
                            # ``ResultMessage.usage``'s cumulative sum across
                            # every tool-loop iteration.
                            msg_obj = evt.get("message")
                            if isinstance(msg_obj, dict):
                                call_usage = msg_obj.get("usage")
                                if isinstance(call_usage, dict) and call_usage:
                                    last_call_usage = call_usage
                            continue

                        if evt_type == "content_block_start":
                            block_evt = evt.get("content_block", {})
                            block_type = block_evt.get("type")
                            if block_type == "thinking":
                                # Anchor the ``thinking…`` indicator at the
                                # start of an extended-thinking block.
                                yield ReasoningChunk(
                                    delta="",
                                    event_type="reasoning_started",
                                )
                                continue
                            if block_type == "tool_use":
                                raw_tool_id = block_evt.get("id")
                                # SSE stream events from the SDK should
                                # always carry an ``id`` on tool_use
                                # blocks; skip malformed events rather
                                # than bucketing them under ``""``.
                                if not isinstance(raw_tool_id, str):
                                    continue
                                tool_name = block_evt.get("name", "unknown")
                                # Track the tool for duration / pairing.
                                # The ToolCallRequest itself is emitted
                                # later, when the ``AssistantMessage``
                                # arrives with ``tool_block.input``
                                # populated — at ``content_block_start``
                                # the args have not yet been streamed via
                                # ``input_json_delta`` and the request
                                # would carry ``args={}``, rendering
                                # downstream as ``⏵ Bash()`` with no
                                # visible command. Waiting one event
                                # loses a few ms of "tool call started"
                                # feedback in exchange for correct args.
                                pending_tools[raw_tool_id] = (tool_name, time.monotonic())

                        elif evt_type == "content_block_delta":
                            delta = evt.get("delta", {})
                            delta_type = delta.get("type")
                            if delta_type == "text_delta":
                                text = delta.get("text")
                                if isinstance(text, str) and text:
                                    response_text += text
                                    yield TextChunk(text=text)
                            elif delta_type == "thinking_delta":
                                # Mirror the OpenAI reasoning path so the
                                # ``thinking…`` panel populates live.
                                thinking_text = delta.get("thinking")
                                if isinstance(thinking_text, str) and thinking_text:
                                    yield ReasoningChunk(
                                        delta=thinking_text,
                                        event_type="reasoning_text",
                                    )

                    elif isinstance(message, sdk.AssistantMessage):
                        assistant_msg = cast(_AssistantMessageObj, message)
                        # Capture the concrete model the SDK used (the resolved
                        # config ``model`` is None when the spec pins none).
                        _am_model = getattr(assistant_msg, "model", None)
                        if isinstance(_am_model, str) and _am_model:
                            observed_model = _am_model
                        if got_stream_events:
                            # StreamEvents already emitted text. Emit the
                            # ToolCallRequest here, once the full
                            # ``tool_block.input`` has assembled — see the
                            # note in the ``content_block_start`` branch
                            # above for why this is deferred.
                            for block in assistant_msg.content:
                                if isinstance(block, sdk.ToolUseBlock):
                                    tool_block = cast(_ToolUseBlockObj, block)
                                    if tool_block.id not in pending_tools:
                                        pending_tools[tool_block.id] = (
                                            tool_block.name,
                                            time.monotonic(),
                                        )
                                    yield ToolCallRequest(
                                        name=tool_block.name,
                                        args=tool_block.input,
                                        # Thread the SDK's
                                        # ``tool_use_id`` through
                                        # so downstream consumers
                                        # (notably the new harness
                                        # contract's
                                        # :class:`ExecutorAdapter`)
                                        # can pair this request
                                        # with the matching
                                        # :class:`ToolCallComplete`
                                        # by call_id.
                                        metadata={"call_id": tool_block.id},
                                    )
                        else:
                            # No streaming — emit events from the full message.
                            for block in assistant_msg.content:
                                if isinstance(block, sdk.TextBlock):
                                    text_block = cast(_TextBlockObj, block)
                                    response_text += text_block.text
                                    yield TextChunk(text=text_block.text)
                                elif isinstance(block, sdk.ThinkingBlock):
                                    # Non-streaming counterpart of the
                                    # ``thinking_delta`` path above.
                                    if block.thinking:
                                        yield ReasoningChunk(
                                            delta="",
                                            event_type="reasoning_started",
                                        )
                                        yield ReasoningChunk(
                                            delta=block.thinking,
                                            event_type="reasoning_text",
                                        )
                                elif isinstance(block, sdk.ToolUseBlock):
                                    tool_block = cast(_ToolUseBlockObj, block)
                                    pending_tools[tool_block.id] = (
                                        tool_block.name,
                                        time.monotonic(),
                                    )
                                    yield ToolCallRequest(
                                        name=tool_block.name,
                                        args=tool_block.input,
                                        metadata={"call_id": tool_block.id},
                                    )

                    elif isinstance(message, sdk.UserMessage):
                        # Tool results come back as UserMessages containing
                        # ToolResultBlocks.  Match each back to its request.
                        user_msg = cast(_UserMessageObj, message)
                        content = user_msg.content
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, sdk.ToolResultBlock):
                                    result_block = cast(_ToolResultBlockObj, block)
                                    tool_name, start = pending_tools.pop(
                                        result_block.tool_use_id,
                                        ("unknown", time.monotonic()),
                                    )
                                    duration_ms = (time.monotonic() - start) * 1000
                                    classification = classify_tool_result(
                                        result_block.content,
                                        fallback_to_string=bool(result_block.is_error),
                                    )
                                    status = classification.status
                                    error = classification.error
                                    if result_block.is_error and not error:
                                        status = ToolCallStatus.ERROR
                                        error = "tool error"
                                    yield ToolCallComplete(
                                        name=tool_name,
                                        status=status,
                                        result=result_block.content,
                                        error=error,
                                        duration_ms=duration_ms,
                                        # Same rationale as the
                                        # ToolCallRequest above —
                                        # the call_id is the only
                                        # thing the adapter can
                                        # use to pair this output
                                        # back to its function_call.
                                        metadata={"call_id": result_block.tool_use_id},
                                    )

                    elif isinstance(message, sdk.ResultMessage):
                        result_msg = cast(_ResultMessageObj, message)
                        claude_session_id = getattr(result_msg, "session_id", None)
                        if not response_text and result_msg.result:
                            response_text = result_msg.result
                        raw_usage = getattr(result_msg, "usage", None)
                        if isinstance(raw_usage, dict) and raw_usage:
                            # ``ResultMessage.usage`` is CUMULATIVE across every
                            # API call in the turn. Keep it for billing
                            # (``input``/``output``/``total`` and the cache
                            # buckets) — that's the correct sum to price.
                            in_tok = raw_usage.get("input_tokens") or 0
                            out_tok = raw_usage.get("output_tokens") or 0
                            # ``context_tokens`` is window FILL, not a billing
                            # sum: it must be a single prompt's size, not the
                            # cumulative input across iterations (which re-sends
                            # the conversation each tool-loop step and would
                            # over-count K-fold on a K-call turn). Use the LAST
                            # ``message_start`` call's prompt — input +
                            # cache_creation + cache_read — which already holds
                            # the full conversation carried into the next turn.
                            # Mirrors openai-agents' ``raw_responses[-1]`` choice.
                            # Fall back to the cumulative sum only when no
                            # ``message_start`` was observed (e.g. non-streaming
                            # paths), preserving a non-null value over None.
                            ctx_src = last_call_usage if last_call_usage is not None else raw_usage
                            ctx_in = ctx_src.get("input_tokens") or 0
                            ctx_cc = ctx_src.get("cache_creation_input_tokens") or 0
                            ctx_cr = ctx_src.get("cache_read_input_tokens") or 0
                            turn_usage = {
                                "input_tokens": in_tok,
                                "output_tokens": out_tok,
                                "total_tokens": in_tok + out_tok,
                                "context_tokens": ctx_in + ctx_cc + ctx_cr,
                                **{
                                    k: v
                                    for k, v in raw_usage.items()
                                    if k
                                    not in (
                                        "input_tokens",
                                        "output_tokens",
                                        "context_tokens",
                                    )
                                },
                                # Harness-reported model for cost pricing: prefer the SDK's
                                # observed_model (config model is None when no model is pinned).
                                "model": observed_model or model,
                            }

                    elif isinstance(message, sdk.SystemMessage):
                        system_msg = cast(_SystemMessageObj, message)
                        subtype = system_msg.subtype
                        data = system_msg.data

                        if subtype == "api_retry":
                            error_status = data.get("error_status")
                            retry_error = data.get("error", "unknown_error")
                            attempt = data.get("attempt")
                            max_retries = data.get("max_retries")
                            retry_delay_ms = data.get("retry_delay_ms")
                            diagnostic = (
                                "Claude CLI API retry"
                                f" {attempt}/{max_retries}: {retry_error}"
                                f" (status={error_status}, retry_delay_ms={retry_delay_ms})"
                            )
                            system_diagnostics.append(diagnostic)
                            logger.warning(diagnostic)

                            if (
                                error_status in {401, 403}
                                or retry_error == "authentication_failed"
                            ):
                                terminal_error = (
                                    "Claude SDK provider authentication failed"
                                    f" ({retry_error}, status={error_status}). "
                                    "Check your selected ~/.databrickscfg profile."
                                )
                                break

                            if error_status == 404:
                                terminal_error = (
                                    "Claude SDK provider endpoint was not found "
                                    f"({retry_error}, status={error_status}). "
                                    "Check ANTHROPIC_BASE_URL / Databricks endpoint configuration."
                                )
                                break
                        elif getattr(system_msg, "hook_event_name", None) == "PreCompact":
                            compaction_occurred = True
                            logger.info("Claude SDK compaction detected (PreCompact hook)")
                        else:
                            logger.info("Claude CLI system message: %s", data)
            finally:
                # ``receive_response`` returns an async generator, which
                # always has ``aclose``; guard anyway for duck-typed test
                # doubles that implement the iterator protocol without it.
                aclose = getattr(message_stream, _ACLOSE_ATTR, None)
                if aclose is not None:
                    await aclose()

        except Exception as exc:  # noqa: BLE001 — top-level executor error boundary; records crash and surfaces to caller
            self._crashed_sessions[session_key] = str(exc)
            await self._close_live_client(session_key)
            stderr_text = "\n".join(stderr_lines) if stderr_lines else "(no stderr captured)"
            diagnostics_text = (
                "\n".join(system_diagnostics)
                if system_diagnostics
                else "(no system diagnostics captured)"
            )
            logger.error(
                "ClaudeSDKExecutor error: %s\nCLI stderr:\n%s\nCLI system diagnostics:\n%s",
                exc,
                stderr_text,
                diagnostics_text,
            )
            yield ExecutorError(
                message=(
                    f"Claude SDK error: {exc}\n"
                    f"CLI stderr:\n{stderr_text}\n"
                    f"CLI system diagnostics:\n{diagnostics_text}"
                )
            )
            return
        if terminal_error:
            yield ExecutorError(message=terminal_error)
            return

        # ── LLM_RESPONSE policy evaluation ───────────────────────
        # Evaluate after the stream completes but before TurnComplete
        # so a DENY prevents the response from being persisted.
        if _policy_eval is not None:
            _resp_data: dict[str, Any] = {
                "model": model,
                "text_preview": (response_text[:500] if response_text else ""),
                "tool_calls_count": len(pending_tools),
            }
            if turn_usage is not None:
                _resp_data["usage"] = turn_usage
            _resp_verdict = await _policy_eval("PHASE_LLM_RESPONSE", _resp_data)
            if _resp_verdict.action == "POLICY_ACTION_DENY":
                _deny_reason = _resp_verdict.reason or "no reason given"
                yield ExecutorError(message=(f"LLM response denied by policy: {_deny_reason}"))
                return

        _notify_usage_from_dict(model=model, usage=turn_usage)

        if compaction_occurred and claude_session_id:
            from omnigent.inner.executor import CompactionComplete

            _compaction_tokens = 0
            if turn_usage is not None:
                _compaction_tokens = turn_usage.get("context_tokens", 0) or 0
            # Read the post-compaction session messages so the runner
            # can persist them for session resume in ephemeral
            # environments where the CLI's own transcript is lost.
            _compacted: list[dict[str, Any]] | None = None
            try:
                from claude_agent_sdk import get_session_messages

                _msgs = get_session_messages(claude_session_id, directory=self._cwd)
                _compacted = [
                    {"type": "message", "role": m.type, "content": m.message.get("content", [])}
                    for m in _msgs
                    if isinstance(m.message, dict)
                ]
                if not _compacted:
                    logger.warning(
                        "Claude post-compaction read returned no messages "
                        "(session=%s); resume will fall back to the synthetic "
                        "summary instead of the harness's real compacted state.",
                        claude_session_id,
                    )
            except Exception:  # noqa: BLE001
                # WARNING, not DEBUG: a swallowed read here silently degrades
                # EVERY later resume of this conversation. The runner persists a
                # compaction item with no ``compacted_messages``, so resume
                # replays the lossy synthetic-summary pair instead of the
                # harness's real post-compaction context. Surface it.
                logger.warning(
                    "Failed to read Claude post-compaction session messages "
                    "(session=%s); resume fidelity for this conversation will "
                    "degrade to the synthetic summary.",
                    claude_session_id,
                    exc_info=True,
                )
            yield CompactionComplete(
                summary="[Claude Code compaction — context was automatically compacted]",
                token_count=_compaction_tokens,
                model=observed_model or model,
                compacted_messages=_compacted,
            )

        yield TurnComplete(response=response_text, usage=turn_usage)

    @staticmethod
    def _build_prompt(
        messages: list[Message],
        *,
        resume_session: bool,
    ) -> str | list[dict[str, Any]]:
        """
        Build the prompt for the SDK.

        For continued Claude SDK sessions, send only the latest user
        message. For a fresh session that already has replayable
        history (for example a sub-agent with
        ``pass_history=True``), serialize that history into the
        first prompt so Claude sees the prior conversation context.

        When the latest user message contains multimodal content
        (images, files), returns a list of Anthropic API content
        blocks instead of a plain string so the caller can send
        them through the SDK's structured message path.

        :param messages: Conversation history as inner
            :class:`Message` dicts.
        :param resume_session: ``True`` when the SDK session
            already has prior turns cached (no need to replay
            history).
        :returns: A plain string prompt, or a list of Anthropic
            API content block dicts when multimodal blocks are
            present in the latest user message.
        """
        if resume_session:
            return ClaudeSDKExecutor._extract_latest_user_content(messages)

        user_messages = [msg for msg in messages if msg.get("role") == "user"]
        if len(messages) <= 1 or len(user_messages) <= 1:
            return ClaudeSDKExecutor._extract_latest_user_content(messages)

        # Check if the latest user message is multimodal — if so,
        # serialize prior history as a text prefix but preserve the
        # latest message's content blocks for native multimodal
        # delivery to the Anthropic API.
        latest_content = ClaudeSDKExecutor._extract_latest_user_content(messages)
        prior = messages[:-1] if messages else []

        lines = ["Conversation so far:"]
        for msg in prior:
            role = str(msg.get("role", "user")).replace("_", " ")
            raw_content = msg.get("content")
            if raw_content is None:
                content = ""
            elif isinstance(raw_content, str):
                content = raw_content
            else:
                content = json.dumps(raw_content, ensure_ascii=True)
            lines.append(f"{role}: {content}")
        lines.append("")
        lines.append(
            "Respond to the latest user message, using the conversation above as context."
        )
        history_prefix = "\n".join(lines)

        if isinstance(latest_content, list):
            return [
                {"type": "text", "text": history_prefix},
                *latest_content,
            ]
        return f"{history_prefix}\n\nuser: {latest_content}"

    @staticmethod
    def _extract_latest_user_content(
        messages: list[Message],
    ) -> str | list[dict[str, Any]]:
        """
        Extract the latest user message content for the SDK.

        Returns a plain string for text-only messages. When the
        message carries multimodal content blocks (``input_image``,
        ``input_file``), converts them to Anthropic API content
        block format and returns the list so the caller can send
        a structured message through the SDK transport.

        :param messages: Conversation history.
        :returns: A string prompt, or a list of Anthropic content
            block dicts.
        """
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content")
                if content is None:
                    return ""
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    return _to_anthropic_content_blocks(content)
                return str(content)
        return ""
