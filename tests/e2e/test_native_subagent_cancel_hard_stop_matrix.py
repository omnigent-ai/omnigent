"""Native sub-agent cancel must hard-stop every uniform-stop harness.

``sys_cancel_task`` routes through ``native_cancel_capability`` (Claude plus
``_UNIFORM_STOP``), not a Claude-only wrapper-label compare. This matrix
proves these cancel surfaces for cursor/goose/kiro/kimi/hermes/qwen:

1. **Active entry** — POST ``stop_session`` and report confirmed cancellation.
2. **Evicted entry** — owned stop-capable natives still receive ``stop_session``.
3. **Failed entry with a live pane** — liveness-probed ``stop_session``, not a
   cached ``failed`` short-circuit.
4. **Hard-stop 503** — a kill failure is unconfirmed/best-effort, never a
   cached terminal / ``absent`` result (503 is not proof the pane is gone).

Reproduced with a mock HTTP transport; no real harness binary is required.

Usage::

    pytest tests/e2e/test_native_subagent_cancel_hard_stop_matrix.py -v
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE,
    CURSOR_NATIVE_WRAPPER_VALUE,
    WRAPPER_LABEL_KEY,
)
from omnigent.runner import app as runner_app
from omnigent.runner.tool_dispatch import execute_tool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UNIFORM_STOP_WRAPPERS = [
    CURSOR_NATIVE_WRAPPER_VALUE,
    "goose-native-ui",
    "kiro-native-ui",
    "kimi-native-ui",
    "hermes-native-ui",
    "qwen-native-ui",
]
"""Wrapper labels whose harnesses register a ``_UNIFORM_STOP`` handler.

