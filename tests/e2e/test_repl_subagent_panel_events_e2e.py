"""E2E for the data contract behind the REPL's live sub-agent display.

The CLI's ``state: N agents running`` badge and ``↓`` sub-agents menu are
fed by two server-side sources, both exercised here against a real LLM:

* the **parent SSE stream** emits ``session.created`` and
  ``session.child_session.updated`` (with ``busy`` / ``current_task_status``)
  for the active session's direct children — the live fast-path; and
* ``GET /v1/sessions/{id}/child_sessions`` lists those children — the source
  the recursive tree poll reads for deeper levels.

If the server stops emitting either, the CLI panel goes blank even while
sub-agents are running. A terminal UI can't be driven from e2e, so this test
asserts the data the panel consumes, not the rendering (covered by the
``tests/frontends/sdk`` unit tests).

Excluded from default ``pytest`` runs via ``--ignore=tests/e2e``. Invoke::

    pytest tests/e2e/test_repl_subagent_panel_events_e2e.py \\
        --llm-api-key "$(cat /tmp/mykey)" -v
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import httpx
from omnigent_client._sessions import SessionsNamespace

from tests.e2e.conftest import (
    create_runner_bound_session,
    poll_until_terminal,
    send_user_message_to_session,
)


def _iter_sse(response: httpx.Response):
    """Yield decoded SSE event dicts from a streaming response."""
    buffer = ""
    for chunk in response.iter_text():
        buffer += chunk
        while "\n\n" in buffer:
            frame, _, buffer = buffer.partition("\n\n")
            data_line = next(
                (line for line in frame.splitlines() if line.startswith("data:")),
                None,
            )
            if data_line is None:
                continue
            payload = data_line[len("data:") :].strip()
            if payload == "[DONE]":
                return
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                continue


def _frame_of_type(ev: dict[str, Any], event_type: str) -> dict[str, Any] | None:
    """Return the event body if *ev* (flat or enveloped) has *event_type*."""
    if ev.get("type") == event_type:
        return ev
    data = ev.get("data")
    if isinstance(data, dict) and data.get("type") == event_type:
        return data
    return None


def test_parent_stream_and_child_sessions_expose_subagents(
    live_server: str,
    http_client: httpx.Client,
    archer_agent: str,
    live_runner_id: str,
    llm_api_key: str,
) -> None:
    """A real sub-agent run emits the SSE child events the badge consumes and
    lists the child via ``child_sessions`` (the tree poll's source).

    :param live_server: Base URL for a side client that tails the stream
        without head-of-line-blocking the main client.
    :param http_client: HTTP client pointed at the live server.
    :param archer_agent: Uploaded archer agent (has fact_checker / summarizer
        server-side sub-agents — no native CLI required).
    :param live_runner_id: Registered runner the session binds to.
    :param llm_api_key: Gates the test on a configured LLM key.
    """
    session_id = create_runner_bound_session(
        http_client, agent_name=archer_agent, runner_id=live_runner_id
    )

    # Tail the parent stream in a side thread so we capture the transient
    # child events while the turn is in flight (they are SSE-only).
    saw_created = threading.Event()
    saw_busy_child = threading.Event()
    saw_status_field = threading.Event()
    stop = threading.Event()

    def _tail_stream() -> None:
        try:
            with httpx.Client(base_url=live_server, timeout=240.0) as side:
                with side.stream("GET", f"/v1/sessions/{session_id}/stream") as resp:
                    if resp.status_code != 200:
                        return
                    for ev in _iter_sse(resp):
                        if stop.is_set():
                            return
                        created = _frame_of_type(ev, "session.created")
                        if created and created.get("child_session_id"):
                            saw_created.set()
                        updated = _frame_of_type(ev, "session.child_session.updated")
                        if updated:
                            child = updated.get("child") or {}
                            if child.get("busy") is True:
                                saw_busy_child.set()
                            if "current_task_status" in child:
                                saw_status_field.set()
        except Exception:
            return

    tail = threading.Thread(target=_tail_stream, daemon=True)
    tail.start()

    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Use sys_session_send to spawn the summarizer sub-agent and ask "
            "it to summarize the concept of photosynthesis in exactly two "
            "sentences. Wait for its result before you finish."
        ),
    )
    body = poll_until_terminal(http_client, response_id, timeout=240)
    assert body["status"] == "completed", f"Sub-agent run failed: {body.get('error')}"

    stop.set()
    tail.join(timeout=10)

    # The tree-poll source: the parent must list the spawned child.
    resp = http_client.get(f"/v1/sessions/{session_id}/child_sessions")
    resp.raise_for_status()
    children = resp.json().get("data", [])
    assert children, (
        "GET /v1/sessions/{id}/child_sessions returned no children — the ↓ "
        "menu / tree poll would render nothing despite a sub-agent running."
    )
    first = children[0]
    assert "busy" in first and "current_task_status" in first, (
        "child_sessions rows are missing the busy / current_task_status fields "
        f"the badge + menu render; got keys: {sorted(first)}"
    )

    # The SDK rollup (subtree_busy / tree_busy) an SDK driver consumes: against
    # the same real server, child_sessions_tree must list the spawned child and
    # subtree_busy must settle to False now the run is terminal — the SDK-side
    # mirror of the data contract asserted above (issue #444).
    async def _sdk_rollup() -> tuple[list[dict[str, Any]], bool]:
        async with httpx.AsyncClient(timeout=30.0) as ac:
            ns = SessionsNamespace(ac, live_server)
            tree = await ns.child_sessions_tree(session_id)
            busy = await ns.subtree_busy(session_id)
            return tree, busy

    tree, subtree_busy = asyncio.run(_sdk_rollup())
    assert {c["id"] for c in children} <= {n["id"] for n in tree}, (
        "child_sessions_tree did not surface the spawned child the one-level "
        "endpoint returned — the SDK rollup would miss it."
    )
    assert subtree_busy is False, (
        "subtree_busy stayed True after the run reached a terminal state — an "
        "SDK eval driver would never resume injecting 'your turn'."
    )

    # The live fast-path: the parent stream emitted the transient child events.
    assert saw_created.is_set(), (
        "parent stream never emitted session.created for the spawned child — "
        "the badge would not flip to 'agents running' until the next poll."
    )
    assert saw_busy_child.is_set(), (
        "no session.child_session.updated reported busy=True — the badge's "
        "running count would never light."
    )
    assert saw_status_field.is_set(), (
        "child updates carried no current_task_status — the menu's per-agent "
        "status word would be blank."
    )
