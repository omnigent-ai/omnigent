"""Mirror a native Devin TUI session into its Omnigent conversation.

Devin's lifecycle hooks are a complete, ordered event stream for a turn, and
they carry correlation ids the other native harnesses have to reconstruct:

* ``prompt_id`` identifies the turn, so it becomes the Omnigent ``response_id``
  directly instead of being inferred from message ordering.
* ``tool_use_id`` pairs ``PreToolUse`` with its ``PostToolUse``, so tool cards
  and their output match up without heuristics.
* ``Stop`` carries ``last_assistant_message``, so the assistant bubble needs no
  transcript parsing.

So this forwarder tails the bridge's ``hooks.jsonl`` (written by
:mod:`omnigent.harnesses.devin_native.hook`) rather than scraping the pane.
The one thing hooks do not carry is reasoning text and token counts; those come
from the ATIF transcript Devin rewrites after every turn (``--export``), read
opportunistically on each turn-end edge.

Sub-agents are the one thing neither the hooks nor the export attribute: Devin
runs each ``run_subagent`` delegate as its own chain inside the parent session's
message forest. So on each turn-end this forwarder reads Devin's own session
store (``sessions.db``, read-only) and mirrors every newly-completed sub-agent's
reconstructed transcript into an Omnigent child session — see
:mod:`omnigent.harnesses.devin_native.subagents`.

The read offset and cumulative usage are persisted into the bridge dir so a
supervisor restart resumes without re-posting items or double-counting tokens.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from omnigent.harnesses.devin_native.bridge import (
    export_path,
    iter_hook_events,
    write_forwarder_ready,
)
from omnigent.harnesses.devin_native.subagents import (
    completed_agent_ids,
    devin_sessions_db_path,
    load_message_nodes,
    parse_spawned_agent_id,
    reconstruct_transcript_nodes,
    transcript_items,
)
from omnigent.native._native_post_delivery import post_external_session_status
from omnigent.util.json_types import JsonObject as _JsonObject

_logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_S = 0.4
_POST_TIMEOUT_S = 30.0
_STATE_FILE = "devin_forwarder_state.json"
#: Backoff bounds for the supervisor when the forward loop dies.
_SUPERVISE_INITIAL_BACKOFF_S = 1.0
_SUPERVISE_MAX_BACKOFF_S = 30.0

_SESSION_START = "SessionStart"
_USER_PROMPT_SUBMIT = "UserPromptSubmit"
_PRE_TOOL_USE = "PreToolUse"
_POST_TOOL_USE = "PostToolUse"
_STOP = "Stop"
_POST_COMPACTION = "PostCompaction"
_SESSION_END = "SessionEnd"


@dataclass
class _ForwardState:
    """Persisted forwarder cursor and cumulative usage."""

    hooks_offset: int = 0
    devin_session_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    #: ``run_subagent`` spawns seen this session, keyed by Devin ``agent_id`` ->
    #: ``{"task", "title", "tool_use_id", "mirrored"}``. Persisted so a
    #: supervisor restart does not re-mirror an already-posted sub-agent.
    subagents: dict[str, _JsonObject] = field(default_factory=dict)

    def as_dict(self) -> _JsonObject:
        """Return a JSON-serializable view for persistence."""
        return {
            "hooks_offset": self.hooks_offset,
            "devin_session_id": self.devin_session_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "subagents": self.subagents,
        }


@dataclass
class _TurnState:
    """The currently-open assistant turn, keyed by Devin's ``prompt_id``."""

    prompt_id: str | None = None
    live: bool = False
    #: Tool ids seen this turn, so a ``PostToolUse`` without its ``PreToolUse``
    #: (a hook that failed to record) still posts an output card.
    seen_tool_ids: set[str] = field(default_factory=set)

    @property
    def response_id(self) -> str | None:
        """Return the Omnigent turn id for the open Devin turn."""
        return f"devin:turn:{self.prompt_id}" if self.prompt_id else None

    def reset(self) -> None:
        """Forget the open turn."""
        self.prompt_id = None
        self.live = False
        self.seen_tool_ids.clear()


