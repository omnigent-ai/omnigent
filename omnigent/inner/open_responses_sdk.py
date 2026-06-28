"""OpenResponsesExecutor: OpenAI Responses API execution.

Uses the OpenAI Python SDK's Responses API with custom function tools.
This maps Omnigent tool schemas onto OpenAI function tools and keeps the
existing Session-managed tool loop intact.

Environment:
    OPENAI_API_KEY           – direct OpenAI / OpenAI-compatible API key
    OPENAI_BASE_URL          – optional override for OpenAI-compatible endpoints
    DATABRICKS_CONFIG_PROFILE – optional Databricks profile selector
    ~/.databrickscfg         – host + token profile used for Databricks FMAPI passthrough
"""

from __future__ import annotations

import json
import logging
import os
from collections import deque
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeAlias, cast

import pydantic

from omnigent.spec.types import RetryPolicy

if TYPE_CHECKING:
    from openai import OpenAI, Stream
    from openai.types.responses import Response, ResponseOutputItem, ResponseStreamEvent

from .async_utils import run_sync_on_thread
from .executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    TextChunk,
    ToolCallRequest,
    ToolSpec,
    TurnComplete,
    iterate_blocking_stream,
    split_transient_tail,
)

logger = logging.getLogger(__name__)

# OpenAI Responses-API input/output items — heterogeneous JSON-shaped
# dicts. The shapes are documented openly; we only pluck a few fields
# with isinstance narrowing at each site, so a TypedDict tree would
# duplicate OpenAI's own SDK types.
ResponsesItem: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]
OpenAIKwargs: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# Plain JSON value — recursive union used by ``_to_plain_data``.
JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

# Placeholder for the OpenAI SDK's ``api_key`` kwarg on OpenAI-compatible
# endpoints (e.g. Databricks model serving) that authenticate through a
# separate mechanism and ignore the key.
_OPENAI_KEY_PLACEHOLDER = "unused"

_SESSION_ONLY_EXECUTOR_EXTRA_KEYS = {
    "new_user_messages_flushed",
    "stepwise_internal_turns",
}


def _databricks_openai_base_url(host: str) -> str:
    # The OpenAI SDK appends /responses to the base_url automatically.
    # Databricks exposes native OpenAI-compatible Responses at
    # /ai-gateway/openai/v1; /serving-endpoints/responses does not exist.
    host = host.rstrip("/")
    return host + "/ai-gateway/openai/v1"


def _is_legacy_databricks_serving_base_url(client: OpenAI) -> bool:
    # Imported lazily so the ``openai`` dependency stays optional for
    # importers that never actually construct an OpenAI client. The
    # runtime isinstance guard also lets tests pass duck-typed fakes.
    from openai import OpenAI as _OpenAI

    if not isinstance(client, _OpenAI):
        return False
    return "/serving-endpoints" in str(client.base_url)


def _is_databricks_gateway_base_url(client: OpenAI) -> bool:
    """Return whether *client* targets a Databricks AI Gateway base URL.

    The gateway exposes OpenAI-compatible endpoints under ``/ai-gateway/`` and
    rejects the ``strict`` field on tool definitions, so callers omit it when
    this is true. Duck-typed (no isinstance guard) so tests can pass fakes.
    """
    return "/ai-gateway/" in str(getattr(client, "base_url", ""))


def _strip_strict_from_tools(tools: Any) -> Any:
    """Return *tools* with the ``strict`` field removed from each tool envelope.

    The Databricks AI Gateway rejects the ``strict`` field on tool definitions
    by presence (a 400 ``tools.N.*.strict: Extra inputs are not permitted`` —
    ``false`` fails the same as ``true``). Removes only the well-known envelope
    locations: the tool's own ``strict`` (Responses-wire flat function/custom
    tool) and a nested ``function``/``custom`` block's ``strict`` (Chat wire).
    Does NOT recurse into ``parameters`` — a user JSON Schema may legitimately
    contain a property named ``strict``. Copies the list and any mutated dicts
    so the caller's tool objects are not aliased; non-dict entries pass through.

    Shared with :mod:`omnigent.inner.openai_agents_sdk_executor` (which wraps the
    async client) so both executors strip identically.

    :param tools: The outgoing ``tools`` value (normally a list of dicts).
    :returns: A sanitized copy when *tools* is a list; *tools* unchanged otherwise.
    """
    if not isinstance(tools, list):
        return tools
    sanitized = []
    for tool in tools:
        if isinstance(tool, dict):
            tool = dict(tool)
            tool.pop("strict", None)
            for envelope_key in ("function", "custom"):
                envelope = tool.get(envelope_key)
                if isinstance(envelope, dict):
                    envelope = dict(envelope)
                    envelope.pop("strict", None)
                    tool[envelope_key] = envelope
        sanitized.append(tool)
    return sanitized


