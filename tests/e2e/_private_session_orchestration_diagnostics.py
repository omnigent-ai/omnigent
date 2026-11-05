"""Content-free observations for one synthetic native worker's failed wait."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from omnigent.entities.conversation import parse_item_data
from omnigent.harnesses.codex_native.bridge import codex_home_for_bridge_dir
from omnigent.harnesses.diagnostics import sanitize_diagnostic_text

_MAX_BYTES = 8192
_MAX_ROLLOUT_BYTES = 4 * 1024 * 1024
_MAX_REQUESTS = 256
_MAX_MESSAGES = 8
_TOOLS = frozenset(
    {
        "mcp__omnigent",
        "sys_session_message",
        "sys_session_get_history",
        "sys_session_create",
        "sys_session_get_info",
    }
)
_CANNED = {
    "worker_result": "WORKER_RESULT",
    "manager_sent": "MANAGER_SENT",
    "generic_mock": "Mock LLM response",
    "policy_allow": '{"action":"allow","reason":""}',
    "native_auto_review_allow": (
        '{"risk_level":"low","user_authorization":"high",'
        '"outcome":"allow","rationale":"Isolated test"}'
    ),
}


def _digest(value: Any) -> str:
    if isinstance(value, str):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def _blocks(content: Any) -> tuple[dict[str, Any], list[str] | None]:
    texts = []
    kinds = []
    other = 0
    canonical = []
    supported = isinstance(content, list) and len(content) <= 64
    for block in content[:64] if isinstance(content, list) else []:
        if (
            isinstance(block, dict)
            and block.get("type") in {"text", "input_text", "output_text"}
            and isinstance(block.get("text"), str)
        ):
            texts.append(block["text"])
            kinds.append("text")
            canonical.append(["text", sanitize_diagnostic_text(block["text"])])
        else:
            kinds.append("other")
            canonical.append(["other", None])
            other += 1
            supported = False
    text = "".join(texts)
    sanitized = [sanitize_diagnostic_text(part) for part in texts]
    summary = {
        "content_complete": supported,
        "block_types": kinds,
        "other_block_count": other,
        "text_utf8_bytes": len(text.encode("utf-8")),
        "redacted_text_sha256": _digest("".join(sanitized)),
        "redacted_content_sha256": _digest(canonical),
        "expected_marker_observed": "WORKER_RESULT" in text or (False if supported else None),
        "exact_canned_matches": [
            label for label, canned in _CANNED.items() if supported and text == canned
        ],
    }
    return summary, texts if supported else None


def _view(messages: list[Any], complete: bool) -> tuple[dict[str, Any], list[list[str]] | None]:
    summaries = []
    originals = []
    complete = complete and len(messages) <= _MAX_MESSAGES
    for message in messages[:_MAX_MESSAGES]:
        summary, text = _blocks(message)
        summaries.append(summary)
        if text is None:
            complete = False
        else:
            originals.append(text)
    return {
        "assistant_count_observed": len(messages),
        "complete": complete,
        "expected_marker_observed": any(s["expected_marker_observed"] for s in summaries)
        or (False if complete else None),
        "messages": summaries,
    }, originals if complete else None


def _contained_path(path: Path, root: Path) -> None:
    path.relative_to(root)
    for parent in [path, *path.parents]:
        if parent == root.parent:
            break
        if parent.is_symlink():
            raise ValueError("symlink")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("outside")


def _regular_bytes(path: Path, root: Path, limit: int) -> bytes:
    _contained_path(path, root)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("not_regular")
        result = handle.read(limit + 1)
    if len(result) > limit:
        raise ValueError("oversized")
    return result


def _native(
    home: Path, worker: str
) -> tuple[dict[str, Any], list[list[str]] | None, str | None, str | None]:
    unavailable = {"state": "unavailable", "view": None, "turn_status": "unknown"}
    try:
        bridge = home / ".omnigent/codex-native" / hashlib.sha256(worker.encode()).hexdigest()[:32]
        state = json.loads(_regular_bytes(bridge / "state.json", home, 64 * 1024))
        thread = state.get("thread_id")
        if (
            state.get("session_id") != worker
            or not isinstance(thread, str)
            or len(thread) > 128
            or not thread
            or any(c not in "0123456789abcdef-" for c in thread)
        ):
            return unavailable, None, None, None
        sessions = codex_home_for_bridge_dir(bridge) / "sessions"
        candidates = []
        for index, path in enumerate(sessions.glob(f"*/*/*/*{thread}.jsonl")):
            if index >= 1:
                return {**unavailable, "state": "ambiguous"}, None, thread, None
            candidates.append(path)
        if not candidates:
            return unavailable, None, thread, None
        data = _regular_bytes(candidates[0], home, _MAX_ROLLOUT_BYTES)
        lines = data.splitlines(keepends=True)
        complete = data.endswith(b"\n") and len(lines) <= 4096
        current = None
        target = set()
        messages: dict[str, list[Any]] = {}
        statuses = {}
        identities = []
        for line in lines[:4096]:
            if not line.endswith(b"\n"):
                complete = False
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                complete = False
                continue
            payload = record.get("payload", {})
            if not isinstance(payload, dict):
                complete = False
                continue
            kind = record.get("type")
            event = payload.get("type")
            if kind == "session_meta":
                identities.append(payload.get("id") == thread)
            if kind == "turn_context" or (kind == "event_msg" and event == "task_started"):
                value = payload.get("turn_id")
                current = value if isinstance(value, str) else None
            if (
                kind == "event_msg"
                and event == "user_message"
                and isinstance(payload.get("message"), str)
                and "WORKER_TASK" in payload["message"]
            ):
                target.add(current)
            if kind == "response_item" and payload.get("type") == "message":
                if payload.get("role") not in (
                    "assistant",
                    "user",
                    "developer",
                    "system",
                ) or not isinstance(payload.get("content"), list):
                    complete = False
            if (
                kind == "response_item"
                and payload.get("type") == "message"
                and payload.get("role") == "assistant"
                and current is not None
            ):
                messages.setdefault(current, []).append(payload.get("content"))
            if kind == "event_msg" and event in {"task_complete", "turn_aborted", "error"}:
                turn = payload.get("turn_id", current)
                if isinstance(turn, str):
                    statuses[turn] = {
                        "task_complete": "completed",
                        "turn_aborted": "aborted",
                        "error": "error",
                    }[event]
        if identities != [True] or len(target) != 1 or None in target:
            return {**unavailable, "state": "unattributed"}, None, thread, None
        turn = target.pop()
        view, original = _view(messages.get(turn, []), complete)
        return (
            {
                "state": "ok" if complete else "partial",
                "view": view,
                "turn_status": statuses.get(turn, "unknown"),
            },
            original,
            thread,
            turn,
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return unavailable, None, None, None


def _user_text(request: dict[str, Any]) -> tuple[str, bool]:
    parts = []
    inputs = request.get("input", request.get("messages", []))
    if isinstance(inputs, str):
        return inputs, True
    complete = isinstance(inputs, list) and len(inputs) <= 256
    for item in inputs[:256] if isinstance(inputs, list) else []:
        if not isinstance(item, dict):
            complete = False
            continue
        if item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            complete = complete and len(content) <= 64
            for block in content[:64]:
                if (
                    isinstance(block, dict)
                    and block.get("type") in {"text", "input_text"}
                    and isinstance(block.get("text"), str)
                ):
                    parts.append(block["text"])
                else:
                    complete = False
        else:
            complete = False
    return " ".join(parts), complete


def _requests(read: Callable[[], list[dict[str, Any]]], thread: str | None) -> dict[str, Any]:
    result = {
        "state": "unavailable",
        "ledger_completeness": "unknown",
        "actual_queue_consumption": "not_observed",
    }
    try:
        requests = read()
        if not isinstance(requests, list):
            return result
        summaries = []
        owned = 0
        for ordinal, request in enumerate(requests[:_MAX_REQUESTS]):
            if not isinstance(request, dict):
                continue
            cache_key = request.get("prompt_cache_key")
            owner = (
                "unknown"
                if not thread or not isinstance(cache_key, str)
                else ("worker" if cache_key == thread else "other")
            )
            owned += owner == "worker"
            text, input_complete = _user_text(request)
            if owner != "worker" and "WORKER_TASK" not in text:
                continue
            names = []
            tools = request.get("tools", [])
            tools_complete = isinstance(tools, list) and len(tools) <= 256
            for tool in tools[:256] if isinstance(tools, list) else []:
                if isinstance(tool, dict):
                    function = tool.get("function", tool)
                    name = function.get("name") if isinstance(function, dict) else None
                    valid_name = isinstance(name, str) and bool(name)
                    tools_complete = tools_complete and valid_name
                    names.append(name if valid_name else None)
                else:
                    tools_complete = False
            summaries.append(
                {
                    "ordinal": ordinal,
                    "owner": owner,
                    "model_role": {
                        "gpt-4o": "expected_generation",
                        "codex-auto-review": "auto_review",
                        "_policy_llm_": "policy",
                    }.get(request.get("model"), "other"),
                    "worker_marker_observed": "WORKER_TASK" in text
                    or (False if input_complete else None),
                    "required_namespace_present": "mcp__omnigent" in names
                    or (False if tools_complete else None),
                    "allowed_tool_names": sorted(
                        _TOOLS.intersection(name for name in names if isinstance(name, str))
                    ),
                    "other_tool_count": sum(name not in _TOOLS for name in names),
                }
            )
        partial = len(requests) > _MAX_REQUESTS or len(summaries) > 8
        return {
            **result,
            "state": "partial" if partial else "ok",
            "request_count_observed": len(requests),
            "worker_owned_count_observed": owned,
            "requests": summaries[:8],
        }
    except Exception:
        return result


def _known_stored_item(item: Any) -> bool:
    if not isinstance(item, dict) or not isinstance(item.get("type"), str):
        return False
    if item["type"] == "message":
        return item.get("role") in ("assistant", "user") and isinstance(item.get("content"), list)
    if "role" in item:
        return False
    try:
        parse_item_data(item["type"], item)
    except (ValueError, TypeError):
        return False
    return True


def collect_worker_observations(
    *,
    home: Path,
    worker: str,
    read_requests: Callable[[], list[dict[str, Any]]],
    read_items: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Read one synthetic fixture without exporting payloads or identities."""
    native, native_text, thread, turn = _native(home, worker)
    stored: dict[str, Any] = {"state": "unavailable", "view": None}
    stored_text = None
    try:
        response = read_items()
        items = response["data"]
        if not isinstance(items, list):
            raise ValueError("invalid_items")
        assistants = [
            item
            for item in items[:100]
            if isinstance(item, dict) and item.get("role") == "assistant"
        ]
        if thread and turn:
            prefix = f"codex:{thread}:{turn}:"
            selected = [
                item.get("content")
                for item in assistants
                if isinstance(item.get("stream_message_id"), str)
                and item["stream_message_id"].startswith(prefix)
            ]
            unattributed = any(
                not isinstance(item.get("stream_message_id"), str)
                or not item["stream_message_id"].startswith("codex:")
                for item in assistants
            )
            complete = (
                not response.get("has_more", False)
                and len(items) < 100
                and not unattributed
                and all(_known_stored_item(item) for item in items)
            )
            view, stored_text = _view(selected, complete)
            stored = {"state": "ok" if complete else "partial", "view": view}
        else:
            stored = {"state": "unattributed", "view": None}
    except Exception:
        pass
    comparable = native_text is not None and stored_text is not None
    return {
        "schema_version": 1,
        "kind": "private_manager_default_native_worker_wait",
        "native": native,
        "stored": stored,
        "mock": _requests(read_requests, thread),
        "comparison": {
            "exact_text_equal": ["".join(parts) for parts in native_text]
            == ["".join(parts) for parts in stored_text]
            if comparable
            else None,
            "exact_text_blocks_equal": native_text == stored_text if comparable else None,
            "redacted_content_hash_equal": (
                [message["redacted_content_sha256"] for message in native["view"]["messages"]]
                == [message["redacted_content_sha256"] for message in stored["view"]["messages"]]
                if comparable
                else None
            ),
        },
    }


@contextmanager
def capture_private_worker_failure(
    *,
    enabled: bool,
    home: Path,
    artifact: Path,
    worker: str,
    read_requests: Callable[[], list[dict[str, Any]]],
    read_items: Callable[[], dict[str, Any]],
) -> Iterator[None]:
    """Keep the original assertion even when failure diagnostics are unavailable."""
    try:
        yield
    except AssertionError:
        if enabled:
            try:
                snapshot = collect_worker_observations(
                    home=home, worker=worker, read_requests=read_requests, read_items=read_items
                )
                encoded = (
                    json.dumps(snapshot, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
                )
                if len(encoded) > _MAX_BYTES:
                    encoded = b'{"schema_version":1,"capture":"output_limit","truncated":true}\n'
                _contained_path(artifact, home.parent)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(
                    artifact, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded)
            except Exception:
                pass
        raise
