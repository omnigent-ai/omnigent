"""Layered conversation history compaction for LLM context management.

Compaction fires when the estimated prompt token count approaches the
model's context window. Three layers are applied in order, from
least-lossy to most-lossy:

1. Surgical clearing — tool result bodies and binary content blocks
   outside the recent window are replaced with markers.
2. LLM summarization — a @step LLM call summarises all messages
   outside the recent window into a single summary pair.
3. Truncation — oldest messages are dropped when layers 1+2
   are still insufficient (emergency fallback).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import tiktoken

from omnigent.entities import (
    CompactionData,
    ConversationItem,
    MessageData,
)
from omnigent.llms.summarize import (
    build_summarization_input,
    build_summarization_prompt,
    extract_summary_text,
)
from omnigent.spec.types import CompactionConfig

_logger = logging.getLogger(__name__)

# Marker written into cleared tool result bodies.
_TOOL_RESULT_CLEARED = "[Previous tool result cleared — re-call tool if needed]"

# Marker written into cleared binary content block payloads.
_BINARY_CONTENT_CLEARED = (
    "[binary content removed for context management — use file_id to retrieve]"
)

# Default compaction settings when AgentSpec.compaction is None.
_DEFAULT_TRIGGER_THRESHOLD: float = 0.8
_DEFAULT_RECENT_WINDOW: int = 5


@dataclass
class SummaryMetadata:
    """
    Metadata from a Layer 2 summarization, passed from
    :func:`compact` to the workflow's end-of-execution
    persistence step.

    :param text: The LLM-generated summary text.
    :param last_item_id: The ID of the last conversation item
        covered by this summary, e.g. ``"msg_abc123"``.
    :param model: The model used for summarization, e.g.
        ``"openai/gpt-4o"``.
    :param token_count: Approximate token count of the summary
        text, e.g. ``342``.
    """

    text: str
    last_item_id: str
    model: str
    token_count: int


@dataclass
class CompactionResult:
    """
    Result of running :func:`compact` on a messages list.

    :param messages: The compacted messages list, ready to pass
        to the LLM.
    :param summary_metadata: Present only when Layer 2
        (summarization) was triggered. Contains the summary text
        and the ``last_item_id`` of the last item covered.
        ``None`` when only Layer 1 or Layer 3 applied, or when
        summarization failed and Layer 3 was used as fallback.
    :param total_tokens: Tiktoken estimate of the post-compaction
        token count for ``messages``, e.g. ``8421``. Populated by
        :func:`compact` using the count it already computed during
        budget checks — no additional tokenization pass needed.
        ``None`` when the count is unavailable (e.g. early-return
        paths that skip counting).
    """

    messages: list[dict[str, Any]]
    summary_metadata: SummaryMetadata | None
    total_tokens: int | None = None


@dataclass
class _CompactionState:
    """
    Per-execution compaction state maintained in the agent loop.

    :param context_window: Cached context window size discovered
        from the first ContextWindowExceededError,
        e.g. ``128000``. ``None`` until the first overflow occurs.
    :param last_summary: Metadata from the most recent Layer 2
        summarization during this execution, or ``None`` if no
        summarization has occurred yet.
    :param config: The compaction config from the agent spec, or
        ``None`` to use defaults.
    :param model: The LLM model string used for tiktoken estimation,
        e.g. ``"openai/gpt-4o"``.
    :param connection: Per-provider connection overrides (api_key,
        base_url, etc.) for the summarization LLM call. ``None``
        means use environment variable defaults.
    :param conversation_id: Conversation id, e.g.
        ``"conv_0123456789abcdef"``. Used to look up the runner
        client from the router so Layer 2 summarization runs through
        the runner's credentials rather than the Omnigent server's.
    """

    context_window: int | None
    last_summary: SummaryMetadata | None
    config: CompactionConfig | None
    model: str
    connection: dict[str, str] | None = None
    conversation_id: str | None = None
    post_compaction_tokens: int | None = None
    history_len_at_compaction: int | None = None


def count_tokens(messages: list[dict[str, Any]], model: str) -> int:
    """
    Estimate the token count for a messages list using tiktoken.

    Used as a sanity check against provider-reported token counts
    (within ~30%) and for proactive threshold checks. Not used as
    the authoritative count — tiktoken is ~85-95% accurate for
    non-OpenAI models, and the 20% headroom from
    ``trigger_threshold`` absorbs the difference.

    :param messages: The messages list to count tokens for.
    :param model: The LLM model string, e.g. ``"openai/gpt-4o"``.
        Used to select the appropriate tiktoken encoding; falls
        back to ``cl100k_base`` for unknown models.
    :returns: Approximate token count for the serialised messages.
    """
    # Strip provider prefix (e.g. "openai/gpt-4o" -> "gpt-4o")
    # so tiktoken can look up the model encoding.
    bare_model = model.split("/", 1)[-1] if "/" in model else model
    try:
        enc = tiktoken.encoding_for_model(bare_model)
    except KeyError:
        # Unknown model — fall back to the most common encoding.
        enc = tiktoken.get_encoding("cl100k_base")
    text = json.dumps(messages, ensure_ascii=False)
    return len(enc.encode(text))


def _find_recent_boundary(
    history: list[ConversationItem],
    recent_window: int,
) -> int:
    """
    Find the index in *history* where the recent window begins.

    The recent window covers the last *recent_window* LLM response
    groups. One group = one assistant message or one function_call
    item (both mark an LLM response boundary). Items at or after
    the returned index are protected from compaction.

    :param history: The full conversation history list.
    :param recent_window: Number of LLM response groups to protect,
        e.g. ``5``.
    :returns: The index of the first item inside the recent window.
        Returns ``0`` if the history has fewer groups than the window
        size (protect everything).
    """
    if recent_window <= 0:
        return len(history)
    groups_seen = 0
    for i in range(len(history) - 1, -1, -1):
        item = history[i]
        is_assistant_msg = (
            item.type == "message"
            and isinstance(item.data, MessageData)
            and item.data.role == "assistant"
        )
        is_function_call = item.type == "function_call"
        if is_assistant_msg or is_function_call:
            groups_seen += 1
            if groups_seen >= recent_window:
                return i
    return 0


def _clear_tool_results(
    messages: list[dict[str, Any]],
    protect_from: int,
) -> list[dict[str, Any]]:
    """
    Replace tool result bodies outside the recent window with a
    clearing marker.

    The function_call / function_call_output pair structure is
    preserved — no orphaned tool calls are created. Only the
    ``output`` field of ``function_call_output`` items is
    replaced.

    :param messages: The messages list to process (modified in place).
    :param protect_from: Index of the first message in the recent
        window. Messages at indices < *protect_from* are eligible
        for clearing.
    :returns: The same list (modified in place) for convenience.
    """
    for i, msg in enumerate(messages):
        if i >= protect_from:
            break
        if msg.get("type") == "function_call_output":
            msg["output"] = _TOOL_RESULT_CLEARED
    return messages


def _clear_binary_content(
    messages: list[dict[str, Any]],
    protect_from: int,
) -> list[dict[str, Any]]:
    """
    Replace binary payload data in image/file content blocks
    outside the recent window with a clearing marker.

    The ``file_id`` is preserved so the agent can re-fetch the
    content if needed. Text content blocks within the same message
    are untouched.

    :param messages: The messages list to process (modified in place).
    :param protect_from: Index of the first message in the recent
        window. Messages at indices < *protect_from* are eligible
        for clearing.
    :returns: The same list (modified in place) for convenience.
    """
    for i, msg in enumerate(messages):
        if i >= protect_from:
            break
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") in ("image", "file")
                and "data" in block
            ):
                block["data"] = _BINARY_CONTENT_CLEARED
    return messages


def _strip_output_annotations(
    messages: list[dict[str, Any]],
    protect_from: int,
) -> list[dict[str, Any]]:
    """
    Remove ``annotations`` from ``output_text`` blocks outside
    the recent window.

    Annotations (e.g. ``file_citation``) are output metadata for
    the client, not content the summarization LLM should see.
    Stripping them before Layer 2 keeps the summarization input
    clean and reduces token waste.

    :param messages: The messages list to process (modified in place).
    :param protect_from: Index of the first message in the recent
        window. Messages at indices < *protect_from* are eligible
        for stripping.
    :returns: The same list (modified in place) for convenience.
    """
    for i, msg in enumerate(messages):
        if i >= protect_from:
            break
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "output_text"
                and "annotations" in block
            ):
                del block["annotations"]
    return messages


def _truncate_oldest(
    messages: list[dict[str, Any]],
    budget: int,
    model: str,
) -> list[dict[str, Any]]:
    """
    Emergency Layer 3: drop oldest messages until the token count
    fits within *budget*.

    Preserves tool call pair integrity — never drops a
    ``function_call`` without also dropping its matching
    ``function_call_output``, and vice versa. Drops from the front
    of the list.

    :param messages: The messages list to truncate.
    :param budget: Maximum token count for the returned list,
        e.g. ``102400``.
    :param model: LLM model string for token counting.
    :returns: A new messages list with oldest items dropped.
    """
    result = list(messages)
    while result and count_tokens(result, model) > budget:
        drop_count = _pair_aware_drop_count(result)
        if drop_count == 0:
            break
        result = result[drop_count:]
    return result


def _pair_aware_drop_count(messages: list[dict[str, Any]]) -> int:
    """
    Return how many items to drop from the front to avoid
    orphaning a tool call pair.

    If the first item is a ``function_call`` and the second is its
    matching ``function_call_output``, both are dropped together.
    Otherwise, a single item is dropped.

    :param messages: The messages list (must be non-empty).
    :returns: Number of items to drop (1 or 2), or 0 if the list
        is empty.
    """
    if not messages:
        return 0
    if (
        len(messages) >= 2
        and messages[0].get("type") == "function_call"
        and messages[1].get("type") == "function_call_output"
        and messages[0].get("call_id") == messages[1].get("call_id")
    ):
        return 2
    return 1


async def summarize_history(
    messages_to_summarize: list[dict[str, Any]],
    llm_client: Any,  # llms.Client — typed as Any to avoid circular import
    model: str,
    connection: dict[str, str] | None = None,
    runner_client: Any | None = None,  # httpx.AsyncClient | None
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """
    Layer 2: call the LLM to summarise conversation messages.

    When *runner_client* is provided the summarization LLM call is
    delegated to the runner's ``POST /v1/summarize`` endpoint so the
    runner's credentials are used instead of the Omnigent server's. Falls
    back to *llm_client* when no runner client is configured.

    :param messages_to_summarize: The messages outside the recent
        window to summarise, as Responses API input dicts. By the
        time this is called, Layer 1 has already cleared binary
        content blocks and tool result bodies from these messages.
    :param llm_client: The LLM client to use, e.g. an instance of
        ``llms.Client``. Ignored when *runner_client* is set.
    :param model: The LLM model string, e.g. ``"openai/gpt-4o"``.
    :param connection: Per-provider connection overrides (api_key,
        base_url, etc.) from the agent spec. ``None`` means use
        environment variable defaults.
    :param runner_client: Optional ``httpx.AsyncClient`` pointed at the
        runner. When set, the summarization LLM call is delegated to
        the runner's ``POST /v1/summarize`` endpoint. ``None`` falls
        back to *llm_client*.
    :param conversation_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``. Forwarded to the runner so it can look up
        the spec's auth credentials for the LLM call.
    :returns: A dict with ``"text"`` (the summary) and
        ``"token_count"`` (approximate token count).
    """
    if runner_client is not None:
        return await _summarize_via_runner_uncached(
            runner_client,
            messages_to_summarize,
            model,
            connection,
            conversation_id=conversation_id,
        )
    return await _summarize_history_uncached(
        messages_to_summarize,
        llm_client,
        model,
        connection,
    )


async def _summarize_history_uncached(
    messages_to_summarize: list[dict[str, Any]],
    llm_client: Any,
    model: str,
    connection: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Run the Layer 2 summarization LLM call.

    :param messages_to_summarize: Messages to summarize.
    :param llm_client: LLM client instance, e.g. ``llms.Client()``.
    :param model: LLM model string, e.g. ``"openai/gpt-4o"``.
    :param connection: Per-provider connection overrides. ``None``
        uses environment variable defaults.
    :returns: Dict with ``"text"`` and ``"token_count"`` keys.
    """
    system_prompt = build_summarization_prompt(messages_to_summarize)
    resp = await llm_client.responses.create(
        model=model,
        input=build_summarization_input(messages_to_summarize),
        instructions=system_prompt,
        tools=[],
        connection_params=connection,
    )
    summary_text = extract_summary_text(resp)
    token_count = count_tokens([{"role": "assistant", "content": summary_text}], model)
    return {"text": summary_text, "token_count": token_count}


