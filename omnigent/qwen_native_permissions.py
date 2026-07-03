"""Qwen-native tool-approval mirror (TUI → web elicitation).

The ``qwen`` TUI gates its own tool calls (shell, file write, …) with an
in-terminal approval prompt. Unlike cursor-native — where the prompt lives only
in the TUI's in-memory state and has to be scraped off the pane — qwen exposes a
structured **permission control plane** on its dual-output stream: whenever a
tool needs approval it emits a ``control_request`` / ``can_use_tool`` event on
``--json-file`` (coexisting with the in-terminal prompt) and accepts a
``confirmation_response`` on ``--input-file`` (whichever side answers first
wins, the loser is harmlessly dropped). See qwen's ``dual-output.md``.

So to surface those approvals in the Omnigent web UI (so a user on the Chat tab
can answer from the chat view, not just inside the embedded terminal), the runner
tails the same event stream the transcript forwarder uses:

1. read a ``control_request`` / ``can_use_tool`` off ``--json-file`` (structured,
   no pane scraping),
2. POST it to the server's generic ``native-permission-request`` hook (shared
   with the hermes-/goose-native mirrors; ``agent="qwen"`` labels the card),
   which publishes the standard ``response.elicitation_request`` event and parks
   for the web verdict (the same machinery cursor-/codex-native use),
3. on the verdict, answer qwen by appending a ``confirmation_response`` to
   ``--input-file`` (``allowed=True`` on accept, ``False`` on decline/cancel) —
   no keystrokes,
4. if instead a ``control_response`` for that ``request_id`` appears while the
   card is still parked (the user answered inside the embedded terminal, or qwen
   auto-resolved), POST ``external_elicitation_resolved`` so the parked web card
   clears and skip the now-stale ``confirmation_response``.

This deliberately does NOT suppress qwen's native gate; qwen's own prompt remains
the source of truth and the fallback if the mirror ever misses a request (the
user can still answer in the terminal). It is the cleaner, structured analog of
:mod:`omnigent.cursor_native_permissions`. See ``docs/QWEN_NATIVE_DESIGN.md`` and
``docs/QWEN_FOLLOWUPS.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from omnigent.qwen_native_bridge import events_file_path, submit_confirmation

_logger = logging.getLogger(__name__)

#: Event-file poll cadence. Matches the transcript forwarder so a pending
#: approval surfaces in the web UI within a step of the terminal prompt.
_POLL_INTERVAL_S = 0.4
# The approval hook parks server-side until a human answers; allow a day, well
# past any realistic wait, so the runner's POST never abandons a live prompt.
_POST_TIMEOUT_S = 86400.0
#: Cap on a preview string POSTed to the card (server truncates too).
_PREVIEW_MAX = 1024


@dataclass(frozen=True)
class QwenApprovalRequest:
    """A parsed qwen ``can_use_tool`` control request.

    :param request_id: qwen's ``request_id`` for the control request — the
        correlation key answered via ``confirmation_response`` and matched
        against a later ``control_response``.
    :param tool_name: The tool qwen wants to run, e.g. ``"run_shell_command"``.
    :param message: Human-readable card message.
    :param preview: Compact preview for the card (the shell command, or the
        JSON-encoded tool input).
    """

    request_id: str
    tool_name: str
    message: str
    preview: str


@dataclass(frozen=True)
class _ControlEvent:
    """One parsed control-plane event from ``--json-file``.

    :param kind: ``"request"`` (a ``can_use_tool`` to park) or ``"response"``
        (a resolution to release the loser).
    :param request_id: The control request's id (correlates request ↔ response).
    :param approval: The parsed request when ``kind == "request"``, else ``None``.
    """

    kind: str
    request_id: str
    approval: QwenApprovalRequest | None


def qwen_permission_elicitation_id(session_id: str, request_id: str) -> str:
    """Return the deterministic Omnigent elicitation id for a qwen control request.

    qwen's ``request_id`` is already unique per pending tool call, so it keys the
    elicitation directly — stable across polls and recomputable for the
    loser-release path.
    """
    return f"elicit_qwen_{session_id}_{request_id}"


def _preview_for(tool_name: str, tool_input: object) -> str:
    """Render a compact card preview from a qwen tool input.

    A shell tool's ``command`` is the most useful single line; otherwise the
    JSON-encoded input, falling back to the bare tool name.
    """
    if isinstance(tool_input, dict):
        command = tool_input.get("command")
        if isinstance(command, str) and command.strip():
            return command.strip()[:_PREVIEW_MAX]
        try:
            return json.dumps(tool_input, ensure_ascii=False)[:_PREVIEW_MAX]
        except (TypeError, ValueError):
            pass
    return tool_name[:_PREVIEW_MAX]


def parse_can_use_tool(event: dict[str, object]) -> QwenApprovalRequest | None:
    """Parse a ``control_request`` / ``can_use_tool`` event, or ``None`` to skip.

    Tolerant of odd shapes: a missing ``tool_name`` degrades to ``"tool"`` and a
    non-dict ``input`` to an empty preview rather than dropping the request.

    :param event: One decoded ``--json-file`` event.
    :returns: The parsed approval request, or ``None`` when *event* is not a
        ``can_use_tool`` control request.
    """
    if event.get("type") != "control_request":
        return None
    request = event.get("request")
    if not isinstance(request, dict) or request.get("subtype") != "can_use_tool":
        return None
    request_id = event.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return None
    raw_name = request.get("tool_name")
    tool_name = raw_name if isinstance(raw_name, str) and raw_name else "tool"
    preview = _preview_for(tool_name, request.get("input"))
    return QwenApprovalRequest(
        request_id=request_id,
        tool_name=tool_name,
        message=f"qwen wants to run {tool_name}",
        preview=preview,
    )


def _control_response_request_id(event: dict[str, object]) -> str | None:
    """Return the ``request_id`` of a ``control_response`` event, or ``None``.

    qwen emits ``control_response`` whether the decision was made in the TUI or
    by an external ``confirmation_response`` — either way it marks the request
    resolved, which is the signal to release a still-parked web card.
    """
    if event.get("type") != "control_response":
        return None
    response = event.get("response")
    if not isinstance(response, dict):
        return None
    request_id = response.get("request_id")
    return request_id if isinstance(request_id, str) and request_id else None


def _read_new_control_events(events_file: Path, offset: int) -> tuple[list[_ControlEvent], int]:
    """Read NDJSON lines past *offset*, returning control events + the new offset.

    Mirrors :func:`omnigent.qwen_native_forwarder._read_new_events`: detects a
    truncated/recreated file (``size < offset`` → rewind to 0), consumes only
    fully terminated lines, and leaves a trailing partial line for the next poll.
    Only control-plane events (``control_request`` / ``control_response``) are
    returned; user/assistant transcript events are the forwarder's concern.
    """
    try:
        size = events_file.stat().st_size
    except OSError:
        return [], offset
    if size < offset:
        offset = 0  # file truncated by a relaunched terminal
    if size == offset:
        return [], offset
    try:
        with open(events_file, "rb") as fh:
            fh.seek(offset)
            data = fh.read(size - offset)
    except OSError:
        return [], offset
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return [], offset  # no complete line yet
    consumed = data[: last_nl + 1]
    new_offset = offset + len(consumed)
    events: list[_ControlEvent] = []
    for raw in consumed.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue  # tolerate a malformed line rather than stalling the tail
        if not isinstance(event, dict):
            continue
        approval = parse_can_use_tool(event)
        if approval is not None:
            events.append(_ControlEvent("request", approval.request_id, approval))
            continue
        response_id = _control_response_request_id(event)
        if response_id is not None:
            events.append(_ControlEvent("response", response_id, None))
    return events, new_offset


async def supervise_qwen_approval_mirror(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    auth: httpx.Auth | None = None,
    poll_interval_s: float = _POLL_INTERVAL_S,
) -> None:
    """
    Tail qwen's ``--json-file`` and mirror its approval prompts to web elicitations.

    Runs for the session's lifetime (cancelled on teardown; any other exception
    is logged and the loop continues, so a transient failure never abandons the
    gate). Seeds the read offset at the current end of file so only *new* control
    requests are parked — history was already resolved, and re-parking it would
    flash stale cards. Several tool calls can be parked at once (qwen tags each
    with its own ``request_id``); a ``control_response`` for a still-parked
    request means it was answered in the TUI (or auto-resolved), so the web card
    is released via ``external_elicitation_resolved``.

    :param base_url: Server base URL.
    :param headers: Auth/routing headers for the runner's requests.
    :param session_id: Omnigent conversation id.
    :param bridge_dir: The qwen-native bridge dir holding the event/input files.
    :param auth: Optional httpx auth for the runner's requests.
    :param poll_interval_s: Event-file poll cadence in seconds.
    """
    events_file = events_file_path(bridge_dir)
    # Start watching from "now": only act on prompts emitted after launch. A
    # request already in the file was either resolved (has a control_response) or
    # is still showing in the TUI as the fallback; either way re-parking it would
    # be wrong. Matches cursor-native's "the terminal is the fallback" stance.
    try:
        offset = events_file.stat().st_size
    except OSError:
        offset = 0
    # request_id -> {"elicitation_id": str, "task": asyncio.Task}
    pending: dict[str, dict[str, object]] = {}
    timeout = httpx.Timeout(_POST_TIMEOUT_S, connect=10.0)
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        while True:
            try:
                events, offset = await asyncio.to_thread(
                    _read_new_control_events, events_file, offset
                )
                # A request whose control_response is already in THIS same batch
                # was answered (in the TUI or auto) within one poll window, before
                # we could park it. Parking it now would race its own response: the
                # response branch runs against a freshly-created task that hasn't
                # POSTed yet, so it can't release the card, which would then linger
                # until the server-side park times out. The decision is already
                # made — skip the card entirely.
                resolved_in_batch = {ev.request_id for ev in events if ev.kind == "response"}
                for ev in events:
                    if ev.kind == "request":
                        if (
                            ev.request_id in pending
                            or ev.approval is None
                            or ev.request_id in resolved_in_batch
                        ):
                            continue
                        elicitation_id = qwen_permission_elicitation_id(session_id, ev.request_id)
                        task = asyncio.create_task(
                            _run_one_approval(
                                client,
                                session_id=session_id,
                                bridge_dir=bridge_dir,
                                approval=ev.approval,
                                elicitation_id=elicitation_id,
                            ),
                            name=f"qwen-approval-{ev.request_id}",
                        )
                        pending[ev.request_id] = {
                            "elicitation_id": elicitation_id,
                            "task": task,
                        }
                    else:  # "response": the request was resolved
                        entry = pending.pop(ev.request_id, None)
                        if entry is None:
                            continue
                        task = entry["task"]
                        if isinstance(task, asyncio.Task) and not task.done():
                            # Resolved in the TUI (or auto) before the web card
                            # answered → release the parked card.
                            await _post_external_elicitation_resolved(
                                client, session_id, str(entry["elicitation_id"])
                            )
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "qwen approval mirror poll failed; session=%s bridge_dir=%s",
                    session_id,
                    bridge_dir,
                )
            await asyncio.sleep(poll_interval_s)


async def _run_one_approval(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    approval: QwenApprovalRequest,
    elicitation_id: str,
) -> None:
    """Park one qwen control request on the server and answer with the verdict."""
    # Reuse the vendor-agnostic native-permission hook (shared with the hermes-
    # and goose-native mirrors); ``agent`` labels the card and ``policy_name``
    # keeps the qwen flavor.
    payload = {
        "elicitation_id": elicitation_id,
        "agent": "qwen",
        "policy_name": "qwen_native_permission",
        "operation_type": approval.tool_name,
        "message": approval.message,
        "content_preview": approval.preview,
    }
    try:
        response = await client.post(
            f"/v1/sessions/{session_id}/hooks/native-permission-request",
            json=payload,
        )
    except httpx.HTTPError:
        _logger.exception("qwen permission hook POST failed; session=%s", session_id)
        return
    if response.status_code >= 400:
        _logger.warning(
            "qwen permission hook rejected: status=%s body=%s",
            response.status_code,
            response.text[:512],
        )
        return
    if not response.content:
        # Empty 2xx → resolved elsewhere (TUI answered) or timeout: no response.
        return
    try:
        result = response.json()
    except ValueError:
        _logger.warning("qwen permission hook returned non-JSON: %s", response.text[:512])
        return
    action = result.get("action") if isinstance(result, dict) else None
    if action == "accept":
        allowed = True
    elif action in {"decline", "cancel"}:
        allowed = False
    else:
        return
    try:
        await asyncio.to_thread(
            submit_confirmation, bridge_dir, request_id=approval.request_id, allowed=allowed
        )
    except RuntimeError:
        _logger.exception(
            "failed to write qwen confirmation_response for %s; session=%s",
            approval.request_id,
            session_id,
        )


async def _post_external_elicitation_resolved(
    client: httpx.AsyncClient, session_id: str, elicitation_id: str
) -> None:
    """Tell the server the native TUI answered a pending qwen prompt."""
    try:
        response = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "external_elicitation_resolved",
                "data": {"elicitation_id": elicitation_id},
            },
            timeout=10.0,
        )
        if response.status_code >= 400:
            _logger.warning(
                "qwen external_elicitation_resolved rejected: status=%s body=%s",
                response.status_code,
                response.text[:512],
            )
    except httpx.HTTPError:
        _logger.exception("qwen external_elicitation_resolved POST failed")