def _read_state(bridge_dir: Path) -> _ForwardState:
    """Load the persisted forwarder state, defaulting to a fresh cursor."""
    try:
        raw = (bridge_dir / _STATE_FILE).read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, ValueError):
        return _ForwardState()
    if not isinstance(parsed, dict):
        return _ForwardState()
    state = _ForwardState()
    offset = parsed.get("hooks_offset")
    if isinstance(offset, int) and offset >= 0:
        state.hooks_offset = offset
    devin_session_id = parsed.get("devin_session_id")
    if isinstance(devin_session_id, str) and devin_session_id:
        state.devin_session_id = devin_session_id
    for key in ("input_tokens", "output_tokens", "cached_tokens"):
        value = parsed.get(key)
        if isinstance(value, int) and value >= 0:
            setattr(state, key, value)
    subagents = parsed.get("subagents")
    if isinstance(subagents, dict):
        for agent_id, info in subagents.items():
            if isinstance(agent_id, str) and isinstance(info, dict):
                state.subagents[agent_id] = info
    return state


def _write_state(bridge_dir: Path, state: _ForwardState) -> None:
    """Persist the forwarder cursor atomically."""
    path = bridge_dir / _STATE_FILE
    tmp = path.with_suffix(".json.tmp")
    try:
        bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state.as_dict()), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        _logger.warning("devin-native: could not persist forwarder state: %s", exc)


# ---------------------------------------------------------------------------
# Event -> conversation item
# ---------------------------------------------------------------------------


def _tool_output_text(response: object) -> str:
    """Flatten Devin's ``tool_response`` into display text for a result card."""
    if isinstance(response, str):
        return response
    if not isinstance(response, dict):
        return "" if response is None else json.dumps(response, ensure_ascii=False)
    output = response.get("output")
    if isinstance(output, str) and output:
        return output
    error = response.get("error")
    if isinstance(error, str) and error:
        return error
    if response.get("success") is True:
        return "(no output)"
    return json.dumps(response, ensure_ascii=False)


async def _post_item(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    item_type: str,
    item_data: _JsonObject,
    response_id: str | None,
) -> None:
    """POST one ``external_conversation_item`` event."""
    data: _JsonObject = {"item_type": item_type, "item_data": item_data}
    if response_id:
        data["response_id"] = response_id
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_conversation_item", "data": data},
    )
    resp.raise_for_status()


async def _persist_devin_session_id(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    devin_session_id: str,
) -> None:
    """Record Devin's session id so a later resume can reattach the TUI.

    This is what ``omnigent devin --resume <conversation>`` reads back to pass
    ``devin --resume <devin_session_id>``.
    """
    with contextlib.suppress(httpx.HTTPError):
        resp = await client.patch(
            f"/v1/sessions/{session_id}",
            json={"external_session_id": devin_session_id},
        )
        resp.raise_for_status()


def _read_export_metrics(bridge_dir: Path) -> tuple[_JsonObject | None, str | None]:
    """Return ``(final_metrics, model_name)`` from the ATIF export, if readable.

    Devin rewrites the export after each turn, so a partially-written file is
    normal — an unparseable read is not an error, just "no metrics this time".
    """
    try:
        raw = export_path(bridge_dir).read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, ValueError):
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    metrics = parsed.get("final_metrics")
    agent = parsed.get("agent")
    model = agent.get("model_name") if isinstance(agent, dict) else None
    return (
        metrics if isinstance(metrics, dict) else None,
        model if isinstance(model, str) and model else None,
    )


def _latest_reasoning(bridge_dir: Path, prompt_id: str | None) -> str | None:
    """Return the reasoning text for the most recent agent step, if any.

    Hooks carry no reasoning, so the ATIF transcript is the only source. Devin
    records ``reasoning_content`` on agent steps; the last one belongs to the
    turn that just ended.
    """
    del prompt_id  # ATIF steps carry no prompt id; the last agent step is ours.
    try:
        raw = export_path(bridge_dir).read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, ValueError):
        return None
    steps = parsed.get("steps") if isinstance(parsed, dict) else None
    if not isinstance(steps, list):
        return None
    for step in reversed(steps):
        if not isinstance(step, dict) or step.get("source") != "agent":
            continue
        reasoning = step.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            return reasoning.strip()
        return None
    return None


async def _post_usage(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    state: _ForwardState,
) -> None:
    """Post cumulative token usage from the ATIF export when it advanced.

    ATIF's ``final_metrics`` are already cumulative for the session, so they map
    straight onto the server's cumulative contract. Only post on an increase so
    a re-read of an unchanged export is a no-op.
    """
    metrics, model = _read_export_metrics(bridge_dir)
    if metrics is None:
        return
    prompt_tokens = metrics.get("total_prompt_tokens")
    completion_tokens = metrics.get("total_completion_tokens")
    cached_tokens = metrics.get("total_cached_tokens")
    new_input = prompt_tokens if isinstance(prompt_tokens, int) else state.input_tokens
    new_output = completion_tokens if isinstance(completion_tokens, int) else state.output_tokens
    new_cached = cached_tokens if isinstance(cached_tokens, int) else state.cached_tokens
    if (
        new_input <= state.input_tokens
        and new_output <= state.output_tokens
        and new_cached <= state.cached_tokens
    ):
        return
    state.input_tokens = max(new_input, state.input_tokens)
    state.output_tokens = max(new_output, state.output_tokens)
    state.cached_tokens = max(new_cached, state.cached_tokens)
    data: _JsonObject = {
        "cumulative_input_tokens": state.input_tokens,
        "cumulative_output_tokens": state.output_tokens,
        "cumulative_cache_read_input_tokens": state.cached_tokens,
    }
    if model:
        data["model"] = model
    with contextlib.suppress(httpx.HTTPError):
        resp = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "external_session_usage", "data": data},
        )
        resp.raise_for_status()