async def _summarize_via_runner_uncached(
    runner_client: Any,  # httpx.AsyncClient
    messages_to_summarize: list[dict[str, Any]],
    model: str,
    connection: dict[str, str] | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """
    POST to the runner's ``/v1/summarize`` endpoint and return the result.

    The runner creates its own LLM client using its local credentials,
    so the Omnigent server needs no LLM auth for this call.

    :param runner_client: ``httpx.AsyncClient`` pointed at the runner.
    :param messages_to_summarize: Messages to summarize.
    :param model: LLM model string, e.g. ``"openai/gpt-4o"``.
    :param connection: Per-provider connection overrides forwarded to
        the runner verbatim. ``None`` omits the field.
    :param conversation_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``. Sent in the payload so the runner can
        look up the spec's auth credentials for the LLM call.
    :returns: Dict with ``"text"`` (summary) and ``"token_count"``
        (approximate tiktoken estimate) keys.
    :raises httpx.HTTPStatusError: On non-2xx responses from the runner.
    """
    payload: dict[str, Any] = {"messages": messages_to_summarize, "model": model}
    if connection:
        payload["connection"] = connection
    if conversation_id is not None:
        payload["session_id"] = conversation_id
    resp = await runner_client.post("/v1/summarize", json=payload, timeout=120.0)
    resp.raise_for_status()
    return resp.json()


def compaction_to_history_items(
    compaction_item: ConversationItem,
) -> list[ConversationItem]:
    """
    Convert a compaction item into a synthetic user + assistant
    message pair for inclusion at the front of conversation history.

    The pair preserves natural turn-taking structure: a synthetic
    user message requests a summary, and a synthetic assistant
    message provides it. This avoids attribution confusion —
    the LLM knows it produced a summary (not a real prior response).

    The synthetic items are NOT persisted to the conversation store;
    they exist only in the in-memory history list for prompt
    construction.

    :param compaction_item: The compaction item from the store,
        with ``type="compaction"`` and
        ``data`` of type :class:`~omnigent.entities.CompactionData`.
    :returns: Two :class:`~omnigent.entities.ConversationItem`
        instances: a ``role=user`` message requesting the summary
        and a ``role=assistant`` message containing it.
    """
    assert isinstance(compaction_item.data, CompactionData)
    data = compaction_item.data

    # Prefer compacted_messages when available — they carry the
    # full compacted state (e.g. OpenAI's opaque compaction tokens
    # or Claude's post-compaction transcript) that the harness can
    # replay directly. Fall back to the synthetic summary pair for
    # older compaction items that don't have compacted messages.
    if data.compacted_messages:
        items: list[ConversationItem] = []
        for i, msg in enumerate(data.compacted_messages):
            items.append(
                ConversationItem(
                    id=f"{compaction_item.id}_compacted_{i}",
                    type=msg.get("type", "message"),
                    status="completed",
                    response_id=compaction_item.response_id,
                    created_at=compaction_item.created_at,
                    data=MessageData(
                        role=msg.get("role", "user"),
                        content=msg.get("content", []),
                    ),
                )
            )
        return items

    synthetic_user_content = (
        "[This is an automatically generated summary of the prior conversation "
        "context. The original messages are available but not included in this "
        "prompt for brevity.]\n\n"
        "Please provide a summary of our conversation so far."
    )
    user_item = ConversationItem(
        id=f"{compaction_item.id}_user",
        type="message",
        status="completed",
        response_id=compaction_item.response_id,
        created_at=compaction_item.created_at,
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": synthetic_user_content}],
        ),
    )
    assistant_item = ConversationItem(
        id=f"{compaction_item.id}_assistant",
        type="message",
        status="completed",
        response_id=compaction_item.response_id,
        created_at=compaction_item.created_at,
        data=MessageData(
            role="assistant",
            content=[{"type": "output_text", "text": data.summary}],
            agent=data.model,
        ),
    )
    return [user_item, assistant_item]


