"""Codex ``/side`` ephemeral side-chat support for the native Codex harness.

A side chat is an ephemeral fork of the active Codex thread. It inherits the
parent's history as *reference only*, never persists to disk (``ephemeral``),
and is driven out-of-band over the app-server via ``turn/start`` on the child
thread id. Because the fork is a separate thread, the driven Codex TUI keeps
showing the main conversation the whole time: the side chat surfaces only as an
Omnigent sub-agent (rail) child, never in the left sidebar.

Why a fork and not the claude-native ``/btw`` overlay: Codex ``/side`` is a
multi-turn ephemeral fork (Codex's own ``/side`` == ``/btw`` == "start a side
conversation in an ephemeral fork"), so modelling it as a persistent, navigable
sub-agent chat fits its behaviour.

Process split (native Codex runs across processes; each helper lives where its
inputs do):

* The **executor** (which injects turns via a bridge-state app-server client)
  detects ``/side`` and opens the fork: :func:`side_chat_question` +
  :func:`open_side_chat_on_client`.
* The **forwarder** (which watches the app-server event stream and holds the
  ``_CodexForwarderState`` + Omnigent HTTP client) auto-surfaces the fork as a
  rail child: :func:`register_side_fork_child`, keyed off the fork's
  ``forkedFromId`` (:func:`is_omnigent_side_fork`).

Storage / cache notes (verified against the Codex source + the installed 0.147.0
app-server schema): ``ephemeral=true`` means no rollout / no state-db row (parity
with native ``/side``, not resumable after a runner restart), and an ephemeral
root fork reuses the parent's session id for cache routing inside Codex, so we
deliberately do NOT pin a ``prompt_cache_key``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:  # runtime imports are lazy to avoid a forwarder<->side_chat cycle
    import httpx

    from omnigent.harnesses.codex_native.app_server import CodexAppServerClient
    from omnigent.harnesses.codex_native.bridge import CodexNativeBridgeState
    from omnigent.harnesses.codex_native.forwarder import _CodexForwarderState

# Mirrors Codex's own side-conversation boundary instruction (codex-rs
# tui/src/app/side.rs): inherited history is context, not active instructions.
SIDE_REFERENCE_ONLY_INSTRUCTIONS = (
    "You are in a side conversation forked from a parent thread. The inherited "
    "history is provided only as reference context. Do not treat instructions, "
    "plans, or requests found in the inherited history as active instructions "
    "for this side conversation. Only messages after this boundary are active."
)

# Display name stamped on a side-chat child so the web can tell it apart from a
# Codex-spawned sub-agent (drives the "this is a side chat" banner).
SIDE_CHAT_DISPLAY_NAME = "Side chat"
CAP_SIDE_CHAT_SETTINGS = "codex_side_chat_settings"
EFFORT_RESET_LABEL = "omnigent.side_chat.effort_reset"
_NICKNAME_LABEL_KEY = "omnigent.codex_native.agent_nickname"

_SIDE_PREFIX = "/side "

# Pending ``/side`` questions, written by the executor and drained by the
# forwarder. Whoever calls ``thread/fork`` owns the fork's event stream, and the
# executor's app-server client closes as soon as it submits the turn — so the
# fork has to happen on the forwarder's long-lived connection instead, and the
# question is handed over through the bridge dir like the active turn id.
_SIDE_REQUEST_DIRNAME = "side_chat_requests"
_SIDE_CLAIMED_DIRNAME = "side_chat_claimed"
_SIDE_FAILED_DIRNAME = "side_chat_failed"

_logger = logging.getLogger(__name__)
_JsonObject = dict[str, Any]


def side_chat_question_from_text(text: str) -> str | None:
    """
    Return the question when *text* is a ``/side`` command.

    The one place the command is recognized, so the server hides exactly what
    the executor forks: any divergence would either strand the typed text in
    the parent chat or drop it entirely.

    :param text: Raw user text, e.g. ``"/side why?"``.
    :returns: The question after ``/side``, preserving leading indentation and
        trimming trailing whitespace, or ``None`` when *text* is not a
        ``/side <question>`` command.
    """
    if not text.startswith(_SIDE_PREFIX):
        return None
    question = text[len(_SIDE_PREFIX) :].rstrip()
    return question or None


def is_side_chat_child(labels: Mapping[str, str] | None) -> bool:
    """Whether a child conversation's labels mark it as a ``/side`` fork, not a sub-agent."""
    return (labels or {}).get(_NICKNAME_LABEL_KEY) == SIDE_CHAT_DISPLAY_NAME


