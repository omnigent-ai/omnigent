"""Runner-side handlers for the ``sys_journal_*`` tools and ``sys_workflow_request_input``.

The runner has no journal store, so these tools call the server's
``/v1/sessions/{id}/journal`` routes. A write carries an idempotency key that
its retries reuse, and once the server accepts it the runner publishes
``session.journal.appended`` on the session stream, the way it reports
child-session updates. Reads come back as compact lines the model can scan.
``sys_workflow_request_input`` has the server mark a task waiting and raise its
questions, then waits for the answer like any other elicitation.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import itertools
import json
import random
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from omnigent.entities.session_journal import (
    IDEMPOTENCY_KEY_MAX_CHARS,
    JOURNAL_KINDS,
    InvalidJournalEntryError,
    JournalEntryTooLargeError,
    normalize_note,
    normalize_plan,
    normalize_status,
    normalize_step_id,
)
from omnigent.entities.workflow_questions import answers_from_content, normalize_questions
from omnigent.runner import pending_approvals
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.tools.builtins.journal import JOURNAL_READ_PAGE_MAX, JOURNAL_TOOLS

JOURNAL_TOOL_NAMES: frozenset[str] = frozenset(tool.name() for tool in JOURNAL_TOOLS)

# A read returns the newest entries whose lines fit this budget.
_READ_OUTPUT_MAX_BYTES = 32 * 1024
# Retries while the runner's tunnel reconnects; the idempotency key makes them safe.
_WRITE_ATTEMPTS = 3
_RETRYABLE_STATUSES = frozenset({502, 503, 504})
_STATUS_GLYPHS = {
    "started": "▶",
    "done": "✅",
    "needs_input": "✋",
    "failed": "❌",
}
# What a server answers for a path it has no route for, unlike a route's own
# 404: FastAPI's default body, or the web UI catch-all's generic error.
_UNROUTED_NOT_FOUND_BODIES = (
    {"detail": "Not Found"},
    {"error": {"code": "not_found", "message": "Not found"}},
)
_UNSUPPORTED = "this Omnigent server has no session journal"

_Publisher = Callable[[str, dict[str, Any]], None]


def _journal_url(session_id: str) -> str:
    """The session's journal route; the ID must stay one URL segment."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        raise ValueError("conversation_id must be a session ID")
    return f"/v1/sessions/{session_id}/journal"


def _error(message: str, **fields: object) -> str:
    return json.dumps({"error": message, **fields}, ensure_ascii=False)


def _is_unrouted(response: httpx.Response) -> bool:
    """Whether *response* says the server has no such route, so it predates the journal."""
    if response.status_code == 405:
        return True
    if response.status_code != 404:
        return False
    try:
        return response.json() in _UNROUTED_NOT_FOUND_BODIES
    except ValueError:
        return False


def _server_message(response: httpx.Response) -> str:
    """The server's error message, e.g. ``"invalid_entry: text is required…"``."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    detail = body.get("detail") if isinstance(body, dict) else None
    if response.status_code == 422 and detail is not None:
        return f"invalid_entry: {json.dumps(detail, ensure_ascii=False)[:500]}"
    return response.text[:500]


def _idempotency_key(call_id: str | None) -> str:
    """``tool:<call ID>``, or a fresh ID when the caller has none, e.g. over MCP."""
    key = f"tool:{call_id}" if call_id else f"tool:{uuid.uuid4().hex}"
    if len(key) > IDEMPOTENCY_KEY_MAX_CHARS or not key.isascii():
        key = f"tool:sha256:{hashlib.sha256(key.encode()).hexdigest()}"
    return key


def _write_request(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Build the POST body, checking fields locally so errors come back at once.

    :raises InvalidJournalEntryError: If a field is invalid.
    """
    step = args.get("step")
    if step is not None:
        step = normalize_step_id(step)
    if tool_name == "sys_journal_write_note":
        return {"kind": "note", "payload": normalize_note(args.get("body")), "step": step}
    if tool_name == "sys_journal_write_status":
        payload = normalize_status(args.get("status"), text=args.get("text"))
        return {"kind": "status", "payload": payload, "step": step}
    plan = normalize_plan(args.get("steps"), name=args.get("name"))
    return {
        "kind": "plan",
        "payload": {"name": plan["name"], "steps": plan["steps"]},
        "step": None,
    }