async def compact(
    messages: list[dict[str, Any]],
    history: list[ConversationItem],
    *,
    config: CompactionConfig | None,
    context_window: int,
    system_token_budget: int,
    model: str,
    task_id: str,
    llm_client: Any,  # llms.Client — typed as Any to avoid circular import
    connection: dict[str, str] | None = None,
    runner_client: Any | None = None,  # httpx.AsyncClient | None
    force: bool = False,
    fail_on_summary_error: bool = False,
    conversation_id: str | None = None,
) -> CompactionResult:
    """
    Apply layered compaction to a messages list to fit within the
    context window budget.

    Layers are applied in order from least-lossy to most-lossy:

    1. **Layer 1** — Clear tool result bodies and binary content
       blocks outside the recent window (fast, no LLM call).
    2. **Layer 2** — LLM summarization of messages outside the
       recent window (slow).
    3. **Layer 3** — Truncate oldest messages (emergency fallback).

    The in-memory *history* list is never modified — only the
    *messages* copy passed to the LLM is compacted.

    :param messages: The messages list to compact. This is a copy
        — the original history is not modified.
    :param history: The original conversation history items, used
        to find ``last_item_id`` for the summary.
    :param config: Compaction configuration from the agent spec.
        ``None`` uses defaults.
    :param context_window: The model's context window size in tokens,
        e.g. ``128000``.
    :param system_token_budget: Tokens already consumed by the system
        prompt and tool schemas, subtracted from the window budget.
    :param model: The LLM model string, e.g. ``"openai/gpt-4o"``.
    :param task_id: The task identifier for SSE event emission.
    :param llm_client: The LLM client instance for Layer 2
        summarization. Ignored when *runner_client* is set.
    :param connection: Per-provider connection overrides (api_key,
        base_url, etc.) passed through to the summarization call.
    :param runner_client: Optional ``httpx.AsyncClient`` pointed at
        the runner. When set, Layer 2 summarization is delegated to
        the runner's ``POST /v1/summarize`` endpoint so the runner's
        credentials are used instead of the Omnigent server's. ``None``
        falls back to *llm_client*.
    :param force: When ``True``, run through Layer 2 even if Layer 1
        already fits within the budget. Used by explicit user-initiated
        compaction (``/compact``) so a summary item is persisted even
        before the automatic threshold is crossed.
    :param fail_on_summary_error: When ``True``, propagate Layer 2
        summarization failures instead of silently falling back to
        Layer 3. Explicit ``/compact`` needs this so it never reports
        success without a durable summary item.
    :param conversation_id: When set, ``response.compaction.in_progress``
        and ``response.compaction.completed`` are published to the session
        stream so the REPL and web UI show the compaction indicator.
        ``None`` for explicit ``/compact`` — sessions.py handles those
        events directly, e.g. ``"conv_abc123"``.
    :returns: A :class:`CompactionResult` with the compacted messages
        and optional summary metadata.
    """
    trigger_threshold = config.trigger_threshold if config else _DEFAULT_TRIGGER_THRESHOLD
    recent_window = config.recent_window if config else _DEFAULT_RECENT_WINDOW
    # Budget = fraction of context window minus system/tool tokens.
    budget = int(context_window * trigger_threshold) - system_token_budget

    # Deep-copy messages so Layer 1 modifications don't affect the
    # caller's list.
    working = _deep_copy_messages(messages)

    history_boundary = _find_recent_boundary(history, recent_window)
    msg_boundary = _history_idx_to_msg_idx(history, history_boundary)

    # --- Layer 1 ---
    _clear_tool_results(working, msg_boundary)
    _clear_binary_content(working, msg_boundary)
    _strip_output_annotations(working, msg_boundary)

    l1_tokens = count_tokens(working, model)
    if not force and l1_tokens <= budget:
        return CompactionResult(messages=working, summary_metadata=None, total_tokens=l1_tokens)

    # --- Layer 2 ---
    _logger.debug(
        "Compaction Layer 2 summarization starting for task %s: "
        "%d tokens after Layer 1 clearing, budget %d%s",
        task_id,
        l1_tokens,
        budget,
        " (forced)" if force else "",
    )
    if conversation_id:
        # Publish to session stream so REPL/web UI shows "Compacting…" indicator.
        # Imported locally to avoid circular imports at module level.
        from omnigent.runtime import session_stream as _session_stream

        _session_stream.publish(
            conversation_id,
            {"type": "response.compaction.in_progress", "task_id": task_id},
        )
    summary_metadata = await _run_layer2(
        working,
        history,
        history_boundary,
        msg_boundary,
        budget,
        model,
        task_id,
        llm_client,
        connection=connection,
        fail_on_error=fail_on_summary_error,
        runner_client=runner_client,
        conversation_id=conversation_id,
    )
    if summary_metadata is not None:
        summary_messages = _summary_to_messages(summary_metadata)
        recent_messages = working[msg_boundary:]
        compacted = summary_messages + recent_messages
        compacted_tokens = count_tokens(compacted, model)
        if compacted_tokens <= budget:
            _logger.debug(
                "Compaction Layer 2 complete for task %s: %d tokens (budget %d)",
                task_id,
                compacted_tokens,
                budget,
            )
            if conversation_id:
                from omnigent.runtime import session_stream as _session_stream

                _session_stream.publish(
                    conversation_id,
                    {
                        "type": "response.compaction.completed",
                        "task_id": task_id,
                        "total_tokens": compacted_tokens,
                    },
                )
            return CompactionResult(
                messages=compacted,
                summary_metadata=summary_metadata,
                total_tokens=compacted_tokens,
            )
        # Summary + recent still exceeds budget — fall through to Layer 3.
        working = compacted

    # --- Layer 3 ---
    _logger.warning(
        "Layer 3 truncation triggered for task %s — context still exceeds budget after layers 1+2",
        task_id,
    )
    truncated = _truncate_oldest(working, budget, model)
    l3_tokens = count_tokens(truncated, model)
    # Emit completed on the Layer 3 path so "Compacting…" spinners
    # resolve even when Layer 2 failed or its output exceeded budget.
    if conversation_id:
        from omnigent.runtime import session_stream as _session_stream

        _session_stream.publish(
            conversation_id,
            {
                "type": "response.compaction.completed",
                "task_id": task_id,
                "total_tokens": l3_tokens,
            },
        )
    return CompactionResult(
        messages=truncated,
        summary_metadata=summary_metadata,
        total_tokens=l3_tokens,
    )