A ``sys_cancel_task`` on any of these should route to ``stop_session``, not
``interrupt``, so the child runner can invoke its hard-stop bridge.
"""


class _LivePane:
    """Stand-in native pane that always answers alive.

    A ``failed`` work status does not say whether the resident harness
    process exited; this models the dangerous case — the entry went terminal
    but the pane is still running — which is exactly when a cancel must be
    able to hard-stop it.
    """

    async def is_alive(self) -> bool:
        return True


class _LivePaneRegistry:
    """Terminal registry stub that reports a live ``main`` pane for any child."""

    def get(self, conversation_id: str, terminal_name: str, session_key: str) -> _LivePane:
        return _LivePane()


def _make_cancel_server(
    child_id: str, events: list[dict[str, Any]], stop_marks_terminal: bool = False
) -> httpx.MockTransport:
    """Return a mock transport that records events sent to *child_id*.

    :param child_id: The child session id to intercept.
    :param events: Mutable list that receives every event body posted.
    :param stop_marks_terminal: When ``True``, the handler also calls
        ``runner_app.mark_subagent_work_terminal`` to simulate the child
        runner marking the entry cancelled on receipt of ``stop_session``.
    """

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == f"/v1/sessions/{child_id}/events":
            body = json.loads(request.content)
            events.append(body)
            if stop_marks_terminal and body.get("type") == "stop_session":
                runner_app.mark_subagent_work_terminal(
                    child_id, status="cancelled", output="[System: sub-agent stopped]"
                )
            return httpx.Response(204)
        return httpx.Response(404, json={"error": str(request.url)})

    return httpx.MockTransport(_handler)


# ---------------------------------------------------------------------------
# Facet 1 — active entry: uniform-stop harnesses must receive stop_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper_label", _UNIFORM_STOP_WRAPPERS)
async def test_cancel_active_uniform_stop_harness_sends_stop_session(
    wrapper_label: str,
) -> None:
    """``sys_cancel_task`` POSTs ``stop_session`` for every ``_UNIFORM_STOP`` harness.

    These harnesses register a runner-side hard-stop handler, so the child
    runner honours ``stop_session`` and kills the resident process. Posting
    ``interrupt`` instead leaves the process running.

    On unfixed ``main`` the posted event is ``interrupt``.
    """
    parent_id = f"conv_parent_cancel_active_{wrapper_label}"
    child_id = f"conv_child_cancel_active_{wrapper_label}"
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="native_impl",
        title="native-task",
        wrapper_label=wrapper_label,
    )
    events: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(
            transport=_make_cancel_server(child_id, events, stop_marks_terminal=True),
            base_url="http://server",
        ) as server_client:
            result_raw = await execute_tool(
                tool_name="sys_cancel_task",
                arguments=json.dumps({"task_id": child_id}),
                server_client=server_client,
                conversation_id=parent_id,
                session_async_tasks={},
            )
    finally:
        runner_app.unregister_subagent_work(child_id)

    result = json.loads(result_raw)

    # The event posted to the child runner must be ``stop_session``; any other
    # value means the harness's hard-stop bridge was bypassed.
    assert len(events) == 1, f"Expected exactly 1 event for {wrapper_label!r}; got {events}"
    assert events[0]["type"] == "stop_session", (
        f"Expected stop_session for uniform-stop harness {wrapper_label!r}; "
        f"got {events[0]['type']!r}.  "
        "The cancel is routing through 'interrupt' (bug) instead of "
        "'stop_session' (fix), so the resident native process keeps running."
    )

    # After the stop marks the entry terminal the result should reflect
    # confirmed cancellation.
    assert result.get("cancelled") is True, (
        f"Expected confirmed cancellation for {wrapper_label!r}; got {result}"
    )


# ---------------------------------------------------------------------------
# Facet 2 — evicted entry: owned stop-capable natives must still be stoppable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper_label", _UNIFORM_STOP_WRAPPERS)
async def test_cancel_evicted_uniform_stop_harness_sends_stop_session(
    wrapper_label: str,
) -> None:
    """An evicted owned stop-capable native still receives ``stop_session``.

    When the work entry is gone from the in-process registry but the child
    session still exists on the server, cancel must verify
    ``parent_session_id`` ownership and POST ``stop_session`` for any
    stop-capable wrapper, not only Claude.

    On unfixed ``main`` the result is ``no in-flight task`` and no event is sent.
    """
    parent_id = f"conv_parent_evicted_{wrapper_label}"
    child_id = f"conv_child_evicted_{wrapper_label}"
    # Do NOT register the work entry — simulate the evicted-entry path.

    events: list[dict[str, Any]] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        """Serve a GET for the child session (it exists) and record events."""
        if request.method == "GET" and request.url.path == f"/v1/sessions/{child_id}":
            return httpx.Response(
                200,
                json={
                    "id": child_id,
                    "parent_session_id": parent_id,
                    "labels": {WRAPPER_LABEL_KEY: wrapper_label},
                },
            )
        if request.method == "POST" and request.url.path == f"/v1/sessions/{child_id}/events":
            body = json.loads(request.content)
            events.append(body)
            return httpx.Response(204)
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://server",
    ) as server_client:
        result_raw = await execute_tool(
            tool_name="sys_cancel_task",
            arguments=json.dumps({"task_id": child_id}),
            server_client=server_client,
            conversation_id=parent_id,
            session_async_tasks={},
        )

    # On the buggy build: result_raw is an error string and events is empty.
    assert "no in-flight task" not in result_raw, (
        f"Evicted {wrapper_label!r} task returned 'no in-flight task' error; "
        "the cancellation fallback is filtering out non-claude-native wrapper labels "
        "so the resident process has no cleanup path.  "
        f"Full result: {result_raw}"
    )
    assert len(events) == 1, (
        f"Expected 1 stop event for evicted {wrapper_label!r} task; got {events}.  "
        f"Result: {result_raw}"
    )
    assert events[0]["type"] == "stop_session", (
        f"Expected stop_session event for evicted {wrapper_label!r} task; "
        f"got {events[0]['type']!r}"
    )


# ---------------------------------------------------------------------------
# Facet 3 — failed entry with a live pane: cancel must still attempt a stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper_label", _UNIFORM_STOP_WRAPPERS)
async def test_cancel_failed_uniform_stop_harness_sends_stop_session(
    wrapper_label: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``failed`` stop-capable entry with a live pane still posts ``stop_session``.

    ``failed`` does not mean the resident process exited. Cancel probes pane
    liveness and hard-stops when the pane still answers.

    On unfixed ``main`` the result is cached ``{'status': 'failed'}`` with no
    stop attempt.
    """
    monkeypatch.setattr("omnigent.runtime.get_terminal_registry", lambda: _LivePaneRegistry())
    parent_id = f"conv_parent_failed_{wrapper_label}"
    child_id = f"conv_child_failed_{wrapper_label}"
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="native_impl",
        title="native-task",
        wrapper_label=wrapper_label,
    )
    # Transition to failed (process might still be alive).
    runner_app.mark_subagent_work_terminal(
        child_id,
        status="failed",
        output="[System: native process crashed]",
    )

    events: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(
            transport=_make_cancel_server(child_id, events),
            base_url="http://server",
        ) as server_client:
            result_raw = await execute_tool(
                tool_name="sys_cancel_task",
                arguments=json.dumps({"task_id": child_id}),
                server_client=server_client,
                conversation_id=parent_id,
                session_async_tasks={},
            )
    finally:
        runner_app.unregister_subagent_work(child_id)

    result = json.loads(result_raw)

    # On the buggy build: events == [] and result == {'cancelled': False, 'status': 'failed'}
    assert len(events) == 1, (
        f"Expected a stop event for failed {wrapper_label!r} entry; got {events}.  "
        f"Result: {result}.  "
        "The cancel is short-circuiting on 'failed' status (bug: can_stop_failed gate "
        "only open for claude-native) so no cleanup is attempted for the potentially "
        "live resident process."
    )
    assert events[0]["type"] == "stop_session", (
        f"Expected stop_session for failed {wrapper_label!r}; got {events[0]['type']!r}"
    )