async def _close_turn(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    turn: _TurnState,
) -> None:
    """Post the idle status edge that ends an open turn."""
    if not turn.live:
        turn.reset()
        return
    with contextlib.suppress(httpx.HTTPError):
        await post_external_session_status(
            client,
            session_id=session_id,
            status="idle",
            response_id=turn.response_id,
        )
    turn.reset()


def _record_subagent_spawn(state: _ForwardState, payload: _JsonObject) -> None:
    """Note a ``run_subagent`` spawn from its ``PostToolUse`` payload.

    The spawned ``agent_id`` rides only the tool-result text; the ``task`` (which
    equals the sub-agent's first user message, so it keys the transcript chain)
    and ``title`` come from ``tool_input``. A spawn with no parseable id or task
    is ignored — it just will not be mirrored as a child.
    """
    response = payload.get("tool_response")
    output = response.get("output") if isinstance(response, Mapping) else None
    agent_id = parse_spawned_agent_id(output if isinstance(output, str) else None)
    if not agent_id or agent_id in state.subagents:
        return
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, Mapping):
        return
    task = tool_input.get("task")
    if not isinstance(task, str) or not task:
        return
    title = tool_input.get("title")
    tool_use_id = payload.get("tool_use_id")
    state.subagents[agent_id] = {
        "task": task,
        "title": title if isinstance(title, str) else "",
        "tool_use_id": tool_use_id if isinstance(tool_use_id, str) else "",
        "mirrored": False,
    }


async def _start_subagent_child(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    agent_id: str,
    title: str,
    tool_use_id: str,
) -> str | None:
    """Mint (or idempotently resolve) the Omnigent child session for a sub-agent.

    :returns: The child conversation id, or ``None`` if the server returned none
        (the mirror is then retried on a later turn-end).
    """
    data: _JsonObject = {"agent_id": agent_id}
    if title:
        data["title"] = title
    if tool_use_id:
        data["tool_use_id"] = tool_use_id
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_devin_subagent_start", "data": data},
    )
    resp.raise_for_status()
    body = resp.json()
    child_id = body.get("child_session_id") if isinstance(body, Mapping) else None
    return child_id if isinstance(child_id, str) and child_id else None