def _get_openai_client(
    profile: str | None = None,
    retry_policy: RetryPolicy | None = None,
) -> OpenAI:
    """Construct an OpenAI client for the Responses API.

    Supports three configuration modes (in priority order):
      1. Direct OpenAI-compatible: OPENAI_BASE_URL + OPENAI_API_KEY
      2. Direct OpenAI default endpoint: OPENAI_API_KEY
      3. Databricks config file: ~/.databrickscfg

    :param profile: Optional ``~/.databrickscfg`` profile name for the
        Databricks fallback path, e.g. ``"<your-profile>"``.
    :param retry_policy: Optional retry policy. When provided, its
        ``policy.openai.kwargs()`` (max_retries, timeout) are spread
        into the ``OpenAI(...)`` constructor for L0 retry budget.
    """
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ImportError(
            "The 'openai' package is required for OpenResponsesExecutor. "
            "Install it with: pip install openai"
        ) from exc

    policy = retry_policy if retry_policy is not None else RetryPolicy()
    retry_kwargs = policy.openai.kwargs()

    if os.environ.get("OPENAI_BASE_URL"):
        return OpenAI(
            base_url=os.environ["OPENAI_BASE_URL"],
            # Some OpenAI-compatible endpoints (Databricks model serving)
            # don't validate the API key client-side; the SDK still
            # requires a non-empty value, so we supply this documented
            # placeholder when OPENAI_API_KEY isn't set.
            api_key=os.environ.get("OPENAI_API_KEY", _OPENAI_KEY_PLACEHOLDER),
            **retry_kwargs,
        )

    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        return OpenAI(api_key=api_key, **retry_kwargs)

    from .databricks_executor import _read_databrickscfg

    creds = _read_databrickscfg(profile)

    if creds is not None:
        return OpenAI(
            base_url=_databricks_openai_base_url(creds.host),
            api_key=creds.token,
            **retry_kwargs,
        )

    raise OSError(
        "OpenResponsesExecutor requires either "
        "(OPENAI_BASE_URL + OPENAI_API_KEY), "
        "OPENAI_API_KEY, "
        "or a valid ~/.databrickscfg profile with host and token."
    )


def _convert_tools_to_responses(
    tools: list[ToolSpec], *, include_strict: bool = True
) -> list[ResponsesItem]:
    """Convert Omnigent tool schemas to Responses API function tools.

    :param tools: Omnigent tool specs to convert.
    :param include_strict: When True (default), emit ``"strict": False`` to be
        permissive with hand-authored schemas. Set False for the Databricks AI
        Gateway, which rejects the ``strict`` field by presence (a 400
        ``tools.N.*.strict: Extra inputs are not permitted`` — ``false`` fails
        the same as ``true``); the key is then omitted entirely.
    """
    result: list[ResponsesItem] = []
    for tool in tools:
        raw_name = tool.get("name")
        # Responses API function tools require a non-empty ``name``;
        # drop malformed specs rather than emitting an unnamed tool.
        if not isinstance(raw_name, str) or not raw_name:
            continue
        raw_desc = tool.get("description")
        desc: str = raw_desc if isinstance(raw_desc, str) else ""
        item: ResponsesItem = {
            "type": "function",
            "name": raw_name,
            "description": desc,
            "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
        }
        if include_strict:
            # Be permissive with hand-authored schemas in this repo.
            item["strict"] = False
        result.append(item)
    return result


def _normalize_message_content(
    content: Any,  # type: ignore[explicit-any]
    *,
    empty_placeholder: str,
) -> str | list[dict[str, Any]]:
    """
    Normalize a message ``content`` field for the Responses API.

    Passes structured lists (``input_file`` / ``input_image`` / ...)
    through unchanged; ``str()`` would flatten file/image attachments
    to a Python repr and the LLM would never see the actual content.

    :param content: A string, a list of content-part dicts, or
        ``None``. Other shapes are stringified defensively.
    :param empty_placeholder: Substituted for falsy content,
        e.g. ``"(empty)"``. The API rejects empty content blocks.
    :returns: A string or the original list.
    """
    if not content:
        return empty_placeholder
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return content
    return str(content)