def _deep_copy_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Return a deep copy of the messages list so Layer 1 clearing
    does not mutate the caller's list.

    :param messages: The messages list to copy.
    :returns: A deep copy.
    """
    result: list[dict[str, Any]] = json.loads(json.dumps(messages))
    return result


def _history_idx_to_msg_idx(
    history: list[ConversationItem],
    history_idx: int,
) -> int:
    """
    Map a history index to the corresponding messages list index.

    ``history_to_input_items`` skips reasoning items, so the
    messages list may be shorter than the history list. This
    function counts non-reasoning items up to *history_idx*.

    :param history: The full conversation history.
    :param history_idx: The index in *history* to map.
    :returns: The corresponding index in the messages list.
    """
    msg_idx = 0
    for i, item in enumerate(history):
        if i >= history_idx:
            break
        if item.type != "reasoning":
            msg_idx += 1
    return msg_idx


def _is_summary_auth_error(exc: BaseException) -> bool:
    """Return ``True`` when *exc* is (or wraps) an HTTP 401/403.

    Layer 2 summarization calls an LLM *outside* the harness, so a missing or
    invalid summarizer credential surfaces here as an auth error. Detecting it
    lets the caller surface a distinct, actionable message instead of burying a
    persistent misconfiguration behind a routine Layer-3 truncation fallback
    (issue #1121).

    :param exc: The exception raised by the summarization call.
    :returns: ``True`` for a 401/403 (by ``response.status_code`` or message).
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return True
    text = str(exc)
    return any(token in text for token in ("401", "403", "Unauthorized", "Forbidden"))


async def _run_layer2(
    messages: list[dict[str, Any]],
    history: list[ConversationItem],
    history_boundary: int,
    msg_boundary: int,
    budget: int,
    model: str,
    task_id: str,
    llm_client: Any,
    connection: dict[str, str] | None = None,
    fail_on_error: bool = False,
    runner_client: Any | None = None,  # httpx.AsyncClient | None
    conversation_id: str | None = None,
) -> SummaryMetadata | None:
    """
    Attempt Layer 2 LLM summarisation.

    Emits a ``response.compaction.in_progress`` SSE event before
    the LLM call. Returns ``None`` and falls through to Layer 3 if
    the LLM call fails.

    :param messages: The working messages list (after Layer 1).
    :param history: The original conversation history items.
    :param history_boundary: The boundary index in *history*.
    :param msg_boundary: The boundary index in *messages*.
    :param budget: Token budget for the compacted result.
    :param model: LLM model string.
    :param task_id: Task identifier for SSE event emission.
    :param llm_client: LLM client instance. Ignored when
        *runner_client* is set.
    :param connection: Per-provider connection overrides (api_key,
        base_url, etc.) passed through to the summarization call.
    :param fail_on_error: When ``True``, re-raise summarization errors
        after logging them.
    :param runner_client: Optional ``httpx.AsyncClient`` pointed at the
        runner. When set, delegates the summarization LLM call to the
        runner's ``POST /v1/summarize`` endpoint. ``None`` uses
        *llm_client* directly.
    :param conversation_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``. Forwarded to the runner so it can look up
        the spec's auth credentials for the LLM call.
    :returns: :class:`SummaryMetadata` on success, ``None`` on failure.
    """

    to_summarize = messages[:msg_boundary]
    # If too large for the model, apply Layer 1 clearing to the
    # summarization input too.
    if count_tokens(to_summarize, model) > budget:
        to_summarize = _deep_copy_messages(to_summarize)
        _clear_tool_results(to_summarize, len(to_summarize))
        _clear_binary_content(to_summarize, len(to_summarize))

    try:
        summarize_kwargs: dict[str, Any] = {"connection": connection}
        if runner_client is not None:
            summarize_kwargs["runner_client"] = runner_client
        if conversation_id is not None:
            summarize_kwargs["conversation_id"] = conversation_id
        result = await summarize_history(
            to_summarize,
            llm_client,
            model,
            **summarize_kwargs,
        )
    except Exception as exc:
        if _is_summary_auth_error(exc):
            # Distinct, actionable signal: an auth/config problem (not a
            # transient blip) is silently degrading compaction to lossy
            # truncation. Don't bury a 401 as a routine fallback (issue #1121).
            _logger.error(
                "Compaction Layer 2 summarisation is UNAUTHORIZED for task %s "
                "(%s) — the summarizer's credentials are missing or invalid, so "
                "compaction is degrading to lossy Layer-3 truncation. Fix the "
                "summarizer auth/config to restore summary-quality compaction.",
                task_id,
                exc,
            )
        else:
            _logger.warning(
                "Layer 2 summarisation failed for task %s — falling back to Layer 3",
                task_id,
                exc_info=True,
            )
        if fail_on_error:
            raise
        return None

    last_item_id = _find_last_summarized_item_id(history, history_boundary)
    if last_item_id is None:
        return None

    return SummaryMetadata(
        text=result["text"],
        last_item_id=last_item_id,
        model=model,
        token_count=result["token_count"],
    )


def _find_last_summarized_item_id(
    history: list[ConversationItem],
    history_boundary: int,
) -> str | None:
    """
    Find the ID of the last history item included in the summary.

    This is the item at ``history[history_boundary - 1]``, skipping
    any synthetic items (those without a real store ID). Synthetic
    items are identified by the ``_user`` or ``_assistant`` suffix
    added by :func:`compaction_to_history_items`.

    :param history: The conversation history items.
    :param history_boundary: The boundary index (exclusive).
    :returns: The last real item ID, or ``None`` if no real items
        exist before the boundary.
    """
    for i in range(history_boundary - 1, -1, -1):
        item = history[i]
        # Skip synthetic items: IDs with _user / _assistant suffix
        # (from compaction_to_history_items) and synthetic_N IDs
        # (from the runner's in-memory ConversationItem construction).
        if not item.id.endswith(("_user", "_assistant")) and not item.id.startswith("synthetic_"):
            return item.id
    return None


def _summary_to_messages(
    summary: SummaryMetadata,
) -> list[dict[str, Any]]:
    """
    Convert a :class:`SummaryMetadata` into the synthetic
    user + assistant message pair for inclusion in the prompt.

    :param summary: The summary metadata from Layer 2.
    :returns: Two message dicts: user request and assistant summary.
    """
    user_text = (
        "[This is an automatically generated summary of the prior conversation "
        "context. The original messages are available but not included in this "
        "prompt for brevity.]\n\n"
        "Please provide a summary of our conversation so far."
    )
    return [
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": summary.text},
    ]