async def _mirror_completed_subagents(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    agent_name: str,
    state: _ForwardState,
) -> None:
    """Mirror every newly-completed sub-agent's transcript into a child session.

    Runs on each turn-end. Devin persists a background sub-agent's full chain in
    its session store and injects a ``<subagent_completion_notification>`` into
    the parent when it finishes, so once that notification is present the chain
    is complete and stable — reconstruct it and post it once.

    Best-effort per sub-agent: one child's POST failure is suppressed so it never
    kills the main mirror, and ``mirrored`` is set only on full success.

    # ponytail: a failure after the child is minted but mid-transcript re-posts
    # that sub-agent's items on the next turn-end (external_conversation_item is
    # not idempotent). Rare and cosmetic; add a per-item cursor if it bites.
    """
    pending = {aid: info for aid, info in state.subagents.items() if not info.get("mirrored")}
    if not pending or not state.devin_session_id:
        return
    db_path = devin_sessions_db_path(os.environ)
    if db_path is None:
        return
    nodes = load_message_nodes(db_path, state.devin_session_id)
    if not nodes:
        return
    completed: set[str] = set()
    for node in nodes:
        message = node.get("chat_message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, str) and "subagent_completion_notification" in content:
            completed.update(completed_agent_ids(content))
    for agent_id, info in pending.items():
        if agent_id not in completed:
            continue
        task = info.get("task")
        if not isinstance(task, str):
            continue
        chain = reconstruct_transcript_nodes(nodes, task)
        if not chain:
            continue
        try:
            child_id = await _start_subagent_child(
                client,
                session_id=session_id,
                agent_id=agent_id,
                title=str(info.get("title") or ""),
                tool_use_id=str(info.get("tool_use_id") or ""),
            )
            if child_id is None:
                continue
            response_id = f"devin:subagent:{agent_id}"
            for item_type, item_data in transcript_items(chain, agent_name):
                await _post_item(
                    client,
                    session_id=child_id,
                    item_type=item_type,
                    item_data=item_data,
                    response_id=response_id,
                )
            await post_external_session_status(
                client, session_id=child_id, status="idle", response_id=response_id
            )
        except httpx.HTTPError as exc:
            _logger.warning("devin-native: could not mirror sub-agent %s: %s", agent_id, exc)
            continue
        info["mirrored"] = True


async def _handle_event(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    agent_name: str,
    payload: _JsonObject,
    state: _ForwardState,
    turn: _TurnState,
) -> None:
    """Mirror one Devin hook event into the Omnigent conversation."""
    event = payload.get("hook_event_name")
    prompt_id = payload.get("prompt_id")
    prompt_id = prompt_id if isinstance(prompt_id, str) and prompt_id else None

    if event == _SESSION_START:
        devin_session_id = payload.get("session_id")
        if isinstance(devin_session_id, str) and devin_session_id:
            if devin_session_id != state.devin_session_id:
                state.devin_session_id = devin_session_id
                await _persist_devin_session_id(
                    client, session_id=session_id, devin_session_id=devin_session_id
                )
        return

    if event == _USER_PROMPT_SUBMIT:
        # A new prompt authoritatively closes any turn still open.
        if turn.prompt_id is not None and turn.prompt_id != prompt_id:
            await _close_turn(client, session_id=session_id, turn=turn)
        prompt = payload.get("prompt")
        turn.prompt_id = prompt_id
        if isinstance(prompt, str) and prompt.strip():
            await _post_item(
                client,
                session_id=session_id,
                item_type="message",
                item_data={
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                },
                response_id=turn.response_id,
            )
        return

    if event == _PRE_TOOL_USE:
        if turn.prompt_id is None:
            turn.prompt_id = prompt_id
        tool_name = payload.get("tool_name")
        tool_use_id = payload.get("tool_use_id")
        if not isinstance(tool_name, str) or not tool_name:
            return
        call_id = tool_use_id if isinstance(tool_use_id, str) and tool_use_id else tool_name
        turn.seen_tool_ids.add(call_id)
        turn.live = True
        await _post_item(
            client,
            session_id=session_id,
            item_type="function_call",
            item_data={
                "agent": agent_name,
                "name": tool_name,
                "arguments": json.dumps(payload.get("tool_input") or {}, ensure_ascii=False),
                "call_id": call_id,
            },
            response_id=turn.response_id,
        )
        return

    if event == _POST_TOOL_USE:
        if turn.prompt_id is None:
            turn.prompt_id = prompt_id
        tool_use_id = payload.get("tool_use_id")
        tool_name = payload.get("tool_name")
        call_id = (
            tool_use_id
            if isinstance(tool_use_id, str) and tool_use_id
            else (tool_name if isinstance(tool_name, str) else "")
        )
        if not call_id:
            return
        turn.live = True
        await _post_item(
            client,
            session_id=session_id,
            item_type="function_call_output",
            item_data={
                "call_id": call_id,
                "output": _tool_output_text(payload.get("tool_response")),
            },
            response_id=turn.response_id,
        )
        # A run_subagent spawn is also a normal tool card above; additionally
        # note it so its transcript can be mirrored as a child once it finishes.
        if tool_name == "run_subagent":
            _record_subagent_spawn(state, payload)
        return

    if event == _POST_COMPACTION:
        summary = payload.get("summary")
        if isinstance(summary, str) and summary.strip():
            await _post_item(
                client,
                session_id=session_id,
                item_type="message",
                item_data={
                    "role": "assistant",
                    "agent": agent_name,
                    "content": [
                        {
                            "type": "output_text",
                            "text": f"[Context compacted]\n\n{summary.strip()}",
                        }
                    ],
                },
                response_id=turn.response_id,
            )
        with contextlib.suppress(httpx.HTTPError):
            resp = await client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "external_compaction_status",
                    "data": {"status": "completed"},
                },
            )
            resp.raise_for_status()
        return

    if event == _STOP:
        if turn.prompt_id is None:
            turn.prompt_id = prompt_id
        message = payload.get("last_assistant_message")
        reasoning = _latest_reasoning(bridge_dir, turn.prompt_id)
        if reasoning:
            await _post_item(
                client,
                session_id=session_id,
                item_type="reasoning",
                item_data={
                    "agent": agent_name,
                    "content": [{"type": "reasoning_text", "text": reasoning}],
                },
                response_id=turn.response_id,
            )
        if isinstance(message, str) and message.strip():
            turn.live = True
            await _post_item(
                client,
                session_id=session_id,
                item_type="message",
                item_data={
                    "role": "assistant",
                    "agent": agent_name,
                    "content": [{"type": "output_text", "text": message}],
                },
                response_id=turn.response_id,
            )
        await _post_usage(client, session_id=session_id, bridge_dir=bridge_dir, state=state)
        await _close_turn(client, session_id=session_id, turn=turn)
        await _mirror_completed_subagents(
            client, session_id=session_id, agent_name=agent_name, state=state
        )
        return

    if event == _SESSION_END:
        await _post_usage(client, session_id=session_id, bridge_dir=bridge_dir, state=state)
        await _close_turn(client, session_id=session_id, turn=turn)
        await _mirror_completed_subagents(
            client, session_id=session_id, agent_name=agent_name, state=state
        )
        return


