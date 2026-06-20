"""OpenAIAgentsSDKExecutor: run agents using the OpenAI Agents SDK.

This executor uses the OpenAI Agents SDK as the agent runtime while keeping
Omnigent as the system of record for tools, policies, and session history.
Omnigent tools are exposed as SDK FunctionTools whose callbacks route back
through the Session's tool registry.

Conversation state is delegated to the SDK's session layer. Each Omnigent
Session gets its own SDK SQLiteSession keyed by the Omnigent session id.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Generator
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Literal, Protocol, TypeAlias, cast

import httpx

from omnigent.llms._usage_observer import notify_from_dict as _notify_usage_from_dict
from omnigent.llms.errors import is_context_length_exceeded as _is_context_length_exceeded
from omnigent.reasoning_effort import OPENAI_AGENTS_EFFORTS, validate_effort
from omnigent.spec.types import RetryPolicy

from .executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolSpec,
    TurnComplete,
    classify_tool_result,
    split_transient_tail,
)
from .open_responses_sdk import (
    _OPENAI_KEY_PLACEHOLDER,
    _convert_messages_to_responses,
    _databricks_openai_base_url,
)

logger = logging.getLogger(__name__)

_OPENAI_AGENTS_DEFAULT_MODEL = "gpt-5.3-codex"
_DATABRICKS_OPENAI_AGENTS_DEFAULT_MODEL = "databricks-gpt-5-5"

# Total run attempts per turn (1 initial + retries). The Databricks
# gateway occasionally returns a completed-but-empty turn (status
# completed, no text / no tool calls / no output items); a single
# retry recovers it. Kept small and hardcoded: more retries would
# risk wedging long e2e turns under the shard timeout, and the
# transient is rare.
_EMPTY_TURN_MAX_ATTEMPTS = 2

# SDK ``RunItem.type`` values that are bookkeeping, not user-visible
# output. A turn whose only new items are these (and which produced no
# text and no tool activity) is treated as empty and retried. Excluding
# by name (rather than allow-listing output types) is the conservative
# direction: an unknown future item type counts as output and is NOT
# retried.
_NON_OUTPUT_ITEM_TYPES: frozenset[str] = frozenset({"reasoning_item", "compaction_item"})

# Replay items persisted to the SDK Session — heterogeneous Responses-API
# input items (function_call / function_call_output / message / etc.).
# The SDK declares this as ``TResponseInputItem``, which is a TypedDict
# union; we only care about two fields (``id``, ``call_id``) and pass the
# rest through opaquely.
ReplayItem: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# Raw pydantic tool-call / tool-output items from the Responses API. Their
# concrete union is huge (``ToolCallItemTypes`` / ``ToolCallOutputTypes``)
# and the fields we touch depend on the runtime member, so we keep it
# opaque at the boundary and narrow with ``isinstance(..., dict)`` + a
# Protocol cast at each read site.
RawToolItem: TypeAlias = Any  # type: ignore[explicit-any]

# The openai ``AsyncOpenAI`` client. Kept as ``Any`` so ``openai`` stays
# an optional import at type-check time — the executor only constructs
# one when instantiated.
AsyncOpenAIClient: TypeAlias = Any  # type: ignore[explicit-any]

# Tool executor callable wired in by ``omnigent.Session``. The result
# is JSON-ish (dict[str, Any]) but the static type leaks ``Any`` through
# the kwargs dict. Constrained here to match omnigent.executor.
ToolExecutor: TypeAlias = Callable[  # type: ignore[explicit-any]
    [str, dict[str, Any]], Awaitable[dict[str, Any]]
]

# SDK objects we treat as opaque: agent/tool instances and tool results
# are structurally duck-typed by callers and the Protocols above.
SDKAgent: TypeAlias = Any  # type: ignore[explicit-any]
SDKTool: TypeAlias = Any  # type: ignore[explicit-any]
ToolResult: TypeAlias = Any  # type: ignore[explicit-any]

# Parsed tool arguments / tool-call-output dict. JSON-shaped bag handed
# to the Omnigent tool executor.
ToolArgs: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]


def _normalize_responses_items_for_chat(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply :func:`_normalize_content_blocks_for_chat` to every message item.

    Walks the full Responses-API item list produced by
    :func:`~omnigent.inner.open_responses_sdk._convert_messages_to_responses`
    and normalises ``input_file`` blocks in any ``message`` item's ``content``
    list.  Non-message items (function_call, function_call_output, …) pass
    through unchanged.

    :param items: Responses-API input item dicts.
    :returns: New list with normalised ``input_file`` blocks in message
        content.  Items without ``input_file`` blocks are returned as-is.
    """
    result: list[dict[str, Any]] = []
    for item in items:
        if item.get("type") == "message":
            raw_content = item.get("content")
            if isinstance(raw_content, list):
                normalized_content = _normalize_content_blocks_for_chat(raw_content)
                if normalized_content is not raw_content:
                    item = {**item, "content": normalized_content}
        result.append(item)
    return result


