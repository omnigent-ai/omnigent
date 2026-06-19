"""E2E tests for ``POST /v1/sessions/{id}/fork`` (mock LLM).

Exercises the fork flows the route/store unit tests can only mock:

1. **Full fork** — fork a two-turn session with no truncation point and
   verify the clone's executor actually *replays* the copied history
   (the agent recalls codewords from both source turns).
2. **Fork from the middle** (``up_to_response_id``) — fork at the first
   assistant response and verify the second turn is gone from the
   clone's items AND from the agent's context (it recalls codeword 1,
   and cannot produce codeword 2).
3. **Fork + agent switch** (``agent_id``) — fork into a different
   built-in agent and verify the clone binds the target agent while
   still carrying the source history. (Skipped under mock LLM — needs
   a built-in claude-sdk agent target.)

All flows route through runner-bound sessions (the alpha runner-state
contract), mirroring how the Web UI drives the fork: create → fork →
``PATCH`` a runner onto the clone → post events.

Usage::

    pytest tests/e2e/test_sessions_fork_e2e.py -v
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)

# The fork-switch TARGET. Only BUILT-IN agents (``session_id IS NULL``)
# are bindable fork targets — agents uploaded via multipart
# ``POST /v1/sessions`` are session-scoped and the route rejects them.
# The live-server fixture seeds this one via OMNIGENT_BUILTIN_AGENT_DIRS
# precisely for fork/switch e2e tests (see conftest).
_BUILTIN_TARGET = "sdk-chat-builtin"

# Codewords the LLM could not produce unless they came through the
# copied history — nonsense token pairs, not real words it might guess.
_CODEWORD_1 = "aurora-zebra-17"
_CODEWORD_2 = "breeze-falcon-42"


def _extract_all_text(body: dict[str, Any]) -> str:
    """
    Concatenate all output_text blocks from a response body.

    :param body: The terminal response body from
        :func:`poll_session_until_terminal`.
    :returns: All assistant text joined by newlines.
    """
    parts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") == "message":
            for block in item.get("content", []):
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts)


def _session_item_texts(client: httpx.Client, session_id: str) -> str:
    """
    Concatenate every text block from a session snapshot's items.

    Used for deterministic copied-history assertions (which markers
    made it into the fork) without involving the LLM.

    :param client: HTTP client pointed at the live server.
    :param session_id: Session/conversation id, e.g. ``"conv_abc"``.
    :returns: All item text joined by newlines.
    """
    resp = client.get(f"/v1/sessions/{session_id}")
    resp.raise_for_status()
    parts: list[str] = []
    for item in resp.json().get("items", []):
        data = item.get("data") or {}
        for block in data.get("content", []) or []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
    return "\n".join(parts)


def _first_assistant_response_id(client: httpx.Client, session_id: str) -> str:
    """
    Return the response id of the session's FIRST assistant message.

    This is exactly the id the Web UI's per-message "Fork from here"
    button sends: the assistant bubble's ``response_id``. For
    runner-native sessions the assistant items carry the
    harness-allocated response id (distinct from the AP-stamped id on
    the user input item), so it must be read off the persisted items
    rather than reusing :func:`send_user_message_to_session`'s return.

    :param client: HTTP client pointed at the live server.
    :param session_id: Session/conversation id, e.g. ``"conv_abc"``.
    :returns: The first assistant item's ``response_id``.
    :raises AssertionError: If the session has no assistant message.
    """
    resp = client.get(f"/v1/sessions/{session_id}")
    resp.raise_for_status()
    for item in resp.json().get("items", []):
        data = item.get("data") or {}
        if item.get("type") == "message" and data.get("role") == "assistant":
            return str(item["response_id"])
    raise AssertionError(f"no assistant message found in session {session_id!r}")


def _register_mock_fork_agent(
    client: httpx.Client,
    mock_llm_server_url: str,
    *,
    prefix: str,
) -> tuple[str, str]:
    """Register an inline agent for mock-mode fork tests.

    :returns: ``(agent_name, model)`` — the model doubles as the
        keyed-queue key on the mock LLM server.
    """
    model = f"mock-fork-{prefix}-{uuid.uuid4().hex[:6]}"
    agent_name = register_inline_agent(
        client,
        name=f"fork-{prefix}-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a terse assistant. When asked to recall, repeat codewords exactly.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    return agent_name, model


def _seed_two_codeword_turns(
    client: httpx.Client,
    *,
    agent_name: str,
    runner_id: str,
) -> str:
    """
    Create a runner-bound session and run two codeword-memorizing turns.

    Turn 1 plants :data:`_CODEWORD_1`, turn 2 plants
    :data:`_CODEWORD_2`. Each turn is polled to terminal so the fork
    sees fully committed history.

    :param client: HTTP client pointed at the live server.
    :param agent_name: Display name of an already-uploaded agent.
    :param runner_id: Registered runner id to bind the session to.
    :returns: The seeded session id, e.g. ``"conv_abc"``.
    """
    session_id = create_runner_bound_session(client, agent_name=agent_name, runner_id=runner_id)
    for codeword in (_CODEWORD_1, _CODEWORD_2):
        response_id = send_user_message_to_session(
            client,
            session_id=session_id,
            content=f"One of my projects is nicknamed {codeword}. Reply with just OK.",
        )
        body = poll_session_until_terminal(client, session_id=session_id, response_id=response_id)
        assert body["status"] == "completed", f"seed turn failed: {body.get('error')}"
    return session_id


def _fork_session(
    client: httpx.Client,
    source_id: str,
    *,
    runner_id: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Fork *source_id*, bind the clone to *runner_id*, return the fork body.

    Mirrors the Web UI flow: ``POST /fork`` creates an unbound clone,
    then ``PATCH`` binds a runner so events can be posted.

    :param client: HTTP client pointed at the live server.
    :param source_id: Session to fork, e.g. ``"conv_abc"``.
    :param runner_id: Registered runner id to bind the fork to.
    :param body: Fork request body (e.g. ``{"up_to_response_id": ...}``,
        ``{"agent_id": ...}``); ``None`` → ``{}`` (full same-agent fork).
    :returns: The 201 response body (``SessionResponse`` shape).
    """
    resp = client.post(f"/v1/sessions/{source_id}/fork", json=body or {})
    assert resp.status_code == 201, f"fork failed: {resp.status_code} {resp.text}"
    fork = resp.json()
    patch = client.patch(f"/v1/sessions/{fork['id']}", json={"runner_id": runner_id})
    patch.raise_for_status()
    return fork