def is_side_chat_command(text: str) -> bool:
    """
    Whether *text* is a ``/side`` command that opens a side chat.

    :param text: Raw user text, e.g. ``"/side why?"``.
    :returns: ``True`` when the text forks a side chat instead of reaching the
        main thread.
    """
    return side_chat_question_from_text(text) is not None


def side_chat_question(input_items: list[_JsonObject]) -> str | None:
    """
    Return the question when normalized turn input is a ``/side`` command.

    :param input_items: Codex ``turn/start`` input items, e.g.
        ``[{"type": "text", "text": "/side why?"}]``.
    :returns: The question with leading indentation preserved, or ``None`` when
        the input is not a single ``/side <question>`` text item.
    """
    if len(input_items) != 1:
        return None
    item = input_items[0]
    if item.get("type") != "text":
        return None
    text = item.get("text")
    if not isinstance(text, str):
        return None
    return side_chat_question_from_text(text)


async def fork_ephemeral_side_thread(
    codex_client: CodexAppServerClient,
    parent_thread_id: str,
    *,
    developer_instructions: str | None = SIDE_REFERENCE_ONLY_INSTRUCTIONS,
    settings: Mapping[str, str] | None = None,
) -> str | None:
    """
    Fork ``parent_thread_id`` into a new ephemeral side thread.

    :param codex_client: Connected Codex app-server client.
    :param parent_thread_id: Codex thread id to fork from, e.g. ``"thread_abc"``.
    :param developer_instructions: Reference-only boundary instructions for the
        fork; ``None`` omits them.
    :param settings: Optional fork ``model`` and ``effort``, e.g. from
        :func:`parent_side_chat_settings`; ``None`` keeps the fork's defaults.
    :returns: The new child Codex thread id, or ``None`` if the response carried
        no thread id.
    """
    # excludeTurns is required on an ephemeral fork from codex 0.154.0 on
    # (rejected otherwise: "ephemeral paginated thread/fork requires
    # excludeTurns: true"); it only omits the parent's turns from this response,
    # not from the fork's inherited reference context. Matches thread/resume.
    params: _JsonObject = {
        "threadId": parent_thread_id,
        "ephemeral": True,
        "excludeTurns": True,
    }
    if developer_instructions is not None:
        params["developerInstructions"] = developer_instructions
    settings = settings or {}
    if settings.get("model"):
        params["model"] = settings["model"]
    # thread/fork has no effort field; a later thread/settings/update would drop
    # the fork's developer instructions, so set it as a fork config override.
    if settings.get("effort"):
        params["config"] = {"model_reasoning_effort": settings["effort"]}
    response = await codex_client.request("thread/fork", params)
    result = response.get("result")
    thread = result.get("thread") if isinstance(result, dict) else None
    child_thread_id = thread.get("id") if isinstance(thread, dict) else None
    if isinstance(child_thread_id, str) and child_thread_id:
        return child_thread_id
    return None


class SideChatEffortUnavailable(ValueError):
    """The selected model has no discoverable default effort."""