def _normalize_content_blocks_for_chat(
    content: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize content blocks before handing them to the openai-agents Runner.

    Converts ``input_file`` blocks whose ``file_data`` is a ``data:`` URI into
    ``input_text`` blocks by decoding the base64 payload.  Also strips
    Omnigent-only metadata (for example ``filename``) from known Responses
    content block types before the OpenAI Agents SDK serializes them to the
    provider API.

    Background: :mod:`omnigent.runtime.content_resolver` resolves uploaded
    file IDs to inline ``data:<mime>;base64,<data>`` URIs stored in
    ``file_data`` / ``image_url`` while preserving UI/history metadata such as
    ``filename``.  That metadata is useful in Omnigent, but the OpenAI API
    rejects unknown content-part fields (for example
    ``input[0].content[0].filename``).  The openai-agents SDK's
    ``chatcmpl_converter`` also passes raw ``file_data`` strings to the Chat
    Completions ``file`` content type, which expects **plain base64** — not a
    data URI.  Additionally, the Databricks GPT endpoint may not support the
    ``file`` content block type at all, so textual files are converted to
    ``input_text``.  Images are preserved as ``input_image`` with a
    conventional ``data:image/<type>;base64,...`` URL so vision-capable
    Chat Completions providers can see the pixels.

    :param content: List of Responses-API content-block dicts, possibly
        containing ``input_file`` blocks with ``file_data`` data URIs or
        metadata-bearing ``input_image`` blocks.
    :returns: New list with each ``input_file`` block replaced by an
        ``input_text`` block carrying the decoded text content, and known
        content blocks stripped to provider-supported keys.  Binary
        (non-``text/*``) file blocks, undecodable, or empty file blocks are
        silently dropped.  Unknown block types pass through unchanged.
    """
    result: list[dict[str, Any]] = []
    changed = False
    for block in content:
        block_type = block.get("type")
        if block_type == "input_file":
            changed = True
            file_data: str = block.get("file_data", "")
            if file_data.startswith("data:"):
                try:
                    meta, b64 = file_data.split(",", 1)
                    mime = meta.split(";")[0].replace("data:", "")
                    if not mime.startswith("text/"):
                        continue  # binary files (PDF, etc.) can't be inlined as text
                    text = base64.b64decode(b64).decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    text = ""
            else:
                text = file_data
            if text:
                result.append({"type": "input_text", "text": text})
            # else: drop empty / undecodable blocks
        elif block_type in {"input_text", "output_text", "text"}:
            sanitized = _copy_known_keys(block, ("type", "text"))
            changed = changed or sanitized != block
            result.append(sanitized)
        elif block_type == "input_image":
            # Preserve inline images; strip Omnigent-only metadata.
            sanitized = _copy_known_keys(block, ("type", "image_url", "file_id", "detail"))
            changed = changed or sanitized != block
            result.append(sanitized)
        else:
            result.append(block)
    # Return original list when nothing changed so callers can use
    # identity check (``is``) to avoid unnecessary copies.
    return result if changed else content


def _copy_known_keys(block: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """Return *block* with only keys accepted by provider content schemas.

    Omnigent content blocks can carry UI/history metadata such as
    ``filename``.  Known OpenAI/Responses block types are strict at the API
    boundary, so those metadata fields must be removed from the payload handed
    to the OpenAI Agents SDK while leaving persisted conversation state intact.
    """
    return {key: block[key] for key in keys if key in block}


class _SDKSession(Protocol):
    """Structural view of ``agents.memory.Session`` — the four async
    methods + two attributes the SDK's Session protocol requires.

    Using a local Protocol lets ``_SanitizingSession`` satisfy the SDK
    contract without inheriting from it, and lets tests plug in fake
    sessions without importing the SDK.
    """

    session_id: str
    session_settings: Any  # type: ignore[explicit-any]  # SDK's SessionSettings | None; opaque here

    async def get_items(self, limit: int | None = None) -> list[ReplayItem]: ...
    async def add_items(self, items: list[ReplayItem]) -> None: ...
    async def pop_item(self) -> ReplayItem | None: ...
    async def clear_session(self) -> None: ...


class _AgentsSDK(Protocol):
    """Structural view of the ``agents`` module.

    Tests swap in a ``SimpleNamespace`` with the same attribute names, so
    we can't rely on ``ModuleType`` with real-type attributes. Attributes
    are typed as ``type`` factories that return the actual SDK objects
    the caller touches — kept deliberately loose (``Any`` factories in a
    few places) so the duck-typed test doubles also satisfy the
    Protocol structurally.
    """

    # Factories invoked with **kwargs; the results are used via Protocols
    # (_RunResult, _RunState, _StreamEvent) or via attribute access that
    # works on both the real dataclasses and the test fakes.
    Agent: Any  # type: ignore[explicit-any]  # agents.Agent[Any] factory
    Runner: Any  # type: ignore[explicit-any]  # agents.Runner class with run_streamed classmethod
    RunConfig: Any  # type: ignore[explicit-any]  # agents.RunConfig dataclass factory
    ModelSettings: Any  # type: ignore[explicit-any]  # agents.ModelSettings dataclass factory
    OpenAIProvider: Any  # type: ignore[explicit-any]  # agents.OpenAIProvider class
    FunctionTool: Any  # type: ignore[explicit-any]  # agents.FunctionTool dataclass factory
    SQLiteSession: Any  # type: ignore[explicit-any]  # agents.SQLiteSession class
    ItemHelpers: Any  # type: ignore[explicit-any]  # agents.ItemHelpers utility class
    MaxTurnsExceeded: type[BaseException]


class _RunResult(Protocol):
    """Structural view of ``agents.result.RunResultStreaming``.

    Mirrors the subset this executor actually consumes.
    """

    final_output: Any  # type: ignore[explicit-any]  # SDK declares this as Any; may be str or arbitrary pydantic output
    new_items: list[Any]  # type: ignore[explicit-any]  # list[RunItem] union the SDK narrows internally
    raw_responses: list[Any]  # type: ignore[explicit-any]  # list[ModelResponse]; each has .usage.input_tokens

    def cancel(self, mode: str = ...) -> None: ...

    def stream_events(self) -> AsyncIterator[_StreamEvent]: ...

    def to_state(self) -> _RunState: ...


class _RunState(Protocol):
    """Structural view of ``agents.run_state.RunState`` — only the three
    private counters this executor tweaks when resuming a stepwise turn.
    """

    _current_turn: int
    _max_turns: int
    _current_turn_persisted_item_count: int


class _StreamEvent(Protocol):
    """Minimal shape shared by the SDK's stream events: each one has a
    ``type`` discriminator. We narrow on it before reading ``data`` /
    ``item`` via ``cast``.
    """

    type: str


class _RawResponseEvent(Protocol):
    type: str
    data: _RawResponseData


class _RawResponseData(Protocol):
    type: str
    delta: str


class _RunItemEvent(Protocol):
    type: str
    item: _RunItem


class _RunItem(Protocol):
    type: str
    raw_item: Any  # type: ignore[explicit-any]  # ToolCallItemTypes | ToolCallOutputTypes union, narrowed on .type
    output: Any  # type: ignore[explicit-any]  # ToolCallOutputItem.output is declared Any by the SDK


class _ToolCallRawItem(Protocol):
    """Duck-typed view of non-dict tool-call raw items (pydantic models
    like ``ResponseFunctionToolCall``). Attribute access returns the
    corresponding field — ``None`` on items where it isn't set.
    """

    name: str | None
    arguments: str | None
    call_id: str | None
    id: str | None


class _ToolCallOutputRawItem(Protocol):
    call_id: str | None
    id: str | None


class _CallModelData(Protocol):
    model_data: _ModelInputData


class _ModelInputData(Protocol):
    input: list[ReplayItem]


def _ensure_agents_sdk() -> ModuleType:
    try:
        import agents

        return agents
    except ImportError as exc:
        raise ImportError(
            "OpenAIAgentsSDKExecutor requires the 'openai-agents' package. "
            "Install it with: pip install openai-agents"
        ) from exc


def _get_openai_async_client(
    profile: str | None = None,
    api_key: str | None = None,
    retry_policy: RetryPolicy | None = None,
    base_url_override: str | None = None,
    host_override: str | None = None,
    databricks_auth_command: str | None = None,
    model: str | None = None,
) -> AsyncOpenAIClient:
    """Construct an AsyncOpenAI client for direct or Databricks-hosted use.

    For Databricks-backed profiles, the returned client uses an httpx
    ``Auth`` callback that calls ``Config.authenticate()`` on every
    HTTP request, so OAuth tokens are refreshed transparently and
    long-running sessions survive the 1-hour access-token lifetime.

    :param profile: Optional ``~/.databrickscfg`` profile name for the
        Databricks path, e.g. ``"dev"``.
    :param api_key: Direct OpenAI-compatible API key, e.g.
        ``"sk-proj-..."``. When set, constructs
        ``AsyncOpenAI(api_key=…)`` directly, bypassing all profile and
        env-var lookups. Intended for specs that declare
        ``executor.auth: {type: api_key, api_key: …}``.
    :param retry_policy: Optional retry policy. When provided, its
        ``policy.openai.kwargs()`` (max_retries, timeout) are spread
        into the ``AsyncOpenAI(...)`` constructor so L0 retry uses
        the spec's budget. ``None`` falls back to a default policy
        with the project-wide budget.
    :param base_url_override: When set, use this as the client base URL.
        In gateway host mode this is required and is populated from
        ``HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL``.
    :param host_override: Databricks workspace host, e.g.
        ``"https://example.databricks.com"``. When set, skips profile host
        lookup and requires ucode-provided gateway URL and auth command
        values.
    :param databricks_auth_command: Shell command from ucode state that
        prints a bearer token, e.g.
        ``"databricks auth token --host https://example.databricks.com ..."``.
    :param model: The model name that will be used. When set to a
        non-Databricks model (i.e. does not start with ``"databricks-"``),
        both the explicit ``profile`` block and the ambient Databricks
        credential fallback are skipped — the caller must supply
        ``OPENAI_API_KEY`` (and optionally ``OPENAI_BASE_URL``) instead.
    :raises DatabricksAuthError: When an explicit ``profile`` is given,
        authentication fails, and no ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY``
        env-var fallbacks are available.
    :raises OSError: If ucode host state is present but missing the
        corresponding base URL or auth command.
    """
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise ImportError(
            "The 'openai' package is required for OpenAIAgentsSDKExecutor. "
            "Install it with: pip install openai"
        ) from exc

    policy = retry_policy if retry_policy is not None else RetryPolicy()
    retry_kwargs = policy.openai.kwargs()

    if host_override:
        if base_url_override is None:
            raise OSError(
                "OpenAIAgentsSDKExecutor with a gateway workspace host requires "
                "HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL."
            )
        if databricks_auth_command is None:
            raise OSError(
                "OpenAIAgentsSDKExecutor with a gateway workspace host requires "
                "HARNESS_OPENAI_AGENTS_GATEWAY_AUTH_COMMAND."
            )
        host = host_override.rstrip("/")
        return AsyncOpenAI(
            base_url=base_url_override,
            api_key=_OPENAI_KEY_PLACEHOLDER,
            http_client=httpx.AsyncClient(auth=_ShellCommandBearerAuth(databricks_auth_command)),
            **retry_kwargs,
        )

    # Explicit spec-level api_key (executor.auth: {type: api_key, …}).
    # Checked before profile and env-var lookups so the spec is self-contained.
    # base_url_override is populated from HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL
    # when the spec also declares executor.auth.base_url.
    #
    # Fall back to the ambient OPENAI_BASE_URL when no override reached us.
    # The api_key is frequently a gateway credential (e.g. a Databricks AI
    # Gateway PAT detected from OPENAI_API_KEY), and the companion base_url
    # can be dropped anywhere on the daemon → runner → harness propagation
    # chain (the spec auth bake omits it when OPENAI_BASE_URL is absent at
    # materialization time; a reused local daemon may predate the env var).
    # Without this fallback, a missing base_url silently routes the gateway
    # token to api.openai.com and every request 401s; honoring the ambient
    # OPENAI_BASE_URL the runner inherits keeps the gateway target present on
    # every turn even when the override is lost. base_url=None still defaults
    # to api.openai.com for a genuine OpenAI key with no gateway configured.
    if api_key and api_key.strip():
        return AsyncOpenAI(
            api_key=api_key,
            base_url=base_url_override or os.environ.get("OPENAI_BASE_URL") or None,
            **retry_kwargs,
        )

    is_databricks_model = model is None or model.startswith("databricks-")

    # Databricks profile auth only applies when the model is Databricks-hosted.
    # An explicit spec/provider profile wins over ambient env vars.
    if is_databricks_model and profile:
        from .databricks_executor import DatabricksAuthError, _resolve_databricks_auth

        try:
            auth, host = _resolve_databricks_auth(profile)
            return AsyncOpenAI(
                base_url=base_url_override or _databricks_openai_base_url(host),
                api_key=_OPENAI_KEY_PLACEHOLDER,
                http_client=httpx.AsyncClient(auth=auth),
                **retry_kwargs,
            )
        except DatabricksAuthError as exc:
            # Fall through to env-var credentials when available so that
            # CI environments (OIDC tokens injected via OPENAI_BASE_URL /
            # OPENAI_API_KEY) still work even if the named profile is not
            # present in ~/.databrickscfg.  Log a warning so the fallback
            # is visible rather than silent.  Re-raise only when there are
            # no env-var fallbacks to fall through to.
            if os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_KEY"):
                logger.warning(
                    "Databricks profile %r authentication failed (%s); "
                    "falling back to OPENAI_BASE_URL/OPENAI_API_KEY.",
                    profile,
                    exc,
                )
            else:
                raise
        except ImportError:
            logger.warning(
                "databricks-sdk is not installed; cannot resolve Databricks "
                "profile %r. Falling back to OPENAI_BASE_URL/OPENAI_API_KEY.",
                profile,
            )

    if os.environ.get("OPENAI_BASE_URL"):
        return AsyncOpenAI(
            base_url=os.environ["OPENAI_BASE_URL"],
            api_key=os.environ.get("OPENAI_API_KEY", _OPENAI_KEY_PLACEHOLDER),
            **retry_kwargs,
        )

    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        return AsyncOpenAI(api_key=api_key, **retry_kwargs)

    # Non-Databricks model with no OpenAI credentials — fail loudly rather
    # than silently routing to the Databricks AI Gateway, which will 404.
    if not is_databricks_model:
        raise ValueError(
            f"Model {model!r} is not a Databricks-hosted model but no OpenAI "
            "credentials were found. Set OPENAI_API_KEY (and optionally "
            "OPENAI_BASE_URL) to use this model, or use a 'databricks-' "
            "prefixed model name to route through Databricks."
        )

    # No profile, no env — final fallback via ambient Databricks credentials.
    try:
        from .databricks_executor import _resolve_databricks_auth

        auth, host = _resolve_databricks_auth(profile)
    except ImportError as exc:
        raise ImportError(
            "The 'databricks-sdk' package is required for Databricks "
            "authentication but is not installed, and no OPENAI_API_KEY or "
            "OPENAI_BASE_URL environment variables are set. Either install "
            "the package (`pip install 'omnigent[databricks]'`) or set "
            "OPENAI_API_KEY/OPENAI_BASE_URL for non-Databricks OpenAI access."
        ) from exc
    return AsyncOpenAI(
        base_url=base_url_override or _databricks_openai_base_url(host),
        api_key=_OPENAI_KEY_PLACEHOLDER,
        http_client=httpx.AsyncClient(auth=auth),
        **retry_kwargs,
    )


class _ShellCommandBearerAuth(httpx.Auth):
    """httpx Auth that refreshes a bearer token through a shell command.

    :param command: Shell command that prints the access token to stdout,
        e.g. ``"databricks auth token --host https://example.databricks.com ..."``.
    """

    def __init__(self, command: str) -> None:
        """Store the command for per-request token refresh.

        :param command: Shell command that prints the access token.
        """
        self._command = command

    def auth_flow(
        self,
        request: httpx.Request,
    ) -> Generator[httpx.Request, httpx.Response, None]:
        """Inject a fresh ``Authorization: Bearer`` header.

        :param request: The outgoing httpx request.
        :yields: The request with the auth header set.
        :raises RuntimeError: When the command fails or prints no token.
        """
        result = subprocess.run(
            ["sh", "-c", self._command],
            check=False,
            capture_output=True,
            text=True,
        )
        token = result.stdout.strip()
        if result.returncode != 0 or not token:
            raise RuntimeError("Databricks auth command failed to return a bearer token.")
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


# Sentinel pushed onto the per-turn event queue when
# :meth:`OpenAIAgentsSDKExecutor._consume_stream_into_queue`
# exits (normally OR via cancellation). The consumer loop in
# :meth:`OpenAIAgentsSDKExecutor.run_turn` breaks on it and
# proceeds to await the producer task, which surfaces any
# non-cancel exception. A unique module-level object avoids
# false positives from real ``StreamEvent`` instances.
_STREAM_DONE = object()


@dataclass
class _AgentsSessionState:
    """
    Per-session state for the OpenAI Agents SDK executor.

    :param sdk_session: The SDK's persistent session (SQLite-
        backed). Holds the conversational history items.
    :param started: ``True`` once the SDK has accepted the
        first turn for this session.
    :param agent: Cached ``agents.Agent[Any]`` instance for
        reuse across turns; rebuilt when *agent_signature*
        changes.
    :param agent_signature: ``(model, system_prompt, tools,
        parallel_tool_calls)``-derived key. A signature change
        forces an agent rebuild.
    :param resume_state: Optional SDK ``_RunState`` carried
        across stepwise-internal turns.
    :param active_result: The in-flight ``RunResultStreaming``
        for the current turn, or ``None`` between turns.
    :param interrupt_requested: Set by :meth:`interrupt_session`
        to signal the in-flight turn to stop emitting events.
        Cleared at the top of the next ``run_turn``.
    :param stream_consumer_task: The asyncio.Task running
        ``_consume_stream_into_queue`` for the in-flight turn.
        :meth:`interrupt_session` cancels this task, which lands
        ``CancelledError`` inside the SDK's
        ``await self._event_queue.get()`` (agents/result.py:721)
        — the SDK's own ``except CancelledError`` then calls
        ``self.cancel()`` to close the underlying httpx stream.
        Without this task indirection, the only cancel signal
        is ``result.cancel(mode="immediate")``, which sets a
        flag but does NOT close the network connection — events
        keep streaming for 15+ seconds. ``None`` between turns.
    :param history_cursor: Index into the persisted-message
        suffix marking how much we've already replayed.
    :param run_item_count_before: ``len(sdk_session.items)``
        snapshot taken before the in-flight turn so a cancelled
        turn can be rolled back to this point.
    :param rollback_to_item_count: When non-``None``, the next
        ``run_turn`` rewinds the SDK session to this length
        before starting. Set by :meth:`interrupt_session`.
    """

    sdk_session: _SanitizingSession
    started: bool = False
    # agents.Agent[Any] instance; cached for reuse across turns.
    agent: SDKAgent = None
    agent_signature: tuple[str, str, str, str, str] | None = None
    resume_state: _RunState | None = None
    active_result: _RunResult | None = None
    interrupt_requested: bool = False
    stream_consumer_task: asyncio.Task[None] | None = None
    history_cursor: int = 0
    run_item_count_before: int | None = None
    rollback_to_item_count: int | None = None


# ``_sanitize_replay_item`` walks replay values recursively. At the
# boundary these are either SDK TypedDicts, pydantic models that have
# already been ``model_dump``-ed to dicts, or primitive JSON values — so
# ``Any`` is the right annotation. The recursion preserves shape, so we
# return the same static type we received.
_ReplayValue: TypeAlias = Any  # type: ignore[explicit-any]


def _sanitize_replay_item(value: _ReplayValue) -> _ReplayValue:
    """Strip provider-only fields Databricks rejects during replay."""
    if isinstance(value, list):
        return [_sanitize_replay_item(item) for item in value]
    if not isinstance(value, dict):
        return value

    return {
        key: _sanitize_replay_item(item)
        for key, item in value.items()
        if not (key == "id" and isinstance(item, str) and len(item) > 64)
    }


class _SanitizingSession:
    """Session wrapper that normalizes replay items before persistence."""

    def __init__(self, underlying: _SDKSession) -> None:
        self._underlying = underlying
        self.session_id = underlying.session_id
        # ``session_settings`` is declared on the SDK Session protocol but
        # some fake sessions (tests) / older SDK revs omit it. Fall back
        # to ``None`` rather than propagating the AttributeError.
        self.session_settings = (
            underlying.session_settings if hasattr(underlying, "session_settings") else None
        )

    async def get_items(self, limit: int | None = None) -> list[ReplayItem]:
        return await self._underlying.get_items(limit)

    async def add_items(self, items: list[ReplayItem]) -> None:
        await self._underlying.add_items([_sanitize_replay_item(item) for item in items])

    async def pop_item(self) -> ReplayItem | None:
        item = await self._underlying.pop_item()
        if item is None:
            return None
        return cast(ReplayItem, _sanitize_replay_item(item))

    async def clear_session(self) -> None:
        await self._underlying.clear_session()


@dataclass(frozen=True)
class RawToolItemParts:
    """Normalized view of a tool-call raw item from the Agents SDK.

    The SDK hands tool calls back in one of two shapes — a dict (e.g. the
    Responses-API input-item TypedDict) or a pydantic model such as
    ``ResponseFunctionToolCall`` — and this dataclass is the single
    extracted form we pass downstream as a ``ToolCallRequest``.

    :param name: The tool name.  Empty string when the raw item didn't
        carry a name.
    :param args: Parsed JSON arguments.  Falls back to
        ``{"raw": <args_text>}`` when the argument string isn't valid JSON.
    :param call_id: The SDK-assigned call id (``call_id`` or ``id``), or
        ``None`` when the raw item didn't carry one.
    """

    name: str
    args: ToolArgs
    call_id: str | None


def _tool_args_from_raw_item(raw_item: RawToolItem) -> RawToolItemParts:
    """Extract ``name`` / ``args`` / ``call_id`` from an SDK tool raw item.

    :param raw_item: Either a dict (Responses-API TypedDict input item) or
        a pydantic ``_ToolCallRawItem`` produced by the Agents SDK.
    """
    name: str | None = None
    args_text: str | None = None
    call_id: str | None = None
    if isinstance(raw_item, dict):
        raw_dict: ToolArgs = raw_item
        name_val = raw_dict.get("name")
        name = name_val if isinstance(name_val, str) else None
        args_val = raw_dict.get("arguments")
        args_text = args_val if isinstance(args_val, str) else None
        cid = raw_dict.get("call_id") or raw_dict.get("id")
        call_id = cid if isinstance(cid, str) else None
    else:
        # Pydantic BaseModel-style raw item (e.g. ResponseFunctionToolCall).
        # Only ``ResponseFunctionToolCall`` in the ToolCallItemTypes union
        # carries all three fields; other members don't emit ToolCallRequest
        # events in practice (this executor only wires function tools).
        proto = cast(_ToolCallRawItem, raw_item)
        name = proto.name
        args_text = proto.arguments
        call_id = proto.call_id or proto.id

    args: ToolArgs
    try:
        args = cast(ToolArgs, json.loads(args_text)) if args_text else {}
    except (TypeError, json.JSONDecodeError):
        args = {"raw": args_text}
    # ``RawToolItemParts.name`` is typed ``str`` because every
    # downstream consumer emits it as the tool-call name; widening to
    # ``str | None`` here would cascade into ``ToolCallRequest`` and
    # force ``None``-handling on every callsite for a case that
    # indicates a malformed SDK payload. Keep the coercion localized
    # with an explicit ternary at this dataclass boundary.
    resolved_name: str = name if name is not None else ""
    return RawToolItemParts(name=resolved_name, args=args, call_id=call_id)


def _build_reasoning_model_settings(effort: str | None) -> dict[str, object]:
    """Build ModelSettings kwargs for reasoning effort."""
    if effort is None:
        return {}
    try:
        from openai.types.shared import Reasoning
    except ImportError:
        return {"reasoning": {"effort": effort}}
    openai_effort = cast(
        Literal["none", "minimal", "low", "medium", "high", "xhigh"],
        effort,
    )
    return {"reasoning": Reasoning(effort=openai_effort)}


def _is_databricks_openai_client(client: AsyncOpenAIClient) -> bool:
    """Return whether *client* targets a Databricks AI Gateway base URL."""
    return "/ai-gateway/" in str(getattr(client, "base_url", ""))


class _ReasoningBlockFilterStream:
    """Async stream wrapper that converts list-type ``delta.content`` to ``None``.

    Kimi K2 (and similar reasoning models served via Databricks) sends
    thinking/reasoning blocks as list-type content in streaming deltas:
    ``choices[0].delta.content = [{'type': 'reasoning', ...}]``.
    The openai-agents SDK's ``ChatCmplStreamHandler`` creates a
    ``ResponseTextDeltaEvent(delta=content)`` which Pydantic rejects for
    list input — ``delta`` must be ``str``.

    This wrapper replaces list-type content with ``None`` (the SDK treats
    ``None`` as "no text delta") so the reasoning tokens are silently
    discarded at the stream boundary and only the final answer text
    reaches the SDK.

    :param stream: The underlying ``AsyncStream[ChatCompletionChunk]``
        from ``AsyncOpenAI.chat.completions.create(stream=True)``.
    """

    def __init__(self, stream: Any) -> None:  # type: ignore[explicit-any]
        """
        :param stream: Underlying async stream of chat completion chunks.
        """
        self._stream = stream

    def __aiter__(self) -> _ReasoningBlockFilterStream:
        return self

    async def __anext__(self) -> Any:  # type: ignore[explicit-any]
        """
        :returns: The next chunk, with list-type ``delta.content`` replaced
            by ``None``.
        :raises StopAsyncIteration: When the underlying stream is exhausted.
        """
        chunk = await self._stream.__anext__()
        for choice in chunk.choices:
            if isinstance(getattr(choice.delta, "content", None), list):
                # Bypass Pydantic's __setattr__ validation — the field type
                # in ChoiceDelta is ``str | None`` and the list value from
                # reasoning models fails validation if assigned normally.
                object.__setattr__(choice.delta, "content", None)
        return chunk

    async def __aenter__(self) -> _ReasoningBlockFilterStream:
        if hasattr(self._stream, "__aenter__"):
            await self._stream.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        if hasattr(self._stream, "__aexit__"):
            await self._stream.__aexit__(*args)

    def __getattr__(self, name: str) -> Any:  # type: ignore[explicit-any]
        return getattr(self._stream, name)


class _ReasoningBlockFilterCompletions:
    """Wraps ``AsyncCompletions`` to inject :class:`_ReasoningBlockFilterStream`.

    Sits between the openai-agents SDK's ``OpenAIProvider`` and the real
    ``AsyncOpenAI.chat.completions`` so reasoning-model list content never
    reaches ``ChatCmplStreamHandler``.

    :param completions: The real ``AsyncCompletions`` object.
    """

    def __init__(self, completions: Any) -> None:  # type: ignore[explicit-any]
        self._completions = completions

    async def create(self, **kwargs: Any) -> Any:  # type: ignore[explicit-any]
        """
        Proxy ``create()``; wrap the result in
        :class:`_ReasoningBlockFilterStream` when streaming is enabled.

        :param kwargs: Forwarded verbatim to the underlying ``create()``.
        :returns: A :class:`_ReasoningBlockFilterStream` when ``stream=True``,
            otherwise the raw ``ChatCompletion`` response.
        """
        result = await self._completions.create(**kwargs)
        if kwargs.get("stream") and hasattr(result, "__anext__"):
            return _ReasoningBlockFilterStream(result)
        return result

    def __getattr__(self, name: str) -> Any:  # type: ignore[explicit-any]
        return getattr(self._completions, name)


class _ReasoningBlockFilterChat:
    """Wraps ``AsyncChat`` to expose a :class:`_ReasoningBlockFilterCompletions`.

    :param chat: The real ``AsyncOpenAI.chat`` object.
    """

    def __init__(self, chat: Any) -> None:  # type: ignore[explicit-any]
        self._chat = chat
        self.completions: _ReasoningBlockFilterCompletions = _ReasoningBlockFilterCompletions(
            chat.completions
        )

    def __getattr__(self, name: str) -> Any:  # type: ignore[explicit-any]
        return getattr(self._chat, name)


def _wrap_client_for_reasoning_models(client: AsyncOpenAIClient) -> AsyncOpenAIClient:
    """Wrap *client* so reasoning-model list content is filtered from streams.

    Replaces ``client.chat`` with a :class:`_ReasoningBlockFilterChat`
    proxy that intercepts ``chat.completions.create(stream=True)`` and
    wraps the result in :class:`_ReasoningBlockFilterStream`.  All other
    attribute accesses fall through to the real client.

    :param client: The ``AsyncOpenAI`` (or compatible) client to wrap.
    :returns: The same *client* object with ``chat`` replaced by the filter
        proxy. The object is modified in-place and returned for chaining.
    """
    # Patch ``chat`` directly on the client instance so the proxy intercepts
    # every ``client.chat.completions.create()`` call the SDK makes.
    # ``object.__setattr__`` bypasses both the OpenAI SDK's own ``__setattr__``
    # and any Pydantic frozen-instance guard on the client class.
    object.__setattr__(client, "chat", _ReasoningBlockFilterChat(client.chat))
    return client


def _count_output_items(new_items: list[Any]) -> int:  # type: ignore[explicit-any]
    """Count run items that represent user-visible output.

    Excludes bookkeeping items (reasoning, compaction) per
    :data:`_NON_OUTPUT_ITEM_TYPES` so a reasoning-only ghost turn
    counts as zero output items.

    :param new_items: ``RunResult.new_items`` for the completed run.
    :returns: Number of items whose ``.type`` is not in
        :data:`_NON_OUTPUT_ITEM_TYPES`.
    """
    return sum(
        1 for item in new_items if getattr(item, "type", None) not in _NON_OUTPUT_ITEM_TYPES
    )


def _sum_output_tokens(raw_responses: list[Any] | None) -> int:  # type: ignore[explicit-any]
    """Sum ``output_tokens`` across a run's raw model responses.

    :param raw_responses: ``RunResult.raw_responses``; each element has a
        ``.usage.output_tokens``. ``None`` or empty yields ``0``.
    :returns: Total output tokens reported across all sub-turn responses.
    """
    if not raw_responses:
        return 0
    return sum(getattr(r.usage, "output_tokens", 0) or 0 for r in raw_responses)


def _is_empty_turn(
    final_text: str,
    saw_tool_activity: bool,
    new_items: list[Any],  # type: ignore[explicit-any]
) -> bool:
    """Whether a completed run produced literally nothing worth surfacing.

    A turn is empty when it has no final text, no tool activity, and no
    output-bearing items. A turn that called tools but produced no text
    is NOT empty (tool activity is legitimate output).

    Token usage is deliberately NOT part of this predicate: it must not
    suppress a retry (gateway token accounting can be unreliable, and a
    token quirk should not stop us from re-running an empty turn). The
    fail-loud decision after the retry loop applies a *separate*,
    narrower gate (``output_tokens == 0``) on top of this one — see the
    call site for why an empty turn that still billed tokens is left to
    complete silently rather than raised as an error.

    :param final_text: The assembled assistant text for the turn.
    :param saw_tool_activity: ``True`` if any tool call was streamed.
    :param new_items: ``RunResult.new_items`` for the completed run.
    :returns: ``True`` if the turn is empty and should be retried.
    """
    return not final_text and not saw_tool_activity and _count_output_items(new_items) == 0


class OpenAIAgentsSDKExecutor(Executor):
    """Execute turns using the OpenAI Agents SDK."""

    def __init__(
        self,
        *,
        client: AsyncOpenAIClient = None,
        profile: str | None = None,
        api_key: str | None = None,
        use_responses: bool = True,
        model: str | None = None,
        retry_policy: RetryPolicy | None = None,
        base_url_override: str | None = None,
        gateway_host: str | None = None,
        gateway_auth_command: str | None = None,
    ) -> None:
        """Create an OpenAIAgentsSDKExecutor.

        :param client: A preconfigured ``openai.AsyncOpenAI`` client.  When
            ``None`` the executor calls :func:`_get_openai_async_client`.
        :param profile: Optional ``~/.databrickscfg`` profile name for the
            Databricks fallback path, e.g. ``"<your-profile>"``.
        :param api_key: Direct OpenAI-compatible API key, e.g.
            ``"sk-proj-..."``. When set, constructs the client with this key
            directly, bypassing profile resolution and env-var lookups. Set
            from ``HARNESS_OPENAI_AGENTS_API_KEY`` when the agent spec
            declares ``executor.auth: {type: api_key, api_key: …}``.
        :param use_responses: When ``True`` the executor talks to the
            OpenAI ``/responses`` endpoint; when ``False`` it falls back to
            ``/chat/completions``.
        :param model: Optional constructor-level model default. Acts
            as the spec-level default — applied when a per-turn
            ``ExecutorConfig.model`` is not set. A non-None
            ``cfg.model`` (set by AP's
            the harness HTTP client when the request carries
            a ``model_override``) wins over this value, so the REPL's
            ``/model`` slash command can shadow the spec model
            per-request. The harness wrap reads
            ``HARNESS_OPENAI_AGENTS_MODEL`` from the spawn env and
            threads the value here so the harness can pin a model
            independently of the per-call ``request.model`` (which AP
            uses to identify the AGENT, not the underlying LLM).
            ``None`` falls back to ``cfg.model`` then the executor's
            built-in default.
        :param base_url_override: Override the OpenAI-compatible base URL
            instead of deriving it from the Databricks profile host.  Set
            from ``HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL`` (written by
            the Omnigent workflow layer). Required whenever ``gateway_host`` is set.
        :param gateway_host: Gateway workspace host origin, e.g.
            ``"https://example.databricks.com"``.  Set from
            ``HARNESS_OPENAI_AGENTS_GATEWAY_HOST`` (written by the AP
            workflow layer). When set, skips profile host lookup and requires
            the gateway base URL and auth command values.
        :param gateway_auth_command: Shell command that prints a bearer token,
            e.g.
            ``"databricks auth token --host https://example.databricks.com ..."``
            or ``"printf %s sk-..."``. Set from
            ``HARNESS_OPENAI_AGENTS_GATEWAY_AUTH_COMMAND``.
        """
        self._retry_policy = retry_policy if retry_policy is not None else RetryPolicy()
        raw_client = (
            client
            if client is not None
            else _get_openai_async_client(
                profile=profile,
                api_key=api_key,
                retry_policy=self._retry_policy,
                base_url_override=base_url_override,
                host_override=gateway_host,
                databricks_auth_command=gateway_auth_command,
                model=model,
            )
        )
        # Wrap the chat.completions path to strip list-type delta.content
        # (reasoning blocks emitted by models like Kimi K2).  The SDK's
        # ChatCmplStreamHandler validates delta as str; list input raises
        # ValidationError and the turn silently produces no output.
        # The wrapper is a no-op for models that always return str content.
        # Only needed for the chat-completions path; the Responses API path
        # has its own event handling that doesn't go through ChatCmplStreamHandler.
        self._client = (
            _wrap_client_for_reasoning_models(raw_client) if not use_responses else raw_client
        )
        self._profile = profile
        self._use_responses = use_responses
        self._model_override = model
        self._databricks = _is_databricks_openai_client(self._client)
        self._tool_executor: ToolExecutor | None = None
        self._session_states: dict[str, _AgentsSessionState] = {}

    def supports_streaming(self) -> bool:
        return True

    def supports_tool_calling(self) -> bool:
        return True

    def handles_tools_internally(self) -> bool:
        return True

    def supports_stepwise_internal_turns(self) -> bool:
        return True

    def max_context_tokens(self) -> int | None:
        return None

    def _session_key(self, messages: list[Message]) -> str:
        if messages:
            if messages[-1].get("session_id"):
                return str(messages[-1]["session_id"])
            metadata = messages[-1].get("metadata", {})
            if isinstance(metadata, dict) and metadata.get("session_id"):
                return str(metadata["session_id"])
        return "default"

    def _get_or_create_session_state(
        self, agents_sdk: _AgentsSDK, session_key: str
    ) -> _AgentsSessionState:
        state = self._session_states.get(session_key)
        if state is not None:
            return state

        sdk_session = _SanitizingSession(agents_sdk.SQLiteSession(session_key))
        state = _AgentsSessionState(sdk_session=sdk_session)
        self._session_states[session_key] = state
        return state

    async def close_session(self, session_key: str) -> None:
        self._session_states.pop(session_key, None)

    async def interrupt_session(self, session_key: str) -> bool:
        """
        Halt the in-flight turn for *session_key* and roll the
        SDK session back to its pre-turn state.

        Cancellation strategy: cancel the per-turn
        ``stream_consumer_task`` task (set up in :meth:`run_turn`).
        That task is parked on
        ``await self._event_queue.get()`` deep inside the SDK's
        ``stream_events`` (agents/result.py:721). Cancelling our
        task lands ``CancelledError`` exactly there — and the
        SDK's own ``except CancelledError`` block (result.py:722-
        725) catches it, calls ``self.cancel()`` on the
        ``RunResultStreaming``, and re-raises. ``self.cancel()``
        in turn cancels the SDK's run-loop task, which closes the
        underlying httpx response stream when its async-with
        unwinds — finally stopping the network bytes that were
        producing 1,500+ post-cancel deltas in the original bug.

        Calling ``active.cancel(mode="immediate")`` on the result
        directly does *not* close the network stream — it only
        sets a flag (``_cancel_mode``) and pushes a sentinel; if
        the consumer is currently awaiting ``queue.get()``,
        ``_drain_event_queue`` is skipped and pre-buffered events
        keep flowing. The task-cancel path above is the SDK's
        documented contract for "really" stopping a stream.

        :param session_key: Key for the per-session state, e.g.
            ``"conv_abc123"``. ``"default"`` for un-keyed turns.
        :returns: ``True`` if a turn was in flight and the
            cancel was issued; ``False`` if no active result
            exists for this session.
        """
        state = self._session_states.get(session_key)
        if state is None or state.active_result is None:
            return False
        state.interrupt_requested = True
        # Cancel the consumer task so CancelledError lands inside
        # the SDK's stream_events()'s queue.get() — see the class
        # docstring above for why this is the *only* path that
        # actually halts the underlying httpx stream.
        consumer = state.stream_consumer_task
        if consumer is not None and not consumer.done():
            consumer.cancel()
        state.resume_state = None
        state.rollback_to_item_count = state.run_item_count_before
        return True

    async def _rewind_sdk_session(
        self,
        state: _AgentsSessionState,
        target_item_count: int,
    ) -> None:
        items = await state.sdk_session.get_items()
        while len(items) > target_item_count:
            await state.sdk_session.pop_item()
            items = await state.sdk_session.get_items()

    async def _prepare_sdk_session_for_turn(
        self,
        state: _AgentsSessionState,
        messages: list[Message],
    ) -> None:
        if state.rollback_to_item_count is not None:
            await self._rewind_sdk_session(state, state.rollback_to_item_count)
            state.rollback_to_item_count = None

        if state.started and len(split_transient_tail(messages).persisted) < state.history_cursor:
            await self._rewind_sdk_session(state, 0)
            state.started = False
            state.resume_state = None
            state.history_cursor = 0

    def _build_input_for_turn(
        self,
        state: _AgentsSessionState,
        messages: list[Message],
        *,
        allow_resume_without_new_input: bool,
    ) -> str | list[Message] | _RunState:
        if state.resume_state is not None and allow_resume_without_new_input:
            resume_state = state.resume_state
            state.resume_state = None
            resume_state._current_turn = 0
            resume_state._max_turns = 1
            resume_state._current_turn_persisted_item_count = 0
            return resume_state

        if state.resume_state is not None:
            state.resume_state = None

        if not state.started:
            return _normalize_responses_items_for_chat(_convert_messages_to_responses(messages))

        split = split_transient_tail(messages)
        delta_persisted = split.persisted[state.history_cursor :]
        delta_messages = list(delta_persisted) + list(split.transient)
        if not delta_messages:
            return ""
        if len(delta_messages) == 1 and delta_messages[0].get("role") == "user":
            content = delta_messages[0].get("content")
            if content is None:
                return ""
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                # Normalize input_file blocks before handing to the SDK.
                # The openai-agents SDK's chatcmpl_converter passes
                # ``file_data`` directly to the Chat Completions ``file``
                # content type, which expects plain base64 — not the data:
                # URI that content_resolver produces.  The Databricks GPT
                # endpoint may not support ``file`` content blocks at all.
                # Converting to ``input_text`` is the universally compatible
                # path: the model sees the file content as plain text.
                normalized = _normalize_content_blocks_for_chat(content)
                return [{"type": "message", "role": "user", "content": normalized}]
            return json.dumps(content)
        return _normalize_responses_items_for_chat(_convert_messages_to_responses(delta_messages))

    def _build_tools(
        self,
        agents_sdk: _AgentsSDK,
        tools: list[ToolSpec],
    ) -> list[SDKTool]:
        sdk_tools: list[SDKTool] = []
        for tool in tools:
            raw_name = tool.get("name")
            # The OpenAI Agents SDK's ``FunctionTool`` requires a
            # non-empty ``name``; drop malformed specs rather than
            # registering an unnamed tool the SDK will reject.
            if not isinstance(raw_name, str) or not raw_name:
                continue
            tool_name: str = raw_name
            raw_desc = tool.get("description")
            tool_desc: str = raw_desc if isinstance(raw_desc, str) else ""
            params = tool.get("parameters", {"type": "object", "properties": {}})

            async def on_invoke_tool(
                _ctx: ToolResult,
                input_json: str,
                *,
                _tool_name: str = tool_name,
            ) -> ToolResult:
                if self._tool_executor is None:
                    return {"error": f"No tool executor for '{_tool_name}'"}

                try:
                    args = json.loads(input_json) if input_json else {}
                except (TypeError, json.JSONDecodeError):
                    args = {}

                result = self._tool_executor(_tool_name, args)
                if hasattr(result, "__await__"):
                    result = await result  # type: ignore[assignment]
                return result

            sdk_tools.append(
                agents_sdk.FunctionTool(
                    name=tool_name,
                    description=tool_desc,
                    params_json_schema=params,
                    on_invoke_tool=on_invoke_tool,
                    strict_json_schema=False,
                )
            )

        return sdk_tools

    @staticmethod
    def _tool_signature(tools: list[ToolSpec]) -> str:
        normalized = [
            {
                # Signature is used only as a cache key; missing fields
                # are normalised to ``None`` so tool specs that differ
                # only by absent keys still produce distinct signatures
                # from ones that explicitly set empty strings.
                "name": tool.get("name"),
                "description": tool.get("description"),
                "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
            }
            for tool in tools
        ]
        return json.dumps(normalized, sort_keys=True, separators=(",", ":"))

    def _get_or_create_agent(
        self,
        agents_sdk: _AgentsSDK,
        state: _AgentsSessionState,
        *,
        model: str,
        system_prompt: str,
        tools: list[ToolSpec],
        parallel_tool_calls: bool | None,
        reasoning_effort: str | None,
        max_tokens: int | None,
    ) -> SDKAgent:
        signature = (
            model,
            system_prompt,
            self._tool_signature(tools),
            json.dumps(parallel_tool_calls),
            reasoning_effort or "",
            json.dumps(max_tokens),
        )
        if state.agent is not None and state.agent_signature == signature:
            return state.agent

        state.agent = agents_sdk.Agent(
            name="Omnigent",
            instructions=system_prompt or None,
            model=model,
            model_settings=agents_sdk.ModelSettings(
                parallel_tool_calls=parallel_tool_calls,
                max_tokens=max_tokens,
                **_build_reasoning_model_settings(reasoning_effort),
            ),
            tools=self._build_tools(agents_sdk, tools),
            tool_use_behavior="run_llm_again",
        )
        state.agent_signature = signature
        return state.agent

    async def _consume_stream_into_queue(
        self,
        result: _RunResult,
        queue: asyncio.Queue[object],
    ) -> None:
        """
        Pump the SDK's ``stream_events`` into a local queue.

        Runs as a separate :class:`asyncio.Task` so
        :meth:`interrupt_session` can cancel us while
        :meth:`run_turn` is blocked on the queue. Cancellation
        lands inside ``await self._event_queue.get()`` deep in
        the SDK's ``stream_events`` (agents/result.py:721) — and
        the SDK's own ``except CancelledError`` (result.py:722-
        725) catches it, calls ``self.cancel()`` on the
        ``RunResultStreaming`` (which closes the underlying
        httpx response stream by cancelling the run-loop task),
        and re-raises.

        Without this task indirection, the only way to signal
        the SDK to stop is the public
        ``result.cancel(mode="immediate")`` API, which sets a
        flag and pushes a sentinel but does NOT close the
        network connection — events keep streaming for 15+
        seconds.

        :param result: The SDK's ``RunResultStreaming``
            instance for the in-flight turn.
        :param queue: Per-turn unbounded :class:`asyncio.Queue`
            owned by :meth:`run_turn`. Receives every
            ``StreamEvent`` the SDK emits, then a single
            :data:`_STREAM_DONE` sentinel in this method's
            ``finally`` (so the consumer sees a terminal even
            on cancellation / exception).
        :raises asyncio.CancelledError: When :meth:`interrupt_session`
            cancels this task. Caller (run_turn) awaits the
            task after the sentinel and expects this on the
            interrupt path.
        """
        try:
            async for event in result.stream_events():
                queue.put_nowait(event)
        finally:
            # Always push the sentinel — even on CancelledError /
            # exception — so the consumer's ``await queue.get()``
            # unblocks and the run_turn finally can clean up.
            queue.put_nowait(_STREAM_DONE)

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        cfg = config or ExecutorConfig()
        # cfg.model (per-request /model override; agent name no longer
        # leaks here) wins over the spec default
        # (HARNESS_OPENAI_AGENTS_MODEL → self._model_override).
        model = (
            cfg.model
            or self._model_override
            or (
                _DATABRICKS_OPENAI_AGENTS_DEFAULT_MODEL
                if self._databricks
                else _OPENAI_AGENTS_DEFAULT_MODEL
            )
        )
        agents_sdk = cast(_AgentsSDK, _ensure_agents_sdk())
        session_key = self._session_key(messages)
        try:
            reasoning_effort = validate_effort(
                cfg.extra.get("reasoning_effort"), "OpenAI Agents SDK", OPENAI_AGENTS_EFFORTS
            )
        except ValueError as exc:
            yield ExecutorError(message=str(exc), retryable=False)
            return

        state = self._get_or_create_session_state(agents_sdk, session_key)
        state.interrupt_requested = False
        await self._prepare_sdk_session_for_turn(state, messages)
        stepwise_internal_turns = bool(cfg.extra.get("stepwise_internal_turns"))
        input_value = self._build_input_for_turn(
            state,
            messages,
            allow_resume_without_new_input=(
                stepwise_internal_turns and not bool(cfg.extra.get("new_user_messages_flushed"))
            ),
        )
        parallel_tool_calls = cfg.extra.get("parallel_tool_calls", None)
        max_tokens_raw = cfg.extra.get("max_tokens")
        max_tokens = int(max_tokens_raw) if max_tokens_raw is not None else None
        provider = agents_sdk.OpenAIProvider(
            openai_client=self._client,
            use_responses=self._use_responses,
        )
        agent = self._get_or_create_agent(
            agents_sdk,
            state,
            model=model,
            system_prompt=system_prompt,
            tools=tools,
            parallel_tool_calls=parallel_tool_calls,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
        )
        current_item_count = len(await state.sdk_session.get_items())
        run_config = agents_sdk.RunConfig(
            model=model,
            model_provider=provider,
            tracing_disabled=True,
            reasoning_item_id_policy="omit",
            call_model_input_filter=self._filter_model_input,
        )
        max_turns = 1 if stepwise_internal_turns else int(cfg.extra.get("max_turns", 1000))

        # ── LLM_REQUEST policy evaluation ────────────────────────
        # If the executor adapter installed a ``_policy_evaluator``
        # callback, call it with the request data so the Omnigent server
        # can evaluate LLM_REQUEST policies before the LLM call.
        # This mirrors the identical block in ``OpenResponsesExecutor``
        # and ``ClaudeSDKExecutor``.
        _policy_eval = getattr(self, "_policy_evaluator", None)
        if _policy_eval is not None:
            _last_user_msg = ""
            for _msg in reversed(messages):
                if _msg.get("role") == "user":
                    _content = _msg.get("content")
                    if isinstance(_content, str):
                        _last_user_msg = _content[:500]
                    elif isinstance(_content, list):
                        _parts = [
                            b.get("text", "")
                            for b in _content
                            if isinstance(b, dict) and b.get("type") in ("input_text", "text")
                        ]
                        _last_user_msg = " ".join(_parts)[:500]
                    break
            _req_data: dict[str, Any] = {
                "model": model,
                "messages_count": len(messages),
                "tools_count": len(tools),
                "system_prompt_preview": (system_prompt[:200] if system_prompt else ""),
                "last_user_message": _last_user_msg,
            }
            _req_verdict = await _policy_eval("PHASE_LLM_REQUEST", _req_data)
            if _req_verdict.action == "POLICY_ACTION_DENY":
                _deny_reason = _req_verdict.reason or "no reason given"
                yield ExecutorError(message=f"LLM call denied by policy: {_deny_reason}")
                return

        # Run the turn, retrying once if the gateway returns a
        # completed-but-empty turn (see ``_EMPTY_TURN_MAX_ATTEMPTS``).
        # ``_is_empty_turn`` guarantees an empty attempt streamed nothing
        # (no text, no tool calls), so retrying never double-emits — we
        # stream each attempt's events live and only defer the terminal
        # TurnComplete until the emptiness decision after the loop.
        result: _RunResult | None = None
        response_text = ""
        saw_tool_activity = False
        final_text = ""
        for attempt in range(_EMPTY_TURN_MAX_ATTEMPTS):
            response_text = ""
            pending_tools: dict[str, tuple[str, float]] = {}
            saw_tool_activity = False
            result = None
            # Per-turn unbounded queue bridging the stream-pump task
            # (producer) with this generator (consumer). Unbounded so
            # the producer's ``put_nowait`` never blocks — keeping the
            # producer parked on ``stream_events()``'s ``queue.get()``
            # is what makes :meth:`interrupt_session`'s ``task.cancel()``
            # land at the SDK boundary instead of in our queue plumbing.
            event_queue: asyncio.Queue[object] = asyncio.Queue()
            try:
                # Reuse the count already fetched above; avoids a second
                # ``get_items()`` round-trip.
                state.run_item_count_before = current_item_count
                result = cast(
                    _RunResult,
                    agents_sdk.Runner.run_streamed(
                        agent,
                        input=input_value,
                        session=state.sdk_session,
                        max_turns=max_turns,
                        run_config=run_config,
                    ),
                )
                state.active_result = result
                state.stream_consumer_task = asyncio.create_task(
                    self._consume_stream_into_queue(result, event_queue),
                    name=f"openai-agents-stream-pump:{session_key}",
                )
                while True:
                    item = await event_queue.get()
                    if item is _STREAM_DONE:
                        break
                    # Drop everything queued after interrupt, but keep
                    # draining so the sentinel can land and the finally
                    # block can run cleanly.
                    if state.interrupt_requested:
                        continue

                    event = cast(_StreamEvent, item)
                    if event.type == "raw_response_event":
                        raw_event = cast(_RawResponseEvent, event)
                        data = raw_event.data
                        if data.type == "response.output_text.delta":
                            text = data.delta
                            if text:
                                response_text += text
                                yield TextChunk(text=text)

                    elif event.type == "run_item_stream_event":
                        item_event = cast(_RunItemEvent, event)
                        sdk_item = item_event.item
                        if sdk_item.type == "tool_call_item":
                            parts = _tool_args_from_raw_item(sdk_item.raw_item)
                            pending_tools[parts.call_id or parts.name] = (
                                parts.name,
                                time.monotonic(),
                            )
                            saw_tool_activity = True
                            yield ToolCallRequest(
                                name=parts.name,
                                args=parts.args,
                                metadata={"call_id": parts.call_id} if parts.call_id else {},
                            )
                        elif sdk_item.type == "tool_call_output_item":
                            raw_item = sdk_item.raw_item
                            call_id = None
                            if isinstance(raw_item, dict):
                                raw_dict: ToolArgs = raw_item
                                maybe_cid = raw_dict.get("call_id") or raw_dict.get("id")
                                call_id = maybe_cid if isinstance(maybe_cid, str) else None
                            else:
                                out_proto = cast(_ToolCallOutputRawItem, raw_item)
                                call_id = out_proto.call_id or out_proto.id
                            name = ""
                            started: float | None = None
                            # ``call_id`` is ``None`` when the SDK didn't
                            # surface one; in that case we can't look up
                            # a pending tool and emit with defaults.
                            if call_id is not None and call_id in pending_tools:
                                name, started = pending_tools.pop(call_id)
                            duration_ms = ((time.monotonic() - started) * 1000) if started else 0.0
                            classification = classify_tool_result(sdk_item.output)
                            yield ToolCallComplete(
                                name=name,
                                status=classification.status,
                                result=sdk_item.output,
                                error=classification.error,
                                duration_ms=duration_ms,
                                metadata={"call_id": call_id} if call_id else {},
                            )

                # Producer signalled completion via _STREAM_DONE.
                # Surface any non-cancel exception it raised so the
                # outer try/except below can translate it to the
                # MaxTurnsExceeded / generic-error paths exactly as
                # before — these used to fire from inside the inline
                # ``async for`` loop; now they fire from the producer.
                pump = state.stream_consumer_task
                assert pump is not None and pump.done()
                # On the interrupt path, ``pump.result()`` raises
                # CancelledError — state.interrupt_requested is True,
                # the consumer already drained without emitting, and
                # the rollback was set up by interrupt_session.
                # Nothing more to do here. Non-cancel exceptions from
                # the producer (MaxTurnsExceeded, generic SDK errors)
                # propagate to the outer try/except blocks below.
                with contextlib.suppress(asyncio.CancelledError):
                    pump.result()

                state.started = True
                state.resume_state = None
            except agents_sdk.MaxTurnsExceeded:
                if state.interrupt_requested:
                    return
                if stepwise_internal_turns and saw_tool_activity and result is not None:
                    state.started = True
                    state.resume_state = result.to_state()
                    state.history_cursor = len(split_transient_tail(messages).persisted)
                    yield TurnComplete(continue_turn=True)
                    return
                logger.error("OpenAIAgentsSDKExecutor: max turns exceeded")
                yield ExecutorError(message=f"OpenAI Agents SDK exceeded max turns ({max_turns})")
                return
            except Exception as exc:  # re-raises context overflow, classifies the rest
                if state.interrupt_requested:
                    return
                if _is_context_length_exceeded(exc):
                    # Let the runtime compaction layer handle context
                    # overflow.  Re-raising propagates the original
                    # exception to the ExecutorAdapter, whose error
                    # classifier maps it to ``context_length_exceeded``
                    # so the workflow's reactive compaction fires.
                    raise
                from .databricks_executor import DatabricksAuthError

                if isinstance(exc, DatabricksAuthError) or (
                    exc.__cause__ is not None and isinstance(exc.__cause__, DatabricksAuthError)
                ):
                    # When exc IS the DatabricksAuthError, use str(exc) — it
                    # carries the actionable "Run: databricks auth login -p X"
                    # message. Its __cause__ is the raw SDK exception (e.g.
                    # ValueError("token expired")), which is NOT actionable.
                    # When exc.__cause__ IS the DatabricksAuthError (exc is a
                    # wrapper), str(exc.__cause__) is correct.
                    auth_msg = (
                        str(exc) if isinstance(exc, DatabricksAuthError) else str(exc.__cause__)
                    )
                    logger.error("OpenAIAgentsSDKExecutor: auth failed: %s", auth_msg)
                    yield ExecutorError(message=auth_msg)
                else:
                    logger.error("OpenAIAgentsSDKExecutor: run failed: %s", exc)
                    yield ExecutorError(message=f"OpenAI Agents SDK error: {exc}")
                return
            finally:
                # If the outer generator was aclose'd before the
                # drain completed (e.g. ExecutorAdapter returned
                # without going through interrupt_session), the
                # pump task is still alive — cancel it here so it
                # doesn't outlive run_turn.
                pump_task = state.stream_consumer_task
                if pump_task is not None and not pump_task.done():
                    pump_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await pump_task
                state.stream_consumer_task = None
                state.active_result = None
                state.run_item_count_before = None

            if state.interrupt_requested:
                return
            assert result is not None  # mypy: try block either assigns or raises/returns
            final_output = result.final_output
            if isinstance(final_output, str):
                final_text = final_output
            else:
                final_text = agents_sdk.ItemHelpers.text_message_outputs(result.new_items)
            if not final_text:
                final_text = response_text

            if not _is_empty_turn(final_text, saw_tool_activity, result.new_items):
                break  # got real output: text, tool activity, or output items

            # Empty turn. Retry from a clean state if attempts remain;
            # otherwise fall through to the fail-loud gate below.
            if attempt + 1 < _EMPTY_TURN_MAX_ATTEMPTS:
                logger.warning(
                    "OpenAIAgentsSDKExecutor: empty completion (attempt %d/%d), retrying",
                    attempt + 1,
                    _EMPTY_TURN_MAX_ATTEMPTS,
                )
                # Roll the SDK session back to its pre-turn item count so
                # the retry re-runs from an identical state — the empty
                # attempt may have appended a stray empty assistant item.
                await self._rewind_sdk_session(state, current_item_count)

        # ``final_text`` / ``result`` are from the surfaced (last) attempt.
        # If still empty AND the gateway reported zero output tokens, the
        # turn is a gateway hiccup, not a deliberate empty answer (which
        # still bills output tokens): surface a loud retryable error so
        # the workflow's retry policy can reissue, rather than a silent
        # empty turn.
        #
        # The zero-token condition deliberately narrows fail-loud to the
        # signature we actually observed (status=completed,
        # output=[], 0 output tokens). An empty turn that DID bill tokens
        # falls through to the silent ``TurnComplete("")`` below — exactly
        # today's behavior, so this change never makes a billed-but-empty
        # turn worse. If a billed-but-empty gateway failure shows up later,
        # widen the gate then, with that evidence in hand.
        assert result is not None
        if _is_empty_turn(final_text, saw_tool_activity, result.new_items):
            if _sum_output_tokens(getattr(result, "raw_responses", None)) == 0:
                logger.error(
                    "OpenAIAgentsSDKExecutor: empty completion after %d attempts",
                    _EMPTY_TURN_MAX_ATTEMPTS,
                )
                yield ExecutorError(
                    message=(
                        "openai-agents returned an empty completion after "
                        f"{_EMPTY_TURN_MAX_ATTEMPTS} attempts"
                    ),
                    retryable=True,
                )
                return

        if not response_text and final_text:
            yield TextChunk(text=final_text)
        state.history_cursor = len(split_transient_tail(messages).persisted) + 1
        # Aggregate usage across all raw responses for this turn. The SDK
        # accumulates per-request usage on RunResult.raw_responses.
        #
        # ``input_tokens``, ``output_tokens``, and ``total_tokens`` are
        # summed across all sub-turns for billing accuracy.
        #
        # ``context_tokens`` is set to the *last* sub-turn's total only,
        # ``context_tokens`` is the last sub-turn's total (input +
        # output) — the best proxy for how full the context window
        # will be on the NEXT request (prior conversation + the
        # response just generated). For multi-tool turns, summing
        # input_tokens across sub-turns double-counts (each sub-turn
        # re-sends the full history); the last sub-turn's total is
        # stable. Always set so the REPL and compaction don't fall
        # back to total_tokens (which sums across ALL sub-turns).
        turn_usage: dict[str, Any] | None = None
        raw_responses = getattr(result, "raw_responses", None)
        if raw_responses:
            in_tok = sum(getattr(r.usage, "input_tokens", 0) or 0 for r in raw_responses)
            out_tok = sum(getattr(r.usage, "output_tokens", 0) or 0 for r in raw_responses)
            total_tok = sum(getattr(r.usage, "total_tokens", 0) or 0 for r in raw_responses)
            # OpenAI's ``input_tokens`` (aka ``prompt_tokens``) is the
            # TOTAL input count *including* cached tokens, whereas
            # ``compute_llm_cost`` expects Anthropic semantics where
            # ``input_tokens`` is the non-cached portion and
            # ``cache_read_input_tokens`` is additive. Extract cached
            # tokens from ``prompt_tokens_details.cached_tokens`` and
            # subtract so downstream billing uses the cheaper cache rate.
            cached_tok = 0
            for r in raw_responses:
                details = getattr(r.usage, "prompt_tokens_details", None)
                if details is not None:
                    cached = getattr(details, "cached_tokens", None)
                    if cached is None and isinstance(details, dict):
                        cached = details.get("cached_tokens")
                    cached_tok += cached or 0
            last_r = raw_responses[-1]
            last_in = getattr(last_r.usage, "input_tokens", 0) or 0
            last_out = getattr(last_r.usage, "output_tokens", 0) or 0
            last_total = getattr(last_r.usage, "total_tokens", 0) or 0
            context_tok = last_total if last_total else last_in + last_out
            if in_tok or out_tok:
                turn_usage = {
                    "input_tokens": in_tok - cached_tok,  # non-cached portion
                    "output_tokens": out_tok,
                    "total_tokens": total_tok if total_tok else in_tok + out_tok,
                    "context_tokens": context_tok,
                    # Harness-reported model for cost pricing when the spec pins no model.
                    "model": model,
                }
                if cached_tok:
                    turn_usage["cache_read_input_tokens"] = cached_tok
        _notify_usage_from_dict(model=model, usage=turn_usage)
        yield TurnComplete(response=final_text, usage=turn_usage)

    @staticmethod
    def _filter_model_input(data: _CallModelData) -> _ModelInputData:
        # Sanitize so identity fields (long ``id`` strings) are stripped
        # and content blocks are normalized for the chat-completions path.
        sanitized = [_sanitize_replay_item(item) for item in data.model_data.input]
        data.model_data.input = _normalize_responses_items_for_chat(sanitized)
        return data.model_data