async def _post_with_retries(
    server_client: httpx.AsyncClient, url: str, body: dict[str, Any], conversation_id: str
) -> httpx.Response | str:
    """POST the write, retrying transport errors and gateway failures with the same key."""
    for attempt in range(_WRITE_ATTEMPTS):
        last = attempt + 1 == _WRITE_ATTEMPTS
        try:
            response = await server_client.post(
                url,
                json=body,
                headers={ORCHESTRATION_SOURCE_HEADER: conversation_id},
                timeout=30.0,
            )
        except httpx.TransportError as exc:
            if last:
                return f"could not reach the server: {exc}"
        else:
            if response.status_code not in _RETRYABLE_STATUSES or last:
                return response
        await _sleep(0.5 * (attempt + 1) + random.uniform(0.0, 0.25))
    raise AssertionError("unreachable")


async def _sleep(seconds: float) -> None:
    """Wait before a retry; tests replace this, never ``asyncio.sleep`` itself."""
    await asyncio.sleep(seconds)


def _publish(
    publish_event: _Publisher | None, conversation_id: str, entry: dict[str, Any]
) -> None:
    event = {
        "type": "session.journal.appended",
        "conversation_id": conversation_id,
        "entry": entry,
    }
    if publish_event is not None:
        publish_event(conversation_id, event)
        return
    from omnigent.runtime import session_stream

    session_stream.publish(conversation_id, event)


async def _write(
    tool_name: str,
    args: dict[str, Any],
    *,
    server_client: httpx.AsyncClient,
    conversation_id: str,
    call_id: str | None,
    publish_event: _Publisher | None,
) -> str:
    try:
        body = _write_request(tool_name, args)
    except JournalEntryTooLargeError as exc:
        return _error(f"entry_too_large: {exc}")
    except InvalidJournalEntryError as exc:
        return _error(f"invalid_entry: {exc}")
    body["idempotency_key"] = _idempotency_key(call_id)
    url = _journal_url(conversation_id)
    response = await _post_with_retries(server_client, url, body, conversation_id)
    if isinstance(response, str):
        return _error(f"{tool_name} failed: {response}")
    if _is_unrouted(response):
        return _error(f"{tool_name}: {_UNSUPPORTED}")
    if response.status_code == 404:
        return _error("session_not_found", conversation_id=conversation_id)
    if response.status_code >= 400:
        return _error(_server_message(response))
    entry = response.json()
    _publish(publish_event, conversation_id, entry)
    result: dict[str, Any] = {"id": entry["id"], "position": entry["position"]}
    if entry.get("kind") == "plan":
        result["version"] = entry["payload"]["version"]
    return json.dumps(result)


# ── Asking the user ───────────────────────────────────


def _stream_publisher(publish_event: _Publisher | None) -> _Publisher:
    if publish_event is not None:
        return publish_event
    from omnigent.runtime import session_stream

    def publish(conversation_id: str, event: dict[str, Any]) -> None:
        session_stream.publish(conversation_id, event)

    return publish


async def _request_input(
    args: dict[str, Any],
    *,
    server_client: httpx.AsyncClient,
    conversation_id: str,
    call_id: str | None,
    publish_event: _Publisher | None,
) -> str:
    """Mark a task waiting, raise its questions, and wait for the user's answers."""
    tool_name = "sys_workflow_request_input"
    try:
        step = normalize_step_id(args.get("step"))
        questions = normalize_questions(args.get("questions"))
    except InvalidJournalEntryError as exc:
        return _error(f"invalid_input: {exc}")
    body = {"step": step, "questions": questions, "idempotency_key": _idempotency_key(call_id)}
    url = f"{_journal_url(conversation_id)}/input-requests"
    while True:
        response = await _post_with_retries(server_client, url, body, conversation_id)
        if isinstance(response, str):
            return _error(f"{tool_name} failed: {response}")
        if _is_unrouted(response):
            return _error(f"{tool_name}: this Omnigent server can't ask questions for workflows")
        if response.status_code == 404:
            return _error("session_not_found", conversation_id=conversation_id)
        if response.status_code >= 400:
            return _error(_server_message(response))
        requested = response.json()
        _publish(publish_event, conversation_id, requested["entry"])
        try:
            verdict = await pending_approvals.wait_for_user_verdict(
                elicitation_id=requested["elicitation_id"],
                conversation_id=conversation_id,
                publish_event=_stream_publisher(publish_event),
                retry_on_server_reconnect=True,
            )
        except pending_approvals.ServerReconnected:
            # The server may have lost the question; the same key raises it again.
            continue
        break
    if not verdict.approved:
        return json.dumps(
            {
                "answered": False,
                "detail": (
                    "No answer: the user dismissed the question, or it timed out. The "
                    "task stays waiting for them; don't ask again this turn."
                ),
            }
        )
    return json.dumps(
        {"answered": True, "answers": answers_from_content(questions, verdict.content)},
        ensure_ascii=False,
    )