def _convert_messages_to_responses(
    messages: list[Message],
) -> list[ResponsesItem]:
    """Convert internal history to Responses API input items for replay/reset."""
    result: list[ResponsesItem] = []

    i = 0
    while i < len(messages):
        msg = messages[i]
        raw_role = msg.get("role")
        role: str | None = raw_role if isinstance(raw_role, str) else None
        content = msg.get("content")

        if role == "user":
            result.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": _normalize_message_content(content, empty_placeholder="(empty)"),
                }
            )

        elif role == "assistant":
            result.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": _normalize_message_content(content, empty_placeholder="(empty)"),
                }
            )

        elif role == "tool_call":
            parsed = content
            if isinstance(parsed, str):
                try:
                    parsed = json.loads(parsed)
                except (TypeError, json.JSONDecodeError):
                    parsed = {}
            if not isinstance(parsed, dict):
                parsed = {}

            raw_tool_name = parsed.get("tool")
            # Responses API ``function_call`` items must carry a
            # ``name``; skip tool_call entries with no tool recorded
            # rather than emitting an empty-string name.
            if not isinstance(raw_tool_name, str) or not raw_tool_name:
                i += 1
                continue
            tool_args = parsed.get("args", {})
            call_id = f"call_{i}"
            result.append(
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": raw_tool_name,
                    "arguments": (
                        json.dumps(tool_args) if isinstance(tool_args, dict) else str(tool_args)
                    ),
                }
            )

            if i + 1 < len(messages) and messages[i + 1].get("role") == "tool_result":
                i += 1
                raw_tool_output = messages[i].get("content")
                if raw_tool_output is None:
                    output_str = ""
                elif isinstance(raw_tool_output, str):
                    output_str = raw_tool_output
                else:
                    output_str = json.dumps(raw_tool_output)
                result.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output_str,
                    }
                )

        elif role == "tool_result":
            tool_output = content if isinstance(content, str) else json.dumps(content)
            result.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": f"[tool result] {tool_output}",
                }
            )

        else:
            result.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": str(content),
                }
            )

        i += 1

    return result


def _extract_response_text(response: Response) -> str:
    """Extract output text from a Responses API response."""
    text = response.output_text
    if text:
        return text

    parts: list[str] = []
    for item in response.output:
        if item.type != "message":
            continue
        for content in item.content:
            if content.type == "output_text":
                parts.append(content.text)
    return "".join(parts)