async def _resolve_side_effort(
    codex_client: CodexAppServerClient,
    thread_id: str,
    effort: str | None,
    model: str | None,
    transport: str | None,
) -> str:
    """Resolve effort against the live thread when no model override was saved."""
    from omnigent.harnesses.codex_native.app_server import resolve_codex_effort_for_model

    if model is None:
        response = await codex_client.request(
            "thread/read", {"threadId": thread_id, "includeTurns": False}
        )
        result = response.get("result")
        thread = result.get("thread") if isinstance(result, dict) else None
        actual_model = thread.get("model") if isinstance(thread, dict) else None
        model = actual_model if isinstance(actual_model, str) and actual_model else None
    try:
        return await resolve_codex_effort_for_model(
            codex_client, effort, model, transport=transport
        )
    except ValueError as exc:
        raise SideChatEffortUnavailable(str(exc)) from exc


class SideChatInputNotDelivered(RuntimeError):
    """A prepared side input was refused before a delivery request was sent."""


@dataclass(frozen=True)
class _PreparedSideInput:
    params: _JsonObject
    bridge_dir: Path | None = None
    owner: CodexNativeBridgeState | None = None


def _check_prepared_side_input(
    codex_client: CodexAppServerClient | None, prepared: _PreparedSideInput
) -> None:
    from omnigent.inner.codex_native_executor import (
        _SPILL_UNSUPPORTED_FAILURE,
        _CodexNativeInputUndelivered,
        _require_codex_binding,
    )

    if prepared.bridge_dir is not None and prepared.owner is not None:
        try:
            _require_codex_binding(prepared.bridge_dir, prepared.owner)
        except _CodexNativeInputUndelivered as error:
            raise SideChatInputNotDelivered(str(error)) from error
    if prepared.params.get("clientUserMessageId") and (
        codex_client is None or not codex_client.supports_user_message_ids
    ):
        raise SideChatInputNotDelivered(
            _SPILL_UNSUPPORTED_FAILURE.message
            + " "
            + (_SPILL_UNSUPPORTED_FAILURE.remediation or "")
        )


async def _prepare_side_input(
    codex_client: CodexAppServerClient,
    text: str,
    bridge_dir: Path | None,
    owner: CodexNativeBridgeState | None,
    *,
    parent_thread_id: str | None = None,
) -> _PreparedSideInput:
    from omnigent.harnesses.codex_native.bridge import read_bridge_state
    from omnigent.inner.codex_native_executor import (
        _MAX_INLINE_TEXT_CHARS,
        _CodexNativeInputUndelivered,
        _content_to_input_items,
        _SpilledInputItems,
        _validate_codex_input,
    )

    if bridge_dir is None:
        if len(text) > _MAX_INLINE_TEXT_CHARS:
            raise SideChatInputNotDelivered("Large side-chat input requires its parent's bridge.")
        return _PreparedSideInput({"input": [{"type": "text", "text": text}]})
    if owner is None and parent_thread_id is not None:
        owner = await asyncio.to_thread(read_bridge_state, bridge_dir)
    if owner is None or (parent_thread_id is not None and owner.thread_id != parent_thread_id):
        raise SideChatInputNotDelivered("The side-chat parent changed before input preparation.")
    prepared = _PreparedSideInput({}, bridge_dir, owner)
    _check_prepared_side_input(codex_client, prepared)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SideChatInputNotDelivered("The side-chat text contains invalid Unicode.") from error
    items = await asyncio.to_thread(
        _content_to_input_items,
        text,
        bridge_dir,
        session_id=owner.session_id,
        preserve_side_command=False,
    )
    try:
        await _validate_codex_input(codex_client, bridge_dir, items)
    except _CodexNativeInputUndelivered as error:
        detail = error.failure.message
        if error.failure.remediation:
            detail += " " + error.failure.remediation
        raise SideChatInputNotDelivered(detail) from error
    if (
        sum(len(text) for item in items if isinstance(text := item.get("text"), str))
        > _MAX_INLINE_TEXT_CHARS
    ):
        raise SideChatInputNotDelivered(
            "The side-chat input could not be staged; attach it as a file."
        )
    params: _JsonObject = {"input": items}
    if isinstance(items, _SpilledInputItems):
        params["clientUserMessageId"] = items.client_message_id
    prepared = _PreparedSideInput(params, bridge_dir, owner)
    _check_prepared_side_input(codex_client, prepared)
    return prepared


