"""Runner-side session history: replay stored items as harness input,
compaction, and cancellation markers."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable, Coroutine
from typing import Any, Protocol, cast

import httpx

from omnigent.debug_logging import runner_primary_session_id
from omnigent.runner.app_support import (
    _resolve_forwarded_message_content,
    _SpecEntry,
)
from omnigent.runner.native import _unwrap_resolved_spec
from omnigent.util.json_types import JsonObject as _JsonObject

_logger = logging.getLogger("omnigent.runner.app")


def pending_user_item_ids(items: list[_JsonObject]) -> list[str]:
    """Server item ids of the unanswered user messages at the tail of ``items``.

    A reconnect recovery turn answers every user message that trails the last
    completed turn at once, so dedup must key on all of them, not just the last
    — otherwise a repeated forward of an earlier pending message reads as new
    input and runs a second time. Non-input items the history conversion drops
    (e.g. reasoning) are skipped; an assistant reply, tool call, tool output,
    error, or compaction ends the pending run.
    """
    ids: list[str] = []
    for item in reversed(items):
        item_type = item.get("type")
        if item_type == "message" and item.get("role") == "user":
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id:
                ids.append(item_id)
            continue
        if item_type in (
            "message",
            "function_call",
            "function_call_output",
            "error",
            "compaction",
        ):
            break
    ids.reverse()
    return ids


class _LoadHistoryAsInputFn(Protocol):
    async def __call__(
        self, session_id: str, drop_item_id: str | None = None
    ) -> list[_JsonObject]: ...


@dataclasses.dataclass(frozen=True)
class SessionHistory:
    """Session-history helpers the rest of the runner app calls."""

    append_cancellation_items: Callable[[str], None]
    convert_raw_items_to_input: Callable[[list[_JsonObject]], list[_JsonObject]]
    extract_last_assistant_text: Callable[[str], str]
    handle_harness_compaction: Callable[[str, _JsonObject], Coroutine[Any, Any, None]]
    load_history_as_input: _LoadHistoryAsInputFn
    seed_last_server_item_id: Callable[[str], Coroutine[Any, Any, None]]


def build_session_history(
    *,
    _background_tasks: set[asyncio.Task[Any]],
    _last_server_item_id: dict[str, str],
    _last_server_user_item_id: dict[str, list[str]],
    _persist_cancellation_items: Callable[[str, list[_JsonObject]], Coroutine[Any, Any, None]],
    _session_histories: dict[str, list[_JsonObject]],
    _session_spec_cache: dict[str, _SpecEntry | None],
    server_client: httpx.AsyncClient,
) -> SessionHistory:
    """Build the session-history helpers over the runner app's session state.

    The keyword arguments are the runner app's shared session state and helpers.
    """

    async def _seed_last_server_item_id(session_id: str) -> None:
        """
        Record the newest server item ID without loading history.

        Native-harness sessions never call ``_load_history_as_input``
        (their transcripts are mirrored from the underlying runtime), but
        harness compaction persistence still needs the latest server item
        ID as its anchor — fetch just that ID.

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        """
        try:
            resp = await server_client.get(
                f"/v1/sessions/{session_id}/items",
                params={"limit": "1", "order": "desc"},
                timeout=10.0,
            )
            if resp.status_code != 200:
                _logger.warning(
                    "Last-item seed returned %d for session=%s",
                    resp.status_code,
                    session_id,
                    extra={"session_id": session_id},
                )
                return
            page_items = resp.json().get("data", [])
        except (httpx.HTTPError, ValueError):
            _logger.warning(
                "Last-item seed failed for session=%s",
                session_id,
                exc_info=True,
                extra={"session_id": session_id},
            )
            return
        last_id = page_items[0].get("id") if page_items else None
        if last_id:
            _last_server_item_id[session_id] = last_id

    async def _load_history_as_input(
        session_id: str,
        drop_item_id: str | None = None,
    ) -> list[_JsonObject]:
        all_items: list[_JsonObject] = []
        after_cursor: str | None = None
        while True:
            params: dict[str, str] = {
                "limit": "100",
                "order": "asc",
            }
            if after_cursor is not None:
                params["after"] = after_cursor
            try:
                resp = await server_client.get(
                    f"/v1/sessions/{session_id}/items",
                    params=params,
                    timeout=10.0,
                )
                if resp.status_code != 200:
                    _logger.warning(
                        "History load returned %d for session=%s",
                        resp.status_code,
                        session_id,
                        extra={"session_id": session_id},
                    )
                    break
            except httpx.HTTPError:
                _logger.warning(
                    "History load failed for session=%s",
                    session_id,
                    exc_info=True,
                    extra={"session_id": session_id},
                )
                break
            page = resp.json()
            page_items = page.get("data", [])
            if not page_items:
                break
            all_items.extend(page_items)
            last_id = page_items[-1].get("id")
            if last_id:
                _last_server_item_id[session_id] = last_id
            if not page.get("has_more", False):
                break
            after_cursor = last_id

        _pending_user_ids = pending_user_item_ids(all_items)
        if _pending_user_ids:
            _last_server_user_item_id[session_id] = _pending_user_ids
        else:
            _last_server_user_item_id.pop(session_id, None)

        if drop_item_id is not None:
            all_items = [it for it in all_items if it.get("id") != drop_item_id]

        converted = _convert_raw_items_to_input(all_items)
        # Items are persisted pre-resolution, so reloaded history can still
        # carry raw file_id blocks (the runner has no file/artifact stores).
        # Resolve them the same way current-turn intake does.
        for item in converted:
            content = item.get("content")
            if item.get("type") == "message" and isinstance(content, list):
                item["content"] = await _resolve_forwarded_message_content(
                    content,
                    session_id=session_id,
                    server_client=server_client,
                )
        return converted

    def _convert_raw_items_to_input(
        items: list[_JsonObject],
    ) -> list[_JsonObject]:
        compaction_idx: int | None = None
        for i, item in enumerate(items):
            if item.get("type") == "compaction":
                compaction_idx = i

        result: list[_JsonObject] = []
        if compaction_idx is not None:
            c = items[compaction_idx]
            _compacted = cast(list[_JsonObject] | None, c.get("compacted_messages"))
            if _compacted:
                result.extend(_compacted)
            else:
                result.append(
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "[Automatically generated summary of prior "
                                    "conversation context.]\n\n"
                                    "Please provide a summary of our conversation so far."
                                ),
                            }
                        ],
                    }
                )
                result.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": c.get("summary", ""),
                            }
                        ],
                    }
                )
            remaining = items[compaction_idx + 1 :]
        else:
            remaining = items

        _skipped_types: list[str] = []
        for item in remaining:
            item_type = item.get("type")
            if item_type not in (
                "message",
                "function_call",
                "function_call_output",
                "error",
            ):
                _skipped_types.append(str(item_type))
            if item_type == "message":
                result.append(
                    {
                        "type": "message",
                        "role": item.get("role", "user"),
                        "content": item.get("content", []),
                    }
                )
            elif item_type == "function_call":
                result.append(
                    {
                        "type": "function_call",
                        "call_id": item.get("call_id"),
                        "name": item.get("name"),
                        "arguments": item.get("arguments"),
                    }
                )
            elif item_type == "function_call_output":
                result.append(
                    {
                        "type": "function_call_output",
                        "call_id": item.get("call_id"),
                        "output": item.get("output"),
                    }
                )
            elif item_type == "error":
                error_message = item.get("message")
                code = item.get("code")
                source = item.get("source")
                result.append(
                    {
                        "type": "error",
                        "source": source if isinstance(source, str) and source else "execution",
                        "code": code if isinstance(code, str) and code else "error",
                        "message": (
                            error_message
                            if isinstance(error_message, str) and error_message
                            else "unknown error"
                        ),
                    }
                )
        if _skipped_types:
            _logger.warning(
                "_convert_raw_items_to_input: skipped %d items with types: %s",
                len(_skipped_types),
                _skipped_types,
                extra={"session_id": runner_primary_session_id()},
            )
        _logger.info(
            "_convert_raw_items_to_input: %d raw items → %d converted (compaction_idx=%s)",
            len(items),
            len(result),
            compaction_idx,
            extra={"session_id": runner_primary_session_id()},
        )
        return result

    def _extract_last_assistant_text(session_id: str) -> str:
        history = _session_histories.get(session_id, [])
        for item in reversed(history):
            if item.get("role") == "assistant":
                content = item.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict):
                            text = block.get("text") or block.get("input_text")
                            if text:
                                parts.append(str(text))
                        elif isinstance(block, str):
                            parts.append(block)
                    return "\n".join(parts) if parts else ""
        return ""

    async def _handle_harness_compaction(
        conv: str,
        event: _JsonObject,
    ) -> None:
        summary = cast(str, event.get("summary", ""))
        token_count = cast(int, event.get("total_tokens") or 0)
        model = cast(str | None, event.get("summary_model"))
        last_item_id = _last_server_item_id.get(conv)

        if not last_item_id:
            _logger.warning(
                "Skipping harness compaction persist for %s: no "
                "server-side last_item_id available",
                conv,
                extra={"session_id": conv},
            )
            return

        compacted_messages = cast(list[_JsonObject] | None, event.get("compacted_messages"))
        compaction_event: _JsonObject = {
            "type": "compaction",
            "summary": summary,
            "last_item_id": last_item_id,
            "model": model,
            "token_count": token_count,
        }
        if compacted_messages:
            compaction_event["compacted_messages"] = compacted_messages
        try:
            await server_client.post(
                f"/v1/sessions/{conv}/events",
                json={
                    "type": "compaction",
                    "data": compaction_event,
                },
                timeout=10.0,
            )
        except (httpx.HTTPError, RuntimeError):
            _logger.warning(
                "Failed to persist harness compaction item for %s",
                conv,
                exc_info=True,
                extra={"session_id": conv},
            )

        if compacted_messages:
            _session_histories[conv] = compacted_messages
        else:
            _session_histories[conv] = [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "[Automatically generated summary of prior "
                                "conversation context.]\n\n"
                                "Please provide a summary of our conversation so far."
                            ),
                        }
                    ],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": summary,
                        }
                    ],
                },
            ]

    _CANCELLATION_TOOL_OUTPUT = "[Cancelled — tool execution was interrupted.]"
    _CANCELLATION_MARKER_TEXT = (
        "[System: interrupted]\n"
        "The user interrupted and abandoned their previous request (the user "
        "message immediately before this one). Do not resume or act on that "
        "interrupted request unless the user asks for it again; treat the next "
        "user message as the current instruction. The preceding assistant "
        "message may be incomplete."
    )

    def _append_cancellation_items(conv_id: str) -> None:
        history = _session_histories.get(conv_id, [])

        call_ids_with_output: set[str] = set()
        dangling_calls: list[_JsonObject] = []
        for item in history:
            itype = item.get("type")
            if itype == "function_call":
                cid = item.get("call_id")
                if cid:
                    dangling_calls.append(item)
            elif itype == "function_call_output":
                cid = item.get("call_id")
                if cid:
                    call_ids_with_output.add(cast(str, cid))

        items_to_persist: list[_JsonObject] = []
        synthetic_items: list[_JsonObject] = []
        cached_spec_entry = _session_spec_cache.get(conv_id)
        cached_spec = _unwrap_resolved_spec(cached_spec_entry)
        agent_name = cached_spec.name if cached_spec else "unknown"
        for fc in dangling_calls:
            call_id = fc["call_id"]
            if call_id not in call_ids_with_output:
                fc_for_db = dict(fc)
                fc_for_db.setdefault("agent", agent_name)
                items_to_persist.append(fc_for_db)
                synthetic_output: _JsonObject = {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _CANCELLATION_TOOL_OUTPUT,
                }
                synthetic_items.append(synthetic_output)
                items_to_persist.append(synthetic_output)

        marker: _JsonObject = {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": _CANCELLATION_MARKER_TEXT,
                }
            ],
        }
        synthetic_items.append(marker)
        items_to_persist.append(marker)

        _session_histories.setdefault(conv_id, []).extend(synthetic_items)

        loop = asyncio.get_running_loop()
        _task = loop.create_task(
            _persist_cancellation_items(conv_id, items_to_persist),
            name=f"persist-cancel-{conv_id}",
        )
        _task.add_done_callback(_background_tasks.discard)
        _background_tasks.add(_task)

    return SessionHistory(
        append_cancellation_items=_append_cancellation_items,
        convert_raw_items_to_input=_convert_raw_items_to_input,
        extract_last_assistant_text=_extract_last_assistant_text,
        handle_harness_compaction=_handle_harness_compaction,
        load_history_as_input=_load_history_as_input,
        seed_last_server_item_id=_seed_last_server_item_id,
    )
