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

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # runtime imports are lazy to avoid a forwarder<->side_chat cycle
    import httpx

    from omnigent.harnesses.codex_native.app_server import CodexAppServerClient
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
SIDE_CHAT_SUBAGENT_NAME = "side-chat"

_SIDE_PREFIX = "/side "
_JsonObject = dict[str, Any]


def side_chat_question(input_items: list[_JsonObject]) -> str | None:
    """
    Return the question when normalized turn input is a ``/side`` command.

    :param input_items: Codex ``turn/start`` input items, e.g.
        ``[{"type": "text", "text": "/side why?"}]``.
    :returns: The trimmed question after ``/side``, or ``None`` when the input
        is not a single ``/side <question>`` text item.
    """
    if len(input_items) != 1:
        return None
    item = input_items[0]
    if item.get("type") != "text":
        return None
    text = item.get("text")
    if not isinstance(text, str) or not text.startswith(_SIDE_PREFIX):
        return None
    question = text[len(_SIDE_PREFIX) :].strip()
    return question or None


async def fork_ephemeral_side_thread(
    codex_client: CodexAppServerClient,
    parent_thread_id: str,
    *,
    developer_instructions: str | None = SIDE_REFERENCE_ONLY_INSTRUCTIONS,
) -> str | None:
    """
    Fork ``parent_thread_id`` into a new ephemeral side thread.

    :param codex_client: Connected Codex app-server client.
    :param parent_thread_id: Codex thread id to fork from, e.g. ``"thread_abc"``.
    :param developer_instructions: Reference-only boundary instructions for the
        fork; ``None`` omits them.
    :returns: The new child Codex thread id, or ``None`` if the response carried
        no thread id.
    """
    params: _JsonObject = {"threadId": parent_thread_id, "ephemeral": True}
    if developer_instructions is not None:
        params["developerInstructions"] = developer_instructions
    response = await codex_client.request("thread/fork", params)
    result = response.get("result")
    thread = result.get("thread") if isinstance(result, dict) else None
    child_thread_id = thread.get("id") if isinstance(thread, dict) else None
    if isinstance(child_thread_id, str) and child_thread_id:
        return child_thread_id
    return None


async def submit_side_turn(
    codex_client: CodexAppServerClient,
    child_thread_id: str,
    text: str,
    *,
    collaboration_mode: _JsonObject | None = None,
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
    :returns: The started turn id, or ``None`` when absent.
    """
    params: _JsonObject = {
        "threadId": child_thread_id,
        "input": [{"type": "text", "text": text}],
    }
    if collaboration_mode is not None:
        params["collaborationMode"] = collaboration_mode
    response = await codex_client.request("turn/start", params)
    result = response.get("result")
    turn = result.get("turn") if isinstance(result, dict) else None
    turn_id = turn.get("id") if isinstance(turn, dict) else None
    return turn_id if isinstance(turn_id, str) and turn_id else None


async def open_side_chat_on_client(
    codex_client: CodexAppServerClient,
    *,
    parent_thread_id: str,
    question: str,
    developer_instructions: str | None = SIDE_REFERENCE_ONLY_INSTRUCTIONS,
) -> str | None:
    """
    Open a side chat on an already-connected app-server client (executor path).

    Forks an ephemeral child of ``parent_thread_id`` and submits ``question`` as
    its first turn. Registration/surfacing is the forwarder's job (it observes
    the fork's ``thread/started`` on the shared event stream), so this does not
    touch the Omnigent server.

    :param codex_client: Connected Codex app-server client (built from bridge state).
    :param parent_thread_id: The active (main) Codex thread id to fork from.
    :param question: The ``/side`` question, submitted as the first turn.
    :param developer_instructions: Reference-only boundary instructions.
    :returns: The child Codex thread id, or ``None`` if the fork failed.
    """
    child_thread_id = await fork_ephemeral_side_thread(
        codex_client, parent_thread_id, developer_instructions=developer_instructions
    )
    if child_thread_id is None:
        return None
    await submit_side_turn(codex_client, child_thread_id, question)
    return child_thread_id


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

    # ponytail: reuse the proven sub-agent registration pipeline
    # (_register_child_session POSTs external_codex_subagent_start; note_child_thread
    # makes _resolve_event_session route the fork's events to the child session).
    from omnigent.harnesses.codex_native import forwarder as _fwd

    child_session_id = await _fwd._register_child_session(
        ap_client,
        parent_session_id=parent_session_id,
        parent_thread_id=parent_thread_id,
        child_thread_id=child_thread_id,
        item={"sub_agent_name": SIDE_CHAT_SUBAGENT_NAME},
    )
    if child_session_id is None:
        return None
    forwarder_state.note_child_thread(child_thread_id, child_session_id)
    return child_session_id