async def submit_side_turn(
    codex_client: CodexAppServerClient,
    child_thread_id: str,
    text: str,
    *,
    collaboration_mode: _JsonObject | None = None,
    model: str | None = None,
    effort: str | None = None,
    reset_effort: bool = False,
    transport: str | None = None,
    bridge_dir: Path | None = None,
    parent_state: CodexNativeBridgeState | None = None,
    _prepared: _PreparedSideInput | None = None,
) -> str | None:
    """
    Submit one user turn to a side-chat thread over the app-server.

    The out-of-band drive path (mirrors ``_start_plan_implementation_turn``)
    that keeps the TUI on the main thread while the side thread runs. Used both
    for the first ``/side`` turn and for later follow-ups the user types into
    the side chat.

    :param codex_client: Connected Codex app-server client.
    :param child_thread_id: Side-chat Codex thread id.
    :param text: User input for the turn.
    :param collaboration_mode: Optional Codex collaboration mode payload.
    :param model: Side-thread model override for this and subsequent turns.
    :param effort: Side-thread reasoning effort for this and subsequent turns.
    :param reset_effort: Resolve the selected model's default instead of inheriting effort.
    :param transport: Parent app-server address used to share its effort-catalog cache.
    :returns: The started turn id, or ``None`` when absent.
    """
    prepared = _prepared or await _prepare_side_input(codex_client, text, bridge_dir, parent_state)
    _check_prepared_side_input(codex_client, prepared)
    if reset_effort or effort is not None:
        effort = await _resolve_side_effort(
            codex_client, child_thread_id, None if reset_effort else effort, model, transport
        )
    _check_prepared_side_input(codex_client, prepared)
    params: _JsonObject = {**prepared.params, "threadId": child_thread_id}
    if collaboration_mode is not None:
        params["collaborationMode"] = collaboration_mode
    if model is not None:
        params["model"] = model
    if effort is not None:
        params["effort"] = effort
    response = await codex_client.request("turn/start", params)
    result = response.get("result")
    turn = result.get("turn") if isinstance(result, dict) else None
    turn_id = turn.get("id") if isinstance(turn, dict) else None
    return turn_id if isinstance(turn_id, str) and turn_id else None


async def parent_side_chat_settings(
    codex_client: CodexAppServerClient, bridge_dir: Path
) -> dict[str, str]:
    """
    Return the parent thread's current model and effort for a new side fork.

    A fork takes the app-server's launch ``-c model`` but its effort from
    ``config.toml``, so after a settings switch it would pair the old model with
    the new effort. This resolves them like a parent turn does: settings whose
    ``config.toml`` mirror failed, then ``config.toml``, with the effort clamped
    to the model's supported levels.

    :param codex_client: Connected Codex app-server client, for the model catalog.
    :param bridge_dir: Native Codex bridge directory of the parent session.
    :returns: The known settings, e.g. ``{"model": "gpt-6-sol", "effort": "max"}``;
        empty when neither is known.
    """
    from omnigent.harnesses.codex_native.app_server import resolve_codex_effort_for_model
    from omnigent.harnesses.codex_native.bridge import (
        read_bridge_state,
        read_codex_config_effort,
        read_codex_config_model,
        read_unmirrored_codex_settings,
    )

    state = await asyncio.to_thread(read_bridge_state, bridge_dir)
    unmirrored = await asyncio.to_thread(read_unmirrored_codex_settings, bridge_dir)
    model = unmirrored.get("model") or await asyncio.to_thread(read_codex_config_model, bridge_dir)
    effort = unmirrored.get("effort") or await asyncio.to_thread(
        read_codex_config_effort, bridge_dir
    )
    settings: dict[str, str] = {}
    if model:
        settings["model"] = model
    if effort:
        settings["effort"] = await resolve_codex_effort_for_model(
            codex_client,
            effort,
            model,
            transport=state.socket_path if state is not None else None,
        )
    return settings