async def forward_devin_hooks_to_session(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    agent_name: str,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    auth: httpx.Auth | None = None,
    start_at_end: bool = False,
) -> None:
    """Tail Devin's hook log and mirror it into the Omnigent conversation.

    :param base_url: Omnigent server base URL.
    :param headers: Static HTTP headers (auth normally via *auth*).
    :param session_id: Omnigent conversation id.
    :param bridge_dir: The devin-native bridge dir (hook log + ATIF export).
    :param agent_name: Agent label stamped on mirrored assistant items.
    :param poll_interval_s: Seconds between hook-log polls.
    :param auth: Optional refresh-capable httpx Auth for remote deployments.
    :param start_at_end: Skip hook events already in the log — used on a cold
        resume so replayed history is not re-posted as new.
    :returns: Never normally returns; cancel the task to stop it.
    """
    from omnigent.cli_auth import open_server_client

    state = _read_state(bridge_dir)
    if start_at_end and state.hooks_offset == 0:
        from omnigent.harnesses.devin_native.bridge import hooks_size

        state.hooks_offset = hooks_size(bridge_dir)
        _write_state(bridge_dir, state)
    turn = _TurnState()
    timeout = httpx.Timeout(_POST_TIMEOUT_S)

    async with open_server_client(base_url, headers=headers, auth=auth, timeout=timeout) as client:
        # Tell the bridge injection path it is safe to send: the cursor is
        # established, so nothing already in the log will be re-published.
        write_forwarder_ready(bridge_dir)
        while True:
            try:
                progressed = False
                for offset, payload in iter_hook_events(
                    bridge_dir, start_offset=state.hooks_offset
                ):
                    await _handle_event(
                        client,
                        session_id=session_id,
                        bridge_dir=bridge_dir,
                        agent_name=agent_name,
                        payload=payload,
                        state=state,
                        turn=turn,
                    )
                    state.hooks_offset = offset
                    progressed = True
                if progressed:
                    _write_state(bridge_dir, state)
            except httpx.HTTPError as exc:
                # Persist what we did consume so a retry does not re-post it,
                # then let the supervisor restart us.
                _write_state(bridge_dir, state)
                raise RuntimeError(f"devin-native forwarder post failed: {exc}") from exc
            await asyncio.sleep(poll_interval_s)


async def supervise_devin_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    agent_name: str,
    auth: httpx.Auth | None = None,
    start_at_end: bool = False,
) -> None:
    """Run :func:`forward_devin_hooks_to_session`, restarting it on failure.

    The forward loop persists its cursor before raising, so a restart resumes
    exactly where it stopped rather than replaying the conversation.
    """
    backoff = _SUPERVISE_INITIAL_BACKOFF_S
    while True:
        started = time.monotonic()
        try:
            await forward_devin_hooks_to_session(
                base_url=base_url,
                headers=headers,
                session_id=session_id,
                bridge_dir=bridge_dir,
                agent_name=agent_name,
                auth=auth,
                start_at_end=start_at_end,
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a mirror must not kill the session
            _logger.warning(
                "devin-native forwarder for %s stopped (%s); restarting", session_id, exc
            )
        # A loop that ran a while before dying gets a fresh backoff; a tight
        # crash loop backs off so a broken server is not hammered.
        if time.monotonic() - started > _SUPERVISE_MAX_BACKOFF_S:
            backoff = _SUPERVISE_INITIAL_BACKOFF_S
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, _SUPERVISE_MAX_BACKOFF_S)
        # After the first restart the log already holds everything we consumed,
        # so never skip to the end again.
        start_at_end = False