def _to_plain_data(value: Any) -> JsonValue:  # type: ignore[explicit-any]
    """Convert OpenAI SDK objects into plain JSON-serializable data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_to_plain_data(item) for item in value]
    if isinstance(value, tuple):
        return [_to_plain_data(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_plain_data(item) for key, item in value.items()}
    if isinstance(value, pydantic.BaseModel):
        return _to_plain_data(value.model_dump(by_alias=True, exclude_none=True))
    value_dict = getattr(value, "__dict__", None)
    if isinstance(value_dict, dict):
        data = {key: val for key, val in value_dict.items() if not key.startswith("_")}
        return _to_plain_data(data)
    # Last-resort: we don't know how to convert this type — stringify so
    # the result remains JSON-serializable.
    return str(value)


def _normalize_response_output_items(items: list[ResponseOutputItem]) -> list[ResponsesItem]:
    """Convert response output items into valid replayable input items."""
    result: list[ResponsesItem] = []
    for item in items:
        plain = _to_plain_data(item)
        if not isinstance(plain, dict):
            continue

        item_type = plain.get("type")
        if item_type == "message":
            replay_item = {
                "type": "message",
                "role": plain.get("role", "assistant"),
            }
            if "content" in plain:
                replay_item["content"] = plain["content"]
            result.append(replay_item)
            continue

        if item_type == "function_call":
            raw_call_id = plain.get("call_id")
            raw_name = plain.get("name")
            # ``function_call`` replay items require identity fields;
            # skip malformed entries rather than posting a blank name.
            if not isinstance(raw_call_id, str) or not isinstance(raw_name, str):
                continue
            if not raw_call_id or not raw_name:
                continue
            raw_args = plain.get("arguments")
            arg_str: str = raw_args if isinstance(raw_args, str) else ""
            replay_item = {
                "type": "function_call",
                "call_id": raw_call_id,
                "name": raw_name,
                "arguments": arg_str,
            }
            result.append(replay_item)
            continue

        if item_type == "reasoning":
            # ``summary`` is required by the Responses API on reasoning
            # input items. Default to ``[]`` when the model emitted no
            # summary parts, otherwise the next turn 400s with
            # "Missing required parameter: 'input[N].summary'".
            raw_summary = plain.get("summary")
            replay_item: ResponsesItem = {
                "type": "reasoning",
                "summary": raw_summary if isinstance(raw_summary, list) else [],
            }
            if plain.get("encrypted_content"):
                replay_item["encrypted_content"] = plain["encrypted_content"]
            result.append(replay_item)
            continue

    return result


class OpenResponsesExecutor(Executor):
    """Execute turns with the OpenAI Responses API."""

    def __init__(self, client: OpenAI | None = None, profile: str | None = None) -> None:
        """Create an OpenResponsesExecutor.

        :param client: A preconfigured ``openai.OpenAI`` client.  When ``None``
            the executor calls :func:`_get_openai_client` with ``profile``.
        :param profile: Optional ``~/.databrickscfg`` profile name, passed
            through to :func:`_get_openai_client` when constructing a client.
        """
        self._profile = profile
        self._client = client if client is not None else _get_openai_client(profile=profile)
        self._stream_responses = not _is_legacy_databricks_serving_base_url(self._client)
        # The Databricks AI Gateway rejects the tool ``strict`` field by
        # presence; omit it from converted tools when targeting the gateway.
        self._omit_tool_strict = _is_databricks_gateway_base_url(self._client)
        self._supports_previous_response_id = True
        self._session_states: dict[str, _ResponsesSessionState] = {}

    def _session_key(self, messages: list[Message]) -> str:
        if messages:
            if messages[-1].get("session_id"):
                return str(messages[-1]["session_id"])
            metadata = messages[-1].get("metadata", {})
            if isinstance(metadata, dict) and metadata.get("session_id"):
                return str(metadata["session_id"])
        return "default"

    def _get_or_create_session_state(
        self,
        session_key: str,
    ) -> _ResponsesSessionState:
        state = self._session_states.get(session_key)
        if state is not None:
            return state
        state = _ResponsesSessionState()
        self._session_states[session_key] = state
        return state

    async def close_session(self, session_key: str) -> None:
        self._session_states.pop(session_key, None)

    async def interrupt_session(self, session_key: str) -> bool:
        state = self._session_states.get(session_key)
        if state is None or state.active_stream is None:
            return False
        state.interrupt_requested = True
        await run_sync_on_thread(state.active_stream.close)
        return True

    def _build_delta_input(
        self,
        state: _ResponsesSessionState,
        messages: list[Message],
    ) -> list[ResponsesItem]:
        """Build only the new input items needed to continue a stored response."""
        if not state.previous_response_id:
            return _convert_messages_to_responses(messages)

        split = split_transient_tail(messages)
        if len(split.persisted) < state.history_cursor:
            state.reset()
            return _convert_messages_to_responses(messages)

        delta_messages = list(split.persisted[state.history_cursor :]) + list(split.transient)
        if not delta_messages:
            return []

        result: list[ResponsesItem] = []
        for msg in delta_messages:
            raw_role = msg.get("role")
            role: str | None = raw_role if isinstance(raw_role, str) else None
            content = msg.get("content")

            if role == "tool_call":
                continue

            if role == "tool_result":
                if not state.pending_function_calls:
                    logger.warning(
                        "OpenResponsesExecutor: tool_result without pending function call; "
                        "resetting provider state"
                    )
                    state.reset()
                    return _convert_messages_to_responses(messages)

                pending = state.pending_function_calls.popleft()
                if content is None:
                    output_str = ""
                elif isinstance(content, str):
                    output_str = content
                else:
                    output_str = json.dumps(content)
                result.append(
                    {
                        "type": "function_call_output",
                        "call_id": pending["call_id"],
                        "output": output_str,
                    }
                )
                continue

            result.extend(_convert_messages_to_responses([msg]))

        return result

    def supports_streaming(self) -> bool:
        return True

    def supports_tool_calling(self) -> bool:
        return True

    def max_context_tokens(self) -> int | None:
        return None

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        cfg = config or ExecutorConfig()
        model = cfg.model or "gpt-5.3-codex"
        session_key = self._session_key(messages)
        state = self._get_or_create_session_state(session_key)
        state.interrupt_requested = False
        delta_input_items = self._build_delta_input(state, messages)
        response_tools = (
            _convert_tools_to_responses(tools, include_strict=not self._omit_tool_strict)
            if tools
            else None
        )

        include = ["reasoning.encrypted_content"]
        extra_include = cfg.extra.get("include")
        if isinstance(extra_include, list):
            include.extend(str(item) for item in extra_include)
        elif extra_include:
            include.append(str(extra_include))
        include = list(dict.fromkeys(include))

        kwargs: OpenAIKwargs = {
            "model": model,
            "max_output_tokens": cfg.max_tokens,
            "stream": self._stream_responses,
            "include": include,
        }
        if system_prompt:
            kwargs["instructions"] = system_prompt
        if cfg.temperature:
            kwargs["temperature"] = cfg.temperature
        if response_tools:
            kwargs["tools"] = response_tools
            kwargs["parallel_tool_calls"] = True
        extra = dict(cfg.extra)
        extra.pop("include", None)
        for key in _SESSION_ONLY_EXECUTOR_EXTRA_KEYS:
            extra.pop(key, None)
        kwargs.update(extra)
        # cfg.extra may carry its own ``tools`` that overwrote the converted set
        # above; re-strip the final value so caller-supplied tools can't smuggle
        # the gateway-rejected ``strict`` field past the conversion gate.
        if self._omit_tool_strict and kwargs.get("tools"):
            kwargs["tools"] = _strip_strict_from_tools(kwargs["tools"])
        if self._supports_previous_response_id and state.previous_response_id:
            kwargs["previous_response_id"] = state.previous_response_id
            request_input = delta_input_items
        else:
            request_input = state.conversation_items + delta_input_items
        kwargs["input"] = request_input

        # ── LLM_REQUEST policy evaluation ────────────────────────
        # If the executor adapter installed a ``_policy_evaluator``
        # callback, call it with the request data so the Omnigent server
        # can evaluate LLM_REQUEST policies before the LLM call.
        _policy_eval = getattr(self, "_policy_evaluator", None)
        if _policy_eval is not None:
            # Extract the last user message text for PII scanning.
            _last_user_msg = ""
            for _item in reversed(request_input):
                if isinstance(_item, dict) and _item.get("role") == "user":
                    _content = _item.get("content")
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
                "messages_count": len(request_input),
                "tools_count": len(tools),
                "system_prompt_preview": (system_prompt[:200] if system_prompt else ""),
                "last_user_message": _last_user_msg,
            }
            verdict = await _policy_eval("PHASE_LLM_REQUEST", _req_data)
            if verdict.action == "POLICY_ACTION_DENY":
                yield ExecutorError(
                    message=f"LLM call denied by policy: {verdict.reason or 'no reason given'}"
                )
                return

        try:
            logger.debug(
                "OpenResponsesExecutor: model=%s messages=%d tools=%d "
                "previous_response_id=%s replay_items=%d",
                model,
                len(request_input),
                len(tools),
                kwargs.get("previous_response_id"),
                len(state.conversation_items),
            )
            response_or_stream = await run_sync_on_thread(self._client.responses.create, **kwargs)
            if self._stream_responses:
                state.active_stream = response_or_stream
        except Exception as exc:  # noqa: BLE001 — executor boundary: detects fallback condition or surfaces error
            err_text = str(exc)
            if (
                kwargs.get("previous_response_id")
                and "does not support the `previous_response_id` parameter" in err_text
            ):
                logger.info(
                    "OpenResponsesExecutor: backend rejected previous_response_id; "
                    "falling back to transcript replay"
                )
                self._supports_previous_response_id = False
                kwargs.pop("previous_response_id", None)
                kwargs["input"] = state.conversation_items + delta_input_items
                try:
                    response_or_stream = await run_sync_on_thread(
                        self._client.responses.create, **kwargs
                    )
                    if self._stream_responses:
                        state.active_stream = response_or_stream
                except Exception as retry_exc:  # noqa: BLE001 — executor boundary surfaces retry error as ExecutorError
                    logger.error(
                        "OpenResponsesExecutor: API call failed after replay fallback: %s",
                        retry_exc,
                    )
                    yield ExecutorError(message=f"OpenAI Responses API error: {retry_exc}")
                    return
            else:
                logger.error("OpenResponsesExecutor: API call failed: %s", exc)
                yield ExecutorError(message=f"OpenAI Responses API error: {exc}")
                return

        response_text = ""
        completed_response: Response | None = None
        streamed_message_items: set[str] = set()
        streamed_message_outputs: set[int] = set()
        yielded_function_calls: set[str] = set()
        queued_function_calls: set[str] = set()
        pending_function_args: dict[str, str] = {}

        if not self._stream_responses:
            completed_response = cast("Response", response_or_stream)
            for item in completed_response.output:
                if item.type == "message":
                    for content in item.content:
                        if content.type == "output_text":
                            text = content.text
                            if text:
                                response_text += text
                                yield TextChunk(text=text)
                elif item.type == "function_call":
                    # ``ResponseFunctionToolCall.arguments`` is a
                    # required ``str``; ``call_id`` is required too
                    # and ``item.id`` is only used as a secondary
                    # fallback for malformed payloads.
                    args_text: str | None = item.arguments
                    call_id = item.call_id or item.id
                    if call_id:
                        queued_function_calls.add(call_id)
                        state.pending_function_calls.append(
                            {
                                "call_id": call_id,
                                "name": item.name,
                            }
                        )
                    try:
                        args = json.loads(args_text) if args_text else {}
                    except (TypeError, json.JSONDecodeError):
                        args = {"raw": args_text}
                    yield ToolCallRequest(
                        name=item.name,
                        args=args,
                        metadata={"call_id": call_id} if call_id else {},
                    )
        else:
            stream = cast("Stream[ResponseStreamEvent]", response_or_stream)
            try:
                async for raw_event in iterate_blocking_stream(stream):
                    if state.interrupt_requested:
                        return
                    event = cast("ResponseStreamEvent", raw_event)

                    if event.type == "response.output_text.delta":
                        text = event.delta
                        if text:
                            response_text += text
                            streamed_message_items.add(event.item_id)
                            streamed_message_outputs.add(event.output_index)
                            yield TextChunk(text=text)

                    elif event.type == "response.function_call_arguments.done":
                        item_id = event.item_id
                        # ``event.arguments`` is typed ``str`` by the
                        # SDK so no coercion is needed.
                        pending_function_args[item_id] = event.arguments

                        name = event.name
                        if name and item_id and item_id not in yielded_function_calls:
                            yielded_function_calls.add(item_id)
                            args_text = pending_function_args.get(item_id)
                            try:
                                args = json.loads(args_text) if args_text else {}
                            except (TypeError, json.JSONDecodeError):
                                args = {"raw": args_text}
                            yield ToolCallRequest(
                                name=name,
                                args=args,
                                metadata={"item_id": item_id},
                            )

                    elif event.type == "response.output_item.done":
                        done_item = event.item
                        # ``done_item.id`` is ``Optional[str]`` on
                        # ``ResponseFunctionToolCall``; skip items
                        # lacking an id rather than bucketing them
                        # under an empty-string key.
                        if done_item.id is None:
                            continue
                        item_id = done_item.id
                        output_index = event.output_index

                        if (
                            done_item.type == "message"
                            and item_id not in streamed_message_items
                            and output_index not in streamed_message_outputs
                        ):
                            for content in done_item.content:
                                if content.type == "output_text":
                                    text = content.text
                                    if text:
                                        response_text += text
                                        yield TextChunk(text=text)

                        elif done_item.type == "function_call":
                            args_text = done_item.arguments or pending_function_args.get(item_id)
                            call_id = done_item.call_id or item_id
                            if call_id and call_id not in queued_function_calls:
                                queued_function_calls.add(call_id)
                                state.pending_function_calls.append(
                                    {
                                        "call_id": call_id,
                                        "name": done_item.name,
                                    }
                                )
                            try:
                                args = json.loads(args_text) if args_text else {}
                            except (TypeError, json.JSONDecodeError):
                                args = {"raw": args_text}
                            emit_keys = {key for key in (item_id, call_id) if key}
                            if yielded_function_calls.isdisjoint(emit_keys):
                                yielded_function_calls.update(emit_keys)
                                yield ToolCallRequest(
                                    name=done_item.name,
                                    args=args,
                                    metadata={"call_id": call_id} if call_id else {},
                                )

                    elif event.type == "response.completed":
                        completed_response = event.response

                    elif event.type == "error":
                        yield ExecutorError(message=f"OpenAI Responses API error: {event.message}")
                        return
            except Exception as exc:  # noqa: BLE001 — stream boundary surfaces any error as ExecutorError
                if state.interrupt_requested:
                    return
                logger.error("OpenResponsesExecutor: streaming API call failed: %s", exc)
                yield ExecutorError(message=f"OpenAI Responses API error: {exc}")
                return
            finally:
                state.active_stream = None

        if state.interrupt_requested:
            return
        response = completed_response
        if response is None:
            yield ExecutorError(
                message="OpenAI Responses API error: stream completed without final response"
            )
            return

        if response.error is not None:
            yield ExecutorError(message=f"OpenAI Responses API error: {response.error.message}")
            return

        # ── LLM_RESPONSE policy evaluation ───────────────────────
        # Evaluate BEFORE any state mutations (previous_response_id,
        # conversation_items, pending_function_calls) so a DENY
        # leaves session state clean for the next turn. Without
        # this ordering, orphaned items in conversation_items and
        # stale entries in pending_function_calls would pollute the
        # next LLM call's prompt.
        has_function_calls = any(item.type == "function_call" for item in response.output)
        if _policy_eval is not None:
            _resp_text_preview = response_text
            if not _resp_text_preview:
                _resp_text_preview = _extract_response_text(response) or ""
            _fc_count = sum(1 for item in response.output if item.type == "function_call")
            _resp_data: dict[str, Any] = {
                "model": model,
                "text_preview": _resp_text_preview[:500],
                "tool_calls_count": _fc_count,
            }
            if hasattr(response, "usage") and response.usage is not None:
                _usage = response.usage
                _resp_data["usage"] = {
                    "input_tokens": getattr(_usage, "input_tokens", 0),
                    "output_tokens": getattr(_usage, "output_tokens", 0),
                    "total_tokens": getattr(_usage, "total_tokens", 0),
                }
            resp_verdict = await _policy_eval("PHASE_LLM_RESPONSE", _resp_data)
            if resp_verdict.action == "POLICY_ACTION_DENY":
                _deny_reason = resp_verdict.reason or "no reason given"
                yield ExecutorError(message=f"LLM response denied by policy: {_deny_reason}")
                return

        # ── State mutations (safe — policy already approved) ─────
        if response.id:
            state.previous_response_id = response.id

        if delta_input_items:
            state.conversation_items.extend(deepcopy(delta_input_items))
        response_output = _normalize_response_output_items(response.output)
        if response_output:
            state.conversation_items.extend(response_output)

        if has_function_calls:
            for item in response.output:
                if item.type != "function_call":
                    continue
                # ``call_id`` is required on ``ResponseFunctionToolCall``
                # but fall back to ``id`` for resilience against
                # provider drift; ``None`` means both are missing.
                call_id = item.call_id or item.id
                if not call_id or call_id in queued_function_calls:
                    continue
                queued_function_calls.add(call_id)
                state.pending_function_calls.append(
                    {
                        "call_id": call_id,
                        "name": item.name,
                    }
                )
            state.history_cursor = len(split_transient_tail(messages).persisted)
            return

        if not response_text:
            response_text = _extract_response_text(response)
            if response_text:
                yield TextChunk(text=response_text)

        incomplete = response.incomplete_details
        if incomplete and not response_text:
            reason = incomplete.reason or "unknown"
            yield ExecutorError(message=f"OpenAI response incomplete: {reason}")
            return

        state.history_cursor = len(split_transient_tail(messages).persisted) + 1
        yield TurnComplete(response=response_text)


@dataclass
class _ResponsesSessionState:
    previous_response_id: str | None = None
    history_cursor: int = 0
    pending_function_calls: deque[dict[str, str]] = field(default_factory=deque)
    conversation_items: list[ResponsesItem] = field(default_factory=list)
    active_stream: Stream[ResponseStreamEvent] | None = None
    interrupt_requested: bool = False

    def reset(self) -> None:
        self.previous_response_id = None
        self.history_cursor = 0
        self.pending_function_calls.clear()
        self.conversation_items = []
        self.active_stream = None
        self.interrupt_requested = False