# ── Reads ─────────────────────────────────────────────


def _encode_cursor(state: dict[str, Any]) -> str:
    raw = json.dumps(state, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        state = json.loads(raw)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("cursor is not a sys_journal_read cursor") from exc
    if not isinstance(state, dict) or not isinstance(state.get("after"), str):
        raise ValueError("cursor is not a sys_journal_read cursor")
    return state


def _when(created_at: object) -> str:
    if not isinstance(created_at, int):
        return "?"
    return datetime.fromtimestamp(created_at, UTC).strftime("%m-%d %H:%MZ")


def _plan_change(plan: dict[str, Any], previous: dict[str, Any] | None) -> str:
    """One line on what a plan changed, e.g. ``"added opt-n1, opt-pool"``."""
    steps = plan.get("payload", {}).get("steps", [])
    if previous is None:
        return f"{len(steps)} step{'' if len(steps) == 1 else 's'}: " + ", ".join(
            step["id"] for step in steps
        )
    before = {step["id"]: step for step in previous.get("payload", {}).get("steps", [])}
    after = {step["id"]: step for step in steps}
    added = [step_id for step_id in after if step_id not in before]
    removed = [step_id for step_id in before if step_id not in after]
    changed = [
        step_id for step_id in after if step_id in before and after[step_id] != before[step_id]
    ]
    parts = [
        f"{label} {', '.join(ids)}"
        for label, ids in (("added", added), ("removed", removed), ("changed", changed))
        if ids
    ]
    return "; ".join(parts) or "no change"


def _line(entry: dict[str, Any], previous_plans: dict[str, dict[str, Any] | None]) -> str:
    """Render one entry as a compact line; a note's later lines follow, indented."""
    head = f"#{entry['position']} {_when(entry.get('created_at'))}"
    run_id = entry.get("scheduled_task_run_id")
    if run_id:
        head += f" run {run_id[:8]}"
    kind, payload = entry.get("kind"), entry.get("payload") or {}
    step = f" [{entry['step']}]" if entry.get("step") else ""
    if kind == "status":
        status = str(payload.get("status"))
        text = f": {payload['text']}" if payload.get("text") else ""
        return f"{head} · status{step} {_STATUS_GLYPHS.get(status, '')} {status}{text}"
    if kind == "note":
        first, _, rest = str(payload.get("body", "")).partition("\n")
        line = f"{head} · note{step}: {first}"
        return line + "".join(f"\n    {more}" for more in rest.splitlines())
    if kind == "plan":
        author = (entry.get("author") or {}).get("type", "agent")
        change = _plan_change(entry, previous_plans.get(entry["id"]))
        name = f" {json.dumps(payload['name'], ensure_ascii=False)}" if payload.get("name") else ""
        return f"{head} · plan v{payload.get('version')}{name} ({author}): {change}"
    return f"{head} · {kind}{step}: {json.dumps(payload, ensure_ascii=False)}"


async def _get(
    server_client: httpx.AsyncClient, target: str, params: dict[str, Any]
) -> dict[str, Any] | str:
    try:
        response = await server_client.get(_journal_url(target), params=params, timeout=30.0)
    except ValueError as exc:
        return _error(f"invalid_entry: {exc}")
    except httpx.HTTPError as exc:
        return _error(f"sys_journal_read failed: {exc}")
    if _is_unrouted(response):
        return _error(f"sys_journal_read: {_UNSUPPORTED}")
    if response.status_code == 404:
        return _error("session_not_found", conversation_id=target)
    if response.status_code in (401, 403):
        return _error("session_out_of_tree", conversation_id=target)
    if response.status_code >= 400:
        return _error(_server_message(response))
    body = response.json()
    return body if isinstance(body, dict) else _error("sys_journal_read: malformed response")


async def _previous_plans(
    server_client: httpx.AsyncClient, target: str, entries: list[dict[str, Any]]
) -> dict[str, dict[str, Any] | None]:
    """Each plan entry's predecessor, for its change line; one extra read at most."""
    plans = [entry for entry in entries if entry.get("kind") == "plan"]  # newest first
    previous: dict[str, dict[str, Any] | None] = {}
    for newer, older in itertools.pairwise(plans):
        previous[newer["id"]] = older
    if plans:
        oldest = plans[-1]
        page = await _get(
            server_client,
            target,
            {"kind": "plan", "order": "desc", "after": oldest["id"], "limit": 1},
        )
        previous[oldest["id"]] = (
            page["data"][0] if isinstance(page, dict) and page["data"] else None
        )
    return previous


async def _read(
    args: dict[str, Any], *, server_client: httpx.AsyncClient, conversation_id: str
) -> str:
    target = args.get("conversation_id") or conversation_id
    kind, step, limit = args.get("kind"), args.get("step"), args.get("limit")
    if not isinstance(target, str):
        return _error("invalid_entry: conversation_id must be a session ID")
    if kind is not None and kind not in JOURNAL_KINDS:
        return _error(f"invalid_entry: kind must be one of {', '.join(JOURNAL_KINDS)}")
    if limit is None:
        limit = JOURNAL_READ_PAGE_MAX
    if not isinstance(limit, int) or not 1 <= limit <= JOURNAL_READ_PAGE_MAX:
        return _error(f"invalid_entry: limit must be between 1 and {JOURNAL_READ_PAGE_MAX}")
    try:
        if step is not None:
            step = normalize_step_id(step)
        after = None
        if args.get("cursor") is not None:
            state = _decode_cursor(str(args["cursor"]))
            if (state.get("conversation_id"), state.get("kind"), state.get("step")) != (
                target,
                kind,
                step,
            ):
                raise ValueError("cursor belongs to a read with other arguments; repeat them")
            after = state["after"]
    except (InvalidJournalEntryError, ValueError) as exc:
        return _error(f"invalid_entry: {exc}")
    params: dict[str, Any] = {"order": "desc", "limit": limit}
    for name, value in (("kind", kind), ("step", step), ("after", after)):
        if value is not None:
            params[name] = value
    page = await _get(server_client, target, params)
    if isinstance(page, str):
        return page
    entries: list[dict[str, Any]] = page.get("data", [])
    if kind == "plan":
        rendered: list[Any] = [
            {
                "position": entry["position"],
                "version": entry["payload"].get("version"),
                "name": entry["payload"].get("name"),
                "created_at": entry["created_at"],
                "author": (entry.get("author") or {}).get("type"),
                "steps": entry["payload"].get("steps", []),
            }
            for entry in entries
        ]
    else:
        previous = await _previous_plans(server_client, target, entries)
        rendered = [_line(entry, previous) for entry in entries]
    # Keep the newest entries that fit; the cursor continues from the oldest kept.
    kept, size = [], 0
    for entry, item in zip(entries, rendered, strict=True):
        cost = len(json.dumps(item, ensure_ascii=False).encode()) + 2
        if kept and size + cost > _READ_OUTPUT_MAX_BYTES:
            break
        kept.append((entry, item))
        size += cost
    has_more = bool(page.get("has_more")) or len(kept) < len(entries)
    next_cursor = (
        _encode_cursor(
            {"conversation_id": target, "kind": kind, "step": step, "after": kept[-1][0]["id"]}
        )
        if has_more and kept
        else None
    )
    return json.dumps(
        {
            "entries": [item for _, item in reversed(kept)],
            "page": {"has_more": has_more, "next_cursor": next_cursor},
        },
        ensure_ascii=False,
    )


async def execute_journal_tool(
    tool_name: str,
    arguments: str,
    *,
    server_client: httpx.AsyncClient | None,
    conversation_id: str | None,
    call_id: str | None = None,
    publish_event: _Publisher | None = None,
) -> str:
    """Run one journal tool call (``sys_journal_*``, ``sys_workflow_request_input``).

    :param tool_name: One of :data:`JOURNAL_TOOL_NAMES`.
    :param arguments: The call's JSON arguments.
    :param server_client: HTTP client pointed at the Omnigent server.
    :param conversation_id: The calling session; writes go to its journal only.
    :param call_id: The harness's tool call ID, for the write's idempotency key;
        ``None`` when the call came without one.
    :param publish_event: Publishes on a session's stream; ``None`` uses the
        runtime's session stream.
    :returns: The tool output, a JSON object; ``{"error": …}`` on failure.
    """
    if server_client is None or not conversation_id:
        return _error(f"{tool_name} requires server access")
    try:
        args = json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError:
        return _error(f"{tool_name}: malformed JSON arguments")
    if not isinstance(args, dict):
        return _error(f"{tool_name}: arguments must be an object")
    if tool_name == "sys_journal_read":
        return await _read(args, server_client=server_client, conversation_id=conversation_id)
    if tool_name == "sys_workflow_request_input":
        return await _request_input(
            args,
            server_client=server_client,
            conversation_id=conversation_id,
            call_id=call_id,
            publish_event=publish_event,
        )
    return await _write(
        tool_name,
        args,
        server_client=server_client,
        conversation_id=conversation_id,
        call_id=call_id,
        publish_event=publish_event,
    )