def test_full_fork_replays_whole_history(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    A full fork (no truncation) carries BOTH turns into the clone's
    context.

    **What breaks if wrong:**

    - ``fork_conversation`` copies no/partial items → the codewords are
      missing from the clone's items (first assertion).
    - The executor doesn't replay the copied history on the fork's
      first turn (e.g. history-prompt building skips pre-fork items) →
      the agent can't produce the codewords (second assertion).
    """
    reset_mock_llm(mock_llm_server_url)
    agent_name, model = _register_mock_fork_agent(http_client, mock_llm_server_url, prefix="full")
    # Two seed turns ("OK" each), then a post-fork recall turn.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": "OK"},
            {"text": "OK"},
            {"text": f"Your projects are {_CODEWORD_1} and {_CODEWORD_2}."},
        ],
        key=model,
    )

    source_id = _seed_two_codeword_turns(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )

    fork = _fork_session(http_client, source_id, runner_id=live_runner_id)
    assert fork["id"] != source_id

    # Deterministic: both codewords were deep-copied into the fork.
    fork_text = _session_item_texts(http_client, fork["id"])
    assert _CODEWORD_1 in fork_text and _CODEWORD_2 in fork_text, (
        f"full fork must copy both turns, fork items contained: {fork_text!r}"
    )

    # Live: the fork's agent actually sees the copied history. The probe
    # is mechanical ("repeat what is visible") rather than "what did you
    # remember" — some models refuse the memory framing even with the
    # messages in plain sight.
    response_id = send_user_message_to_session(
        http_client,
        session_id=fork["id"],
        content=(
            "What are the nicknames of my projects mentioned earlier in "
            "this conversation? List them exactly as written."
        ),
    )
    body = poll_session_until_terminal(http_client, session_id=fork["id"], response_id=response_id)
    assert body["status"] == "completed", f"fork turn failed: {body.get('error')}"
    text = _extract_all_text(body)
    assert _CODEWORD_1 in text and _CODEWORD_2 in text, (
        f"fork's agent should recall both codewords from replayed history, got: {text!r}"
    )


def test_fork_from_middle_truncates_context(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    Forking at the FIRST assistant response drops the second turn.

    Sends the first assistant message's ``response_id`` as
    ``up_to_response_id`` — exactly what the Web UI's per-message
    "Fork from here" button sends.

    **What breaks if wrong:**

    - The store ignores the cutoff → codeword 2 appears in the fork's
      items (first assertion).
    - The cutoff lands mid-turn (first item of the response instead of
      the last) → the first turn's assistant reply is missing and the
      replayed context is malformed.
    - History replay includes dropped items anyway → the agent can
      produce codeword 2 (it is NOT in the truncated context, so a
      correct fork cannot emit that exact token pair).
    """
    reset_mock_llm(mock_llm_server_url)
    agent_name, model = _register_mock_fork_agent(http_client, mock_llm_server_url, prefix="mid")
    # Two seed turns ("OK" each), then a post-fork recall turn
    # that only mentions codeword 1 (codeword 2 was truncated).
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": "OK"},
            {"text": "OK"},
            {"text": f"Your project is {_CODEWORD_1}."},
        ],
        key=model,
    )

    source_id = _seed_two_codeword_turns(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    first_response_id = _first_assistant_response_id(http_client, source_id)

    fork = _fork_session(
        http_client,
        source_id,
        runner_id=live_runner_id,
        body={"up_to_response_id": first_response_id},
    )

    # Deterministic: turn 1 copied, turn 2 dropped; source untouched.
    fork_text = _session_item_texts(http_client, fork["id"])
    assert _CODEWORD_1 in fork_text, f"truncated fork lost turn 1: {fork_text!r}"
    assert _CODEWORD_2 not in fork_text, (
        f"truncated fork must not contain the post-cutoff turn: {fork_text!r}"
    )
    source_text = _session_item_texts(http_client, source_id)
    assert _CODEWORD_2 in source_text, "fork must never mutate the source's history"

    # Live: the agent's context matches the truncated items.
    response_id = send_user_message_to_session(
        http_client,
        session_id=fork["id"],
        content=(
            "What are the nicknames of my projects mentioned earlier in "
            "this conversation? List them exactly as written."
        ),
    )
    body = poll_session_until_terminal(http_client, session_id=fork["id"], response_id=response_id)
    assert body["status"] == "completed", f"fork turn failed: {body.get('error')}"
    text = _extract_all_text(body)
    assert _CODEWORD_1 in text, f"agent should recall the pre-cutoff codeword, got: {text!r}"
    assert _CODEWORD_2 not in text, (
        f"agent produced the dropped codeword — truncated history leaked: {text!r}"
    )

    # Stale-client guard: an unknown response id is a 400, not a silent
    # full-history fork.
    bad = http_client.post(
        f"/v1/sessions/{source_id}/fork",
        json={"up_to_response_id": "resp_does_not_exist"},
    )
    assert bad.status_code == 400, f"expected 400 for unknown cutoff, got {bad.status_code}"


def _builtin_agent_id(client: httpx.Client, name: str) -> str:
    """Return the id of a built-in agent by name from ``GET /v1/agents``.

    :param client: HTTP client pointed at the test server.
    :param name: Built-in agent name, e.g. ``"sdk-chat-builtin"``.
    :returns: The built-in agent's id.
    :raises AssertionError: If no built-in with that name is registered.
    """
    resp = client.get("/v1/agents")
    resp.raise_for_status()
    for agent in resp.json()["data"]:
        if agent["name"] == name:
            return str(agent["id"])
    raise AssertionError(f"built-in agent {name!r} not registered on the server")


def test_fork_with_agent_switch_carries_history(
    http_client: httpx.Client,
    claude_coder_agent: str,
    live_runner_id: str,
    using_mock_llm: bool,
    mock_llm_server_url: str,
) -> None:
    """
    Forking with ``agent_id`` binds the TARGET agent and keeps history.

    Forks a source-agent-seeded session into the seeded
    ``sdk-chat-builtin`` built-in (same provider family — the
    history-preserving switch the Web UI's picker offers). SDK targets
    replay the copied transcript as context.

    In mock mode the source agent is an inline ``openai-agents`` agent
    pointed at the mock LLM server, and the ``sdk-chat-builtin`` built-in
    is wired to the mock server via ``executor.auth.base_url`` (seeded in
    ``conftest._materialize_builtin_sdk_chat_spec``). The mock server
    keys responses by model name so each agent gets its own queue.

    **What breaks if wrong:**

    - The route binds the source's agent instead of (a clone of) the
      target → ``agent_name`` is the source's.
    - The switch path drops the copied items → the codeword is missing
      from the fork's items / the agent can't recall it.
    """
    if using_mock_llm:
        uid = uuid.uuid4().hex[:6]
        source_model = f"mock-fork-switch-src-{uid}"
        source_agent = register_inline_agent(
            http_client,
            name=f"fork-switch-src-{uid}",
            harness="openai-agents",
            model=source_model,
            profile="",
            prompt="You are a terse assistant.",
            mock_llm_base_url=f"{mock_llm_server_url}/v1",
        )
        # Target: the sdk-chat-builtin built-in uses model
        # "claude-sonnet-4-20250514" — key the mock queue on that.
        target_model = "claude-sonnet-4-20250514"
        reset_mock_llm(mock_llm_server_url)
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": "OK"}],
            key=source_model,
        )
        # The fork+switch passes the copied transcript as context to the
        # first real LLM call (the recall turn itself) — no separate
        # replay request is made. Queue only the codeword for the recall.
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": _CODEWORD_1}],
            key=target_model,
        )
    else:
        source_agent = claude_coder_agent

    source_id = create_runner_bound_session(
        http_client, agent_name=source_agent, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=source_id,
        content=f"One of my projects is nicknamed {_CODEWORD_1}. Reply with just OK.",
    )
    body = poll_session_until_terminal(http_client, session_id=source_id, response_id=response_id)
    assert body["status"] == "completed", f"seed turn failed: {body.get('error')}"

    target_id = _builtin_agent_id(http_client, _BUILTIN_TARGET)
    fork = _fork_session(
        http_client, source_id, runner_id=live_runner_id, body={"agent_id": target_id}
    )

    # The fork is bound to (a clone of) the TARGET agent, not the source's.
    assert fork.get("agent_name") == _BUILTIN_TARGET, (
        f"fork should report the switched-to agent, got {fork.get('agent_name')!r}"
    )
    source_snap = http_client.get(f"/v1/sessions/{source_id}").json()
    assert fork["agent_id"] != source_snap["agent_id"], (
        "switched fork must not share the source's agent binding"
    )

    # History carried across the switch — items and live context.
    fork_text = _session_item_texts(http_client, fork["id"])
    assert _CODEWORD_1 in fork_text, f"switched fork lost the source history: {fork_text!r}"
    recall_id = send_user_message_to_session(
        http_client,
        session_id=fork["id"],
        content="What is the nickname of my project? Reply with just the nickname.",
    )
    recall = poll_session_until_terminal(http_client, session_id=fork["id"], response_id=recall_id)
    assert recall["status"] == "completed", f"switched fork turn failed: {recall.get('error')}"
    text = _extract_all_text(recall)
    assert _CODEWORD_1 in text, (
        f"switched fork's agent should recall the codeword from copied history, got: {text!r}"
    )