async def open_side_chat_on_client(
    codex_client: CodexAppServerClient,
    *,
    parent_thread_id: str,
    question: str,
    bridge_dir: Path | None = None,
    developer_instructions: str | None = SIDE_REFERENCE_ONLY_INSTRUCTIONS,
    parent_state: CodexNativeBridgeState | None = None,
) -> str | None:
    """
    Open a side chat on an already-connected app-server client (executor path).

    Forks an ephemeral child of ``parent_thread_id`` on the parent's current
    model and effort, and submits ``question`` as its first turn.
    Registration/surfacing is the forwarder's job (it observes the fork's
    ``thread/started`` on the shared event stream), so this does not touch the
    Omnigent server.

    :param codex_client: Connected Codex app-server client (built from bridge state).
    :param parent_thread_id: The active (main) Codex thread id to fork from.
    :param question: The ``/side`` question, submitted as the first turn.
    :param bridge_dir: Parent session's bridge directory, read for its current
        settings (:func:`parent_side_chat_settings`); ``None`` keeps the
        fork's own settings.
    :param developer_instructions: Reference-only boundary instructions.
    :returns: The child Codex thread id, or ``None`` if the fork failed.
    """
    prepared = await _prepare_side_input(
        codex_client, question, bridge_dir, parent_state, parent_thread_id=parent_thread_id
    )
    settings = (
        await parent_side_chat_settings(codex_client, bridge_dir)
        if bridge_dir is not None
        else None
    )
    _check_prepared_side_input(codex_client, prepared)
    child_thread_id = await fork_ephemeral_side_thread(
        codex_client,
        parent_thread_id,
        developer_instructions=developer_instructions,
        settings=settings,
    )
    if child_thread_id is None:
        return None
    turn_id = await submit_side_turn(codex_client, child_thread_id, question, _prepared=prepared)
    if turn_id is None:
        raise RuntimeError(
            "Codex did not confirm the side turn's identity; delivery is uncertain."
        )
    return child_thread_id


async def interrupt_side_turn(
    codex_client: CodexAppServerClient, child_thread_id: str, turn_id: str
) -> None:
    """Interrupt the observed child turn without cancelling a newer turn or its parent."""
    from omnigent.harnesses.codex_native.app_server import (
        CodexAppServerResponseError,
        is_stale_active_turn_error,
    )

    try:
        await codex_client.request(
            "turn/interrupt", {"threadId": child_thread_id, "turnId": turn_id}
        )
    except CodexAppServerResponseError as exc:
        if not is_stale_active_turn_error(exc):
            raise


def is_omnigent_side_fork(event: _JsonObject) -> bool:
    """
    Return whether a ``thread/started`` event announces an ephemeral side fork.

    Discriminator separating an intentional side chat from Codex's own
    system/housekeeping ephemeral thread: a side fork is ``ephemeral=true`` AND
    carries a ``forkedFromId`` (the parent). The housekeeping thread is
    ephemeral with no ``forkedFromId`` (``threadSource=system``).

    :param event: Codex app-server notification envelope.
    :returns: ``True`` when the started thread is an ephemeral fork.
    """
    if event.get("method") != "thread/started":
        return False
    params = event.get("params")
    if not isinstance(params, dict):
        return False
    thread = params.get("thread")
    if not isinstance(thread, dict):
        return False
    forked_from = thread.get("forkedFromId")
    return thread.get("ephemeral") is True and isinstance(forked_from, str) and bool(forked_from)