def _assert_unconfirmed_hard_stop(result: dict[str, Any], *, task_id: str) -> None:
    """503 must be an explicit unconfirmed cancel, not a cached terminal status."""
    assert result.get("cancelled") is False
    assert result.get("cancel_requested") is True
    assert result.get("cancel_confirmed") is False
    assert result.get("best_effort") is True
    assert result.get("task_id") == task_id
    assert result.get("status") not in {"absent", "cancelled"}
    assert result != {"cancelled": False, "task_id": task_id, "status": "failed"}
    assert result != {"cancelled": False, "task_id": task_id, "status": "absent"}
    message = str(result.get("message", "")).lower()
    assert "may still be running" in message
    assert "not confirmed" in message


# ---------------------------------------------------------------------------
# Facet 4 — stop_session 503 is failed-to-stop, not pane-gone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrapper_label",
    [CLAUDE_NATIVE_WRAPPER_VALUE, "goose-native-ui"],
)
async def test_cancel_failed_live_pane_503_is_unconfirmed(
    wrapper_label: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live failed pane whose hard-stop 503s is unconfirmed, not cached ``failed``.

    ``_claude_stop`` already returns 204 for ``TmuxSessionNotAdvertised``; a 503
    there is a kill failure on a process that is likely still alive.
    ``_uniform_stop`` 503s any ``RuntimeError``, including a transient tmux
    error against a live pane. Neither case may collapse into a cached
    terminal status.
    """
    monkeypatch.setattr("omnigent.runtime.get_terminal_registry", lambda: _LivePaneRegistry())
    parent_id = f"conv_parent_failed_503_{wrapper_label}"
    child_id = f"conv_child_failed_503_{wrapper_label}"
    runner_app.register_subagent_work(
        parent_session_id=parent_id,
        child_session_id=child_id,
        agent="native_impl",
        title="native-task",
        wrapper_label=wrapper_label,
    )
    runner_app.mark_subagent_work_terminal(
        child_id,
        status="failed",
        output="[System: native process crashed]",
    )
    events: list[dict[str, Any]] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == f"/v1/sessions/{child_id}/events":
            events.append(json.loads(request.content))
            return httpx.Response(503, json={"error": "native_stop_failed"})
        return httpx.Response(404, json={"error": str(request.url)})

    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_handler),
            base_url="http://server",
        ) as server_client:
            result_raw = await execute_tool(
                tool_name="sys_cancel_task",
                arguments=json.dumps({"task_id": child_id}),
                server_client=server_client,
                conversation_id=parent_id,
                session_async_tasks={},
            )
    finally:
        runner_app.unregister_subagent_work(child_id)

    result = json.loads(result_raw)
    assert events == [{"type": "stop_session", "data": {}}]
    _assert_unconfirmed_hard_stop(result, task_id=child_id)


@pytest.mark.asyncio
async def test_cancel_evicted_stop_503_is_unconfirmed() -> None:
    """Evicted ``stop_session`` 503 must not report ``status: absent``."""
    parent_id = "conv_parent_evicted_503"
    child_id = "conv_child_evicted_503"
    events: list[dict[str, Any]] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/sessions/{child_id}":
            return httpx.Response(
                200,
                json={
                    "id": child_id,
                    "parent_session_id": parent_id,
                    "labels": {WRAPPER_LABEL_KEY: "goose-native-ui"},
                },
            )
        if request.method == "POST" and request.url.path == f"/v1/sessions/{child_id}/events":
            events.append(json.loads(request.content))
            return httpx.Response(503, json={"error": "native_stop_failed"})
        return httpx.Response(404, json={"error": str(request.url)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://server",
    ) as server_client:
        result_raw = await execute_tool(
            tool_name="sys_cancel_task",
            arguments=json.dumps({"task_id": child_id}),
            server_client=server_client,
            conversation_id=parent_id,
            session_async_tasks={},
        )

    result = json.loads(result_raw)
    assert events == [{"type": "stop_session", "data": {}}]
    _assert_unconfirmed_hard_stop(result, task_id=child_id)
