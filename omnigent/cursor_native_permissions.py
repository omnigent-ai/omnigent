"""Cursor-native tool-approval + question surfacing (TUI → web elicitation).

The ``cursor-agent`` TUI gates its own tool calls (shell, file write/delete, MCP,
…) with an in-terminal approval prompt, and asks structured questions via its
``AskQuestion`` tool. To surface both in the Omnigent web UI (so a user can
answer from the chat view, not just inside the embedded terminal), the runner
tails cursor's chat ``store.db`` — the SAME store the forwarder mirrors — for
*pending tool calls* and drives the TUI to deliver the web verdict:

1. detect a pending tool call (an assistant ``tool-call`` content part carrying
   ``providerOptions.cursor.pendingToolCallStartedAtMs`` with no matching
   ``tool-result`` yet — see :func:`read_cursor_pending_tool_calls`),
2. after a short settle (auto-approved calls resolve inside it and never
   surface), POST it to the server's ``cursor-permission-request`` hook, which
   publishes ``response.elicitation_request`` and parks for the web verdict,
3. on the verdict, drive the cursor TUI by sending keystrokes into the pane
   (``y``/``Escape`` for an approval; the picker navigation for a question),
4. if the pending call instead disappears on its own (the user answered in the
   embedded terminal), POST ``external_elicitation_resolved`` to clear the card.

Detection lives in the transcript (keyed on the stable ``toolCallId``), NOT in
pane scraping — which silently missed any prompt whose wording fell outside a
regex. The pane is still used only to *deliver* the keystroke verdict. This does
NOT modify cursor's JS bundle and does NOT suppress cursor's native gate; the
TUI prompt remains the source of truth and the benign fallback if store
detection ever fails. See ``docs/cursor-native-elicitation.md`` (and the
superseded ``docs/cursor-native-tui-mirror-plan.md`` for the original pane-scrape
design and why the transcript channel replaced it).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from omnigent.cursor_native_bridge import send_cursor_pane_keys

# Reuse the forwarder's store discovery and WAL-aware blob reader so the
# transcript-based detector binds to the SAME cursor chat the forwarder mirrors
# (one chat per workspace) and reads the live ``-wal`` state correctly.
from omnigent.cursor_native_forwarder import _discover_store, _read_blob_rows

_logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.3
# The approval hook parks server-side until a human answers; allow a day, well
# past any realistic wait, so the runner's POST never abandons a live prompt.
_POST_TIMEOUT_S = 86400.0


@dataclass(frozen=True)
class CursorApprovalPrompt:
    """A cursor tool-approval prompt to surface as a web elicitation.

    Built from a transcript-detected pending tool call (see
    :func:`_prompt_from_pending`) and consumed by :func:`_run_one_approval` to
    render the card and drive the verdict keystroke.

    :param operation_type: Coarse type, ``"shell"`` for a command, else
        ``"tool"`` — a label only.
    :param message: Human-readable card message.
    :param preview: Compact preview for the card (the command / path / args).
    :param accept_key: tmux key to approve, e.g. ``"y"``.
    :param decline_key: tmux key to reject, e.g. ``"Escape"``.
    """

    operation_type: str
    message: str
    preview: str
    accept_key: str
    decline_key: str


async def _park_cursor_elicitation(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    payload: dict[str, object],
) -> dict | None:
    """POST the cursor permission hook and return the web verdict, or ``None``.

    ``None`` covers every "no keystroke" outcome: an empty 2xx (resolved in the
    TUI / timed out), an HTTP error, or a non-dict body — all logged here so the
    callers (approval and question) only deal with a real verdict dict.
    """
    try:
        response = await client.post(
            f"/v1/sessions/{session_id}/hooks/cursor-permission-request",
            json=payload,
        )
    except httpx.HTTPError:
        _logger.exception("cursor permission hook POST failed; session=%s", session_id)
        return None
    if response.status_code >= 400:
        _logger.warning(
            "cursor permission hook rejected: status=%s body=%s",
            response.status_code,
            response.text[:512],
        )
        return None
    if not response.content:
        # Empty 2xx → resolved elsewhere (TUI answered) or timeout: no keystroke.
        return None
    try:
        result = response.json()
    except ValueError:
        _logger.warning("cursor permission hook returned non-JSON: %s", response.text[:512])
        return None
    return result if isinstance(result, dict) else None


async def _send_cursor_keys(bridge_dir: Path, session_id: str, *keys: str) -> None:
    """Send tmux keys to the cursor pane ONE AT A TIME, with settle pauses.

    A multi-key sequence (the ``AskQuestion`` picker: ``Down``/``Space``/
    ``Enter``) must NOT be sent as a single ``send-keys`` call — the cursor TUI
    drops keys delivered back-to-back and needs a brief beat before it registers
    ``Enter`` (it re-renders the picker between keys). So send each key in its
    own call, pause between them, and pause a little longer before ``Enter``.
    A single-key approval (``y`` / ``Escape``) just sends once. Delivery failure
    is logged and aborts the rest of the sequence.
    """
    if not keys:
        return
    for key in keys:
        if key == "Enter":
            await asyncio.sleep(_KEY_ENTER_SETTLE_S)
        try:
            await asyncio.to_thread(send_cursor_pane_keys, bridge_dir, key)
        except RuntimeError:
            _logger.exception(
                "failed to send cursor keystroke %r (of %r); session=%s", key, keys, session_id
            )
            return
        await asyncio.sleep(_KEY_INTERVAL_S)
    _logger.debug("cursor keystrokes sent: %r; session=%s", keys, session_id)


async def _run_one_approval(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    prompt: CursorApprovalPrompt,
    elicitation_id: str,
) -> None:
    """Park one cursor approval prompt on the server and send the verdict key."""
    result = await _park_cursor_elicitation(
        client,
        session_id=session_id,
        payload={
            "elicitation_id": elicitation_id,
            "operation_type": prompt.operation_type,
            "message": prompt.message,
            "content_preview": prompt.preview,
        },
    )
    if result is None:
        return
    action = result.get("action")
    if action == "accept":
        await _send_cursor_keys(bridge_dir, session_id, prompt.accept_key)
    elif action in {"decline", "cancel"}:
        # Cursor's tool-reject doesn't dismiss on the decline key alone — it
        # opens a "Reason for rejection (Enter to submit, Esc to cancel)"
        # sub-prompt and waits there. Follow the decline key with Enter to
        # submit an empty reason so the rejection completes, instead of leaving
        # the TUI parked at the reason input (which the user then has to clear
        # by hand). The settle pause before Enter (see _send_cursor_keys) gives
        # the reason prompt time to render first.
        await _send_cursor_keys(bridge_dir, session_id, prompt.decline_key, "Enter")


async def _run_one_question(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    call: CursorPendingToolCall,
    elicitation_id: str,
) -> None:
    """Park one cursor ``AskQuestion`` on the server and drive the TUI picker.

    Renders as the structured multiple-choice form (not an approve/reject card)
    by sending an ``AskUserQuestion(...)`` ``content_preview`` the web UI already
    knows how to parse. On the web verdict, translates the chosen option labels
    into the TUI picker's key sequence (navigate → ``Space`` select → ``Enter``
    advance/submit); a decline sends ``Escape`` to skip the question.
    """
    result = await _park_cursor_elicitation(
        client,
        session_id=session_id,
        payload={
            "elicitation_id": elicitation_id,
            "operation_type": "question",
            "message": _askquestion_message(call.args),
            # Structured payload is the authoritative source the web UI renders
            # (no length cap); content_preview is the ≤1024-char legacy fallback.
            "ask_user_question": _askquestion_payload(call.args),
            "content_preview": _askquestion_preview(call.args),
        },
    )
    if result is None:
        return
    action = result.get("action")
    if action == "accept":
        content = result.get("content")
        keys = _askquestion_keystrokes(call.args, content if isinstance(content, dict) else {})
        _logger.debug(
            "cursor question accept; session=%s content=%r keys=%r", session_id, content, keys
        )
        await _send_cursor_keys(bridge_dir, session_id, *keys)
    elif action in {"decline", "cancel"}:
        # The question picker's "Esc to skip" dismisses cleanly (no rejection-
        # reason sub-prompt like the tool-approval gate has), so a single key.
        await _send_cursor_keys(bridge_dir, session_id, _TRANSCRIPT_DECLINE_KEY)
    else:
        _logger.warning(
            "cursor question verdict: unexpected action=%r; session=%s", action, session_id
        )


async def _post_external_elicitation_resolved(
    client: httpx.AsyncClient, session_id: str, elicitation_id: str
) -> None:
    """Tell the server the native TUI answered a pending cursor prompt."""
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
                "cursor external_elicitation_resolved rejected: status=%s body=%s",
                response.status_code,
                response.text[:512],
            )
    except httpx.HTTPError:
        _logger.exception("cursor external_elicitation_resolved POST failed")


# ─────────────────────────────────────────────────────────────────────────────
# Transcript-based elicitation detection (preferred over pane scraping)
#
# Pane scraping (above) recognises a prompt by its rendered wording, so it
# silently misses any prompt whose verb/keys fall outside the regex (e.g. the
# file-deletion prompt "Delete this file? → Delete (y) / Keep (n)", whose accept
# verb "Delete" is not in the run/allow/approve allowlist). The cursor chat
# ``store.db`` — the same store the forwarder tails — records the gated tool call
# itself the moment cursor blocks on it, which is a far more reliable signal:
#
#   • A blocked tool call is written as an assistant ``tool-call`` content part
#     carrying ``providerOptions.cursor.pendingToolCallStartedAtMs`` (an
#     auto-run call has the part but NOT the marker). The pending call lives
#     only inside cursor's binary protobuf *checkpoint* frame (not yet flushed
#     as a plain-JSON ``blobs`` row), so we scan each blob's raw bytes for
#     embedded JSON objects rather than ``json.loads``-ing the whole row.
#   • When the call is answered (in the web UI or the TUI), cursor appends a
#     ``tool-result`` content part with the SAME ``toolCallId``. So an active
#     elicitation is exactly a pending ``tool-call`` whose ``toolCallId`` has no
#     matching ``tool-result`` yet.
#
# This captures EVERY gated tool (Delete, Write, shell, …) with structured
# ``toolName`` + ``args``, with the ``toolCallId`` as a stable correlation key —
# no prompt-wording allowlist. The pane is still used to DELIVER the verdict
# (send-keys ``y`` / ``Escape``); only DETECTION moves to the transcript.
#
# Verified against cursor-agent 2026.06.24; the store schema is private and
# version-sensitive, so this remains a best-effort channel — cursor's own TUI
# gate stays authoritative, exactly as the pane mirror's contract.
# ─────────────────────────────────────────────────────────────────────────────

# tmux keys for the verdict. The transcript records WHAT cursor is asking but
# not the advertised hot-keys; cursor's per-tool gate uniformly accepts ``y``
# and rejects on ``Escape`` (empirically true across tool kinds), so map the
# web verdict onto those rather than reading the pane for key hints.
_TRANSCRIPT_ACCEPT_KEY = "y"
_TRANSCRIPT_DECLINE_KEY = "Escape"

# Inter-key pacing when driving the multi-step AskQuestion picker. The cursor
# TUI re-renders between keys and drops a back-to-back burst, so keys go one at
# a time with a short gap, and a longer settle precedes ``Enter`` (which commits
# a question / submits) so the prior selection has rendered first.
_KEY_INTERVAL_S = 0.15
_KEY_ENTER_SETTLE_S = 0.4

# Tool names cursor treats as shell-ish, for the card's coarse operation_type.
_SHELL_TOOL_NAMES = frozenset({"shell", "run", "runterminalcmd", "terminal", "bash"})

# Tool names that are structured multiple-choice questions, not approval gates.
# These surface as an interactive question form (not approve/reject) and are
# answered by driving the TUI picker rather than a single y/Escape keystroke.
_QUESTION_TOOL_NAMES = frozenset({"askquestion"})

# Short settle before surfacing a pending call. The auto-approve flash is now
# excluded *structurally* (see read_cursor_pending_tool_calls: a committed call
# appears without the marker), so this is only a small backstop for the sub-poll
# race where cursor's marker frame is observed a tick before its committed frame.
# Kept short so a genuinely-gated prompt that resolves quickly (e.g. an
# Auto-review retry) still surfaces a card rather than being suppressed.
_ELICITATION_SETTLE_S = 0.5


@dataclass(frozen=True)
class CursorPendingToolCall:
    """One gated cursor tool call awaiting approval, parsed from ``store.db``.

    :param tool_call_id: cursor's ``toolCallId`` (a stable per-call id; note it
        may embed a newline). The correlation key between the pending
        ``tool-call`` and its eventual ``tool-result``.
    :param tool_name: The gated tool, e.g. ``"Delete"`` / ``"Write"`` /
        ``"Shell"``.
    :param args: The tool arguments, e.g. ``{"path": "…/hello.txt"}``.
    """

    tool_call_id: str
    tool_name: str
    args: dict[str, object]


def cursor_tool_call_elicitation_id(session_id: str, tool_call_id: str) -> str:
    """Return the deterministic Omnigent elicitation id for a gated tool call.

    Keyed by ``toolCallId`` (hashed for a clean, fixed-width id) so the same
    pending call maps to the same elicitation across polls and supervisor
    restarts — the transcript analog of :func:`cursor_permission_elicitation_id`.
    """
    digest = hashlib.sha256(tool_call_id.encode("utf-8")).hexdigest()[:16]
    return f"elicit_cursor_{session_id}_{digest}"


def _iter_embedded_json_objects(raw: bytes) -> list[dict]:
    """Extract top-level JSON objects embedded anywhere in a blob's raw bytes.

    A pending tool call lives inside cursor's binary protobuf checkpoint frame
    with the JSON message embedded (and binary protobuf before/after it), so a
    whole-row ``json.loads`` fails. Decode as latin-1 (1 byte → 1 char, keeping
    indices aligned; JSON is ASCII regardless) and brace-match each ``{`` while
    tracking string state + escapes so braces inside strings don't miscount. A
    candidate ``{`` in surrounding binary that fails to balance or parse is
    skipped without abandoning the rest of the row.

    :param raw: The raw ``blobs.data`` bytes.
    :returns: Every successfully parsed JSON object (dicts only), in order.
    """
    text = raw.decode("latin-1")
    objects: list[dict] = []
    i, n = 0, len(text)
    while i < n:
        # Only attempt at a plausible object opener: ``{`` then (whitespace) ``"``.
        # Every message/part object we care about is keyed (``{"type"``, ``{"role"``,
        # ``{"args":{"…``), so this skips the stray ``{`` bytes that pepper the
        # surrounding binary protobuf — cheaply and without false starts.
        if text[i] != "{":
            i += 1
            continue
        k = i + 1
        while k < n and text[k] in " \t\r\n":
            k += 1
        if k >= n or text[k] != '"':
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        parsed_ok = False
        j = i
        while j < n:
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[i : j + 1])
                    except ValueError:
                        obj = None
                    if isinstance(obj, dict):
                        objects.append(obj)
                        parsed_ok = True
                    break
            j += 1
        # Jump past the object ONLY when it parsed; a balanced-but-invalid span
        # (a stray opener whose braces happened to balance around real JSON) must
        # advance by one so the genuine object nested inside still gets scanned —
        # otherwise a large binary checkpoint frame can swallow a real tool-call.
        i = (j + 1) if parsed_ok else (i + 1)
    return objects


def _pending_started_ms(obj: dict) -> int | None:
    """Return ``providerOptions.cursor.pendingToolCallStartedAtMs`` or ``None``."""
    provider = obj.get("providerOptions")
    if not isinstance(provider, dict):
        return None
    cursor = provider.get("cursor")
    if not isinstance(cursor, dict):
        return None
    marker = cursor.get("pendingToolCallStartedAtMs")
    return marker if isinstance(marker, int) else None


def read_cursor_pending_tool_calls(store_path: Path) -> list[CursorPendingToolCall]:
    """Return the gated tool calls currently awaiting a human in *store_path*.

    Scans every blob for embedded JSON messages and classifies each
    ``toolCallId`` by how its ``tool-call`` part appears:

    * **pending** — appears in an object WITH ``pendingToolCallStartedAtMs``
      (cursor is blocking on it),
    * **committed** — appears in an object WITHOUT the marker (cursor finalized
      it to run: either auto-approved, or approved and now executing),
    * **resolved** — has a ``tool-result``.

    A call is *awaiting a human* exactly when it is pending and NOT (committed or
    resolved). Empirically a call that's genuinely blocked on the human appears
    ONLY with the marker (never committed) until answered, while an
    auto-approved / committed call appears without it — so this excludes the
    auto-approve flash structurally, with no timing heuristic. Pure / read-only,
    so it is safe to run alongside the forwarder tailing the same store.

    :param store_path: This session's cursor ``store.db``.
    :returns: Active pending tool calls (cursor shows them one at a time, but the
        list supports more than one defensively), in first-seen order.
    """
    pending: dict[str, CursorPendingToolCall] = {}
    committed: set[str] = set()
    resolved: set[str] = set()
    for _rowid, _blob_id, data in _read_blob_rows(store_path, 0):
        raw = bytes(data) if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")
        for obj in _iter_embedded_json_objects(raw):
            content = obj.get("content")
            if not isinstance(content, list):
                continue
            marker = _pending_started_ms(obj)
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                tool_call_id = part.get("toolCallId")
                if not isinstance(tool_call_id, str):
                    continue
                if part_type == "tool-call":
                    if marker is not None:
                        tool_name = part.get("toolName")
                        args = part.get("args")
                        pending[tool_call_id] = CursorPendingToolCall(
                            tool_call_id=tool_call_id,
                            tool_name=tool_name if isinstance(tool_name, str) else "tool",
                            args=args if isinstance(args, dict) else {},
                        )
                    else:
                        committed.add(tool_call_id)
                elif part_type == "tool-result":
                    resolved.add(tool_call_id)
    return [
        call for tcid, call in pending.items() if tcid not in committed and tcid not in resolved
    ]


def _preview_for_args(args: dict[str, object]) -> str:
    """Build a compact human preview from a gated tool call's args."""
    for key in ("command", "path", "file_path", "filePath", "target"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value[:1024]
    try:
        return json.dumps(args, ensure_ascii=False)[:1024]
    except (TypeError, ValueError):
        return str(args)[:1024]


def _prompt_from_pending(call: CursorPendingToolCall) -> CursorApprovalPrompt:
    """Adapt a transcript-detected pending call to the existing card payload.

    Reuses :class:`CursorApprovalPrompt` (and thus :func:`_run_one_approval`)
    so the server hook, card rendering and keystroke path are unchanged — only
    the detection source differs.
    """
    preview = _preview_for_args(call.args)
    operation_type = "shell" if call.tool_name.lower() in _SHELL_TOOL_NAMES else "tool"
    return CursorApprovalPrompt(
        operation_type=operation_type,
        message=f"Cursor wants to run {call.tool_name}",
        preview=preview,
        accept_key=_TRANSCRIPT_ACCEPT_KEY,
        decline_key=_TRANSCRIPT_DECLINE_KEY,
    )


def _is_question_call(call: CursorPendingToolCall) -> bool:
    """Whether a pending call is a structured question rather than a gate."""
    return call.tool_name.lower() in _QUESTION_TOOL_NAMES


def _iter_askquestion_questions(args: dict[str, object]) -> list[dict]:
    """Return the well-formed question dicts from an ``AskQuestion`` args blob."""
    questions = args.get("questions")
    if not isinstance(questions, list):
        return []
    return [q for q in questions if isinstance(q, dict)]


def _askquestion_message(args: dict[str, object]) -> str:
    """Card title for an ``AskQuestion`` (its ``title``, else a generic label)."""
    title = args.get("title")
    return title if isinstance(title, str) and title else "Cursor is asking a question"


def _askquestion_payload(args: dict[str, object]) -> dict[str, object]:
    """Translate cursor ``AskQuestion`` args into the web ``AskUserQuestion`` shape.

    The web UI renders the multiple-choice form from a ``{"questions": [...]}``
    structure (see ``web`` ``askUserQuestion`` lib). cursor's field names
    differ — its question text is ``prompt`` (vs ``question``) and it has no
    ``multiSelect`` — so map them across, preserving each question ``id`` we'll
    need to interpret the answer.
    """
    web_questions: list[dict[str, object]] = []
    for question in _iter_askquestion_questions(args):
        prompt = question.get("prompt") or question.get("question")
        options = question.get("options")
        if not isinstance(prompt, str) or not prompt or not isinstance(options, list):
            continue
        web_options = [
            {"label": opt["label"]}
            for opt in options
            if isinstance(opt, dict) and isinstance(opt.get("label"), str) and opt["label"]
        ]
        if not web_options:
            continue
        web_question: dict[str, object] = {
            "question": prompt,
            "options": web_options,
            "multiSelect": question.get("multiSelect") is True,
        }
        qid = question.get("id")
        if isinstance(qid, str) and qid:
            web_question["id"] = qid
        web_questions.append(web_question)
    return {"questions": web_questions}


def _askquestion_preview(args: dict[str, object]) -> str:
    """``AskUserQuestion(...)`` preview string — the legacy ≤1024-char fallback.

    The structured ``ask_user_question`` hook field (see :func:`_run_one_question`)
    is the authoritative source the web UI consumes; this preview is the
    binary-card fallback for when that field is absent or truncated.
    """
    return "AskUserQuestion(" + json.dumps(_askquestion_payload(args)) + ")"


def _askquestion_keystrokes(args: dict[str, object], content: dict) -> list[str]:
    """Compute the TUI key sequence that enters ``content`` and submits.

    cursor's picker: ``↑/↓`` move within a question's options, ``Space`` toggles
    the highlighted option, ``Enter`` advances to the next question (or submits
    on the last); the highlight resets to the first option of each question. The
    web form answers (``content``) are keyed by question ``id`` (else question
    text) with the chosen option *label(s)* as the value. For each question, in
    order, we move down to each chosen option, ``Space``-select it, then press
    ``Enter`` to advance/submit.

    A chosen value not matching any predefined option is treated as a free-form
    answer: navigate to the trailing "Other (type to answer)" row and type it.
    """
    keys: list[str] = []
    for question in _iter_askquestion_questions(args):
        prompt = question.get("prompt") or question.get("question")
        qid = question.get("id")
        answer_key = qid if isinstance(qid, str) and qid else prompt
        labels = [opt.get("label") for opt in question.get("options", []) if isinstance(opt, dict)]
        answer = content.get(answer_key) if isinstance(answer_key, str) else None
        if isinstance(answer, list):
            chosen: list[object] = answer
        elif isinstance(answer, str):
            chosen = [answer]
        else:
            chosen = []
        # The "Other" row sits just past the predefined options.
        other_index = len(labels)
        # Map each chosen value to a target row, keeping ascending order so the
        # ``Down`` navigation is monotonic; custom values target the Other row.
        targets: list[tuple[int, str | None]] = []
        for value in chosen:
            if value in labels:
                targets.append((labels.index(value), None))
            elif isinstance(value, str) and value:
                targets.append((other_index, value))
        pos = 0
        for index, custom_text in sorted(targets, key=lambda t: t[0]):
            keys.extend(["Down"] * (index - pos))
            pos = index
            if custom_text is not None:
                # Typing into the Other row implicitly selects it.
                keys.append(custom_text)
            else:
                keys.append("Space")
        keys.append("Enter")  # advance to the next question / submit on the last
    return keys


async def supervise_cursor_transcript_elicitations(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    workspace: str,
    launch_epoch_ms: int,
    auth: httpx.Auth | None = None,
    poll_interval_s: float = _POLL_INTERVAL_S,
    settle_s: float = _ELICITATION_SETTLE_S,
) -> None:
    """Mirror cursor's gated tool calls to web elicitations via the chat store.

    The transcript counterpart of :func:`supervise_cursor_approval_mirror`:
    instead of scraping the rendered pane, it tails this session's cursor
    ``store.db`` for pending tool calls (see
    :func:`read_cursor_pending_tool_calls`). A call is surfaced only once it has
    stayed continuously pending for ``settle_s`` — auto-approved calls resolve
    inside cursor's gate-decision window and so never produce a (flashing) card,
    while a call that truly waits on a human persists well past it. A surfaced
    call is parked on the server hook exactly as the pane mirror does; on the web
    verdict it sends the ``y`` / ``Escape`` keystroke into the pane, and when a
    surfaced call later vanishes from the store while still parked (answered in
    the TUI, or executed after approval) it releases the card via
    ``external_elicitation_resolved``.

    Store discovery reuses the forwarder's logic, so this binds to the same chat
    the forwarder mirrors. Detection is keyed by ``toolCallId`` (stable across
    polls and restarts), capturing every gated tool kind without a
    prompt-wording allowlist.

    :param base_url: Server base URL.
    :param headers: Auth/routing headers for the runner's requests.
    :param session_id: Omnigent conversation id.
    :param bridge_dir: The cursor-native bridge dir holding ``tmux.json``.
    :param workspace: The session's working directory (cursor's chat-dir key).
    :param launch_epoch_ms: Wall-clock ms when this terminal launched.
    :param auth: Optional httpx auth for the runner's requests.
    :param poll_interval_s: Store poll cadence in seconds.
    :param settle_s: How long a call must stay pending before it is surfaced.
    """
    # tool_call_id → {"elicitation_id": str, "task": asyncio.Task} for SURFACED
    # (parked) calls; tool_call_id → loop-time first seen pending, for calls
    # still inside the settle window (not yet surfaced).
    active: dict[str, dict[str, object]] = {}
    first_seen: dict[str, float] = {}
    store_path: Path | None = None
    loop = asyncio.get_running_loop()
    timeout = httpx.Timeout(_POST_TIMEOUT_S, connect=10.0)
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        while True:
            try:
                if store_path is None or not store_path.exists():
                    store_path = await asyncio.to_thread(
                        _discover_store, workspace, launch_epoch_ms
                    )
                pending_calls = (
                    await asyncio.to_thread(read_cursor_pending_tool_calls, store_path)
                    if store_path is not None
                    else []
                )
                seen_ids = {call.tool_call_id for call in pending_calls}
                now = loop.time()
                # Surfaced calls whose pending entry vanished → answered in the
                # TUI (or executed after approval): release the web card.
                for tool_call_id in [tcid for tcid in active if tcid not in seen_ids]:
                    entry = active.pop(tool_call_id)
                    task = entry["task"]
                    if isinstance(task, asyncio.Task) and not task.done():
                        await _post_external_elicitation_resolved(
                            client, session_id, str(entry["elicitation_id"])
                        )
                # Calls that vanished before settling were auto-approved — drop
                # their debounce timer silently (no card was ever shown).
                for tool_call_id in [tcid for tcid in first_seen if tcid not in seen_ids]:
                    pending_for = now - first_seen.pop(tool_call_id, now)
                    _logger.debug(
                        "cursor elicitation: pending call resolved within settle window "
                        "(%.2fs) — no card; session=%s tool_call_id=%s",
                        pending_for,
                        session_id,
                        tool_call_id.splitlines()[0],
                    )
                # Surface calls that have now stayed pending past the settle window.
                for call in pending_calls:
                    if call.tool_call_id in active:
                        continue
                    is_new = call.tool_call_id not in first_seen
                    first = first_seen.setdefault(call.tool_call_id, now)
                    if is_new:
                        _logger.debug(
                            "cursor elicitation: pending %s detected, settling %.1fs; "
                            "session=%s tool_call_id=%s",
                            call.tool_name,
                            settle_s,
                            session_id,
                            call.tool_call_id.splitlines()[0],
                        )
                    if now - first < settle_s:
                        continue
                    elicitation_id = cursor_tool_call_elicitation_id(session_id, call.tool_call_id)
                    _logger.debug(
                        "cursor elicitation: surfacing %s; session=%s tool_call_id=%s",
                        call.tool_name,
                        session_id,
                        call.tool_call_id.splitlines()[0],
                    )
                    if _is_question_call(call):
                        coro = _run_one_question(
                            client,
                            session_id=session_id,
                            bridge_dir=bridge_dir,
                            call=call,
                            elicitation_id=elicitation_id,
                        )
                    else:
                        coro = _run_one_approval(
                            client,
                            session_id=session_id,
                            bridge_dir=bridge_dir,
                            prompt=_prompt_from_pending(call),
                            elicitation_id=elicitation_id,
                        )
                    task = asyncio.create_task(coro, name=f"cursor-approval-{elicitation_id}")
                    active[call.tool_call_id] = {
                        "elicitation_id": elicitation_id,
                        "task": task,
                    }
                    first_seen.pop(call.tool_call_id, None)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "cursor transcript elicitation poll failed; session=%s bridge_dir=%s",
                    session_id,
                    bridge_dir,
                )
            await asyncio.sleep(poll_interval_s)