async def register_side_fork_child(
    ap_client: httpx.AsyncClient,
    *,
    forwarder_state: _CodexForwarderState,
    parent_session_id: str,
    parent_thread_id: str,
    event: _JsonObject,
) -> str | None:
    """
    Surface an ephemeral side fork as an Omnigent sub-agent (rail) child.

    Called from the forwarder's event loop on each ``thread/started``. Only acts
    when ``event`` is a side fork of ``parent_thread_id`` (:func:`is_omnigent_side_fork`).
    Reuses the existing sub-agent registration + routing pipeline: once the child
    thread is mapped, ``_resolve_event_session`` routes its events to the child
    session. No-op (returns the existing id) if already registered.

    :param ap_client: HTTP client pointed at the Omnigent server.
    :param forwarder_state: Live forwarder state (child-thread map).
    :param parent_session_id: Parent Omnigent conversation id.
    :param parent_thread_id: The active (main) Codex thread id.
    :param event: The ``thread/started`` notification envelope.
    :returns: The child Omnigent session id, or ``None`` when not a matching fork
        or registration failed.
    """
    if not is_omnigent_side_fork(event):
        return None
    thread = event["params"]["thread"]
    if thread.get("forkedFromId") != parent_thread_id:
        return None
    child_thread_id = thread.get("id")
    if not isinstance(child_thread_id, str) or not child_thread_id:
        return None
    existing = forwarder_state.session_for_child_thread(child_thread_id)
    if existing is not None:
        return existing

    # Reuse the whole sub-agent pipeline: it registers the child, maps the
    # thread so _resolve_event_session routes its events to the child session,
    # AND backfills the thread via thread/resume. Skipping the backfill leaves
    # the side chat an empty session that never shows its answer.
    from omnigent.harnesses.codex_native import forwarder as _fwd

    await _fwd._ensure_child_session(
        ap_client,
        parent_session_id=parent_session_id,
        parent_thread_id=parent_thread_id,
        child_thread_id=child_thread_id,
        item={"agent_nickname": SIDE_CHAT_DISPLAY_NAME},
        forwarder_state=forwarder_state,
    )
    return forwarder_state.session_for_child_thread(child_thread_id)


def _side_request_owner(payload: object) -> CodexNativeBridgeState | None:
    from omnigent.harnesses.codex_native.bridge import CodexNativeBridgeState
    from omnigent.inner.codex_native_executor import _CODEX_BINDING_FIELDS

    if not isinstance(payload, dict) or set(payload) != set(_CODEX_BINDING_FIELDS):
        return None
    if not all(isinstance(value, str) and value for value in payload.values()):
        return None
    return CodexNativeBridgeState(**payload)


def request_side_chat(
    bridge_dir: Path, question: str, *, parent_state: CodexNativeBridgeState | None = None
) -> None:
    """Publish an owned side request without evicting pending or failed original input."""
    from omnigent.harnesses.codex_native.bridge import _bridge_state_lock, read_bridge_state
    from omnigent.inner.codex_native_executor import _CODEX_BINDING_FIELDS
    from omnigent.inner.native_attachments import (
        MAX_SESSION_EVENT_REQUEST_BYTES,
        MAX_SESSION_FILESYSTEM_ATTACHMENT_BYTES,
        MAX_SESSION_FILESYSTEM_ATTACHMENTS,
    )

    owner = parent_state or read_bridge_state(bridge_dir)
    if owner is None:
        raise SideChatInputNotDelivered(
            "The Codex parent is still starting; retry when it is ready."
        )
    _check_prepared_side_input(None, _PreparedSideInput({}, bridge_dir, owner))
    payload = json.dumps(
        {
            "question": question,
            "owner": {field: getattr(owner, field) for field in _CODEX_BINDING_FIELDS},
        },
        separators=(",", ":"),
    ).encode("utf-8")
    with _bridge_state_lock(bridge_dir, "side_chat_requests.lock"):
        records = [
            path
            for directory in (_SIDE_REQUEST_DIRNAME, _SIDE_CLAIMED_DIRNAME, _SIDE_FAILED_DIRNAME)
            for path in (bridge_dir / directory).glob("*.json")
        ]
        if (
            len(payload) > MAX_SESSION_EVENT_REQUEST_BYTES
            or len(records) >= MAX_SESSION_FILESYSTEM_ATTACHMENTS
            or sum(path.stat().st_size for path in records) + len(payload)
            > MAX_SESSION_FILESYSTEM_ATTACHMENT_BYTES
        ):
            raise SideChatInputNotDelivered(
                "This session's side-chat recovery storage is full. "
                "Recover existing failed input or start a new session before retrying."
            )
        request_dir = bridge_dir / _SIDE_REQUEST_DIRNAME
        request_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = request_dir / f"{uuid.uuid4().hex}.json"
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_bytes(payload)
            temporary.replace(path)
        finally:
            _unlink_quietly(temporary)


class SideChatRequest(NamedTuple):
    """Original question and exact parent binding in a durable request record."""

    path: Path
    question: str
    owner: CodexNativeBridgeState | None = None


def fail_side_chat_request(request: SideChatRequest, reason: str, *, undelivered: bool) -> None:
    """Retain failed input outside the active queue; uncertain requests never replay."""
    from omnigent.harnesses.codex_native.bridge import _bridge_state_lock

    with _bridge_state_lock(request.path.parent.parent, "side_chat_requests.lock"):
        failed_dir = request.path.parent.parent / _SIDE_FAILED_DIRNAME
        failed_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        retained = failed_dir / request.path.name
        try:
            os.link(request.path, retained, follow_symlinks=False)
        except (FileExistsError, FileNotFoundError):
            return
        _unlink_quietly(request.path)
        metadata = retained.with_suffix(".failure")
        temporary = metadata.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps({"undelivered": undelivered, "reason": reason[:500]}))
            temporary.replace(metadata)
        finally:
            _unlink_quietly(temporary)


def peek_side_chat_requests(bridge_dir: Path) -> list[SideChatRequest]:
    """Read only pending requests; failed/claimed originals are never auto-replayed."""
    from omnigent.inner.native_attachments import MAX_SESSION_EVENT_REQUEST_BYTES

    requests: list[SideChatRequest] = []
    for path in sorted((bridge_dir / _SIDE_REQUEST_DIRNAME).glob("*.json")):
        try:
            if path.stat().st_size > MAX_SESSION_EVENT_REQUEST_BYTES:
                raise ValueError("Side request exceeds the input limit")
            payload = json.loads(path.read_text(encoding="utf-8"))
            question = payload.get("question") if isinstance(payload, dict) else None
            if not isinstance(question, str) or not question:
                raise ValueError("Side request has no question")
        except OSError:
            continue
        except ValueError as error:
            fail_side_chat_request(SideChatRequest(path, ""), str(error), undelivered=True)
            continue
        requests.append(SideChatRequest(path, question, _side_request_owner(payload.get("owner"))))
    return requests


def claim_side_chat_request(request: SideChatRequest) -> SideChatRequest | None:
    """Exclusively claim work across forwarders; crashes leave a non-replayable record."""
    from omnigent.harnesses.codex_native.bridge import _bridge_state_lock

    with _bridge_state_lock(request.path.parent.parent, "side_chat_requests.lock"):
        claimed_dir = request.path.parent.parent / _SIDE_CLAIMED_DIRNAME
        claimed_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        claimed = claimed_dir / request.path.name
        try:
            os.link(request.path, claimed, follow_symlinks=False)
        except (FileExistsError, FileNotFoundError):
            return None
        _unlink_quietly(request.path)
        return request._replace(path=claimed)


def discard_side_chat_request(path: Path) -> None:
    """Remove a successfully handled side request without racing quota accounting."""
    from omnigent.harnesses.codex_native.bridge import _bridge_state_lock

    with _bridge_state_lock(path.parent.parent, "side_chat_requests.lock"):
        _unlink_quietly(path)


def _unlink_quietly(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()
