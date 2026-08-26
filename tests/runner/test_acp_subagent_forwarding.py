"""Runner forwarding of a harness's sub-agents to child sessions.

The runner intercepts the runner-internal ``subagent.started`` / ``subagent.completed``
SSE events (which the adapter emits from an ACP agent's normalized sub-agent
lifecycle — see :mod:`omnigent.inner.acp_subagents`) and mints / finalizes a child
session via the existing ``external_subagent_start`` + ``external_session_status``
endpoints. These test the two forwarding helpers against a real-``Response`` stub.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from omnigent.runner import app as runner_app_mod


@dataclass
class _Post:
    """One recorded POST: the path and the JSON body."""

    url: str
    body: dict[str, Any]


class _RecordingServerClient:
    """Records POSTs and returns queued real ``httpx.Response`` objects.

    A real stub (not ``MagicMock``) so an unexpected call fails loudly, and real
    responses so ``raise_for_status`` runs its true logic. Matches the helpers'
    call shape ``post(url, *, json=...)`` — no ``timeout`` kwarg.
    """

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.calls: list[_Post] = []

    async def post(self, url: str, *, json: dict[str, Any]) -> httpx.Response:
        """Record the POST and pop the next queued response."""
        self.calls.append(_Post(url=url, body=json))
        assert self._responses, f"unexpected POST #{len(self.calls)} (no response queued)"
        return self._responses.pop(0)


def _resp(status: int, url: str, body: dict[str, Any]) -> httpx.Response:
    """Build a real ``httpx.Response`` with a request attached (for raise_for_status)."""
    return httpx.Response(status, request=httpx.Request("POST", f"http://test{url}"), json=body)


@pytest.mark.asyncio
async def test_mint_subagent_child_posts_start_and_resolves_id() -> None:
    """The start edge POSTs ``external_subagent_start`` and resolves the child id.

    **What breaks if this fails**: the sub-agent never becomes a child session,
    so the Subagents panel stays empty and the completion edge can't address it.
    """
    url = "/v1/sessions/parent1/events"
    client = _RecordingServerClient([_resp(200, url, {"child_session_id": "child_abc"})])
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    await runner_app_mod._mint_acp_subagent_child(
        client,  # type: ignore[arg-type]
        parent_id="parent1",
        child_key="a0ac9364",
        title="mathutils",
        task="create mathutils.py",
        child_id_future=fut,
    )

    assert fut.result() == "child_abc"
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call.url == url
    assert call.body["type"] == "external_subagent_start"
    assert call.body["data"] == {
        "subagent_id": "a0ac9364",
        "agent_type": "mathutils",
        "description": "create mathutils.py",
        "tool_use_id": "a0ac9364",
    }


@pytest.mark.asyncio
async def test_mint_subagent_child_falls_back_agent_type_to_child_key() -> None:
    """A blank title still sends a non-empty ``agent_type`` (the child_key)."""
    client = _RecordingServerClient(
        [_resp(200, "/v1/sessions/p/events", {"child_session_id": "c"})]
    )
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    await runner_app_mod._mint_acp_subagent_child(
        client,  # type: ignore[arg-type]
        parent_id="p",
        child_key="k1",
        title="",
        task="",
        child_id_future=fut,
    )
    assert client.calls[0].body["data"]["agent_type"] == "k1"


@pytest.mark.asyncio
async def test_mint_subagent_child_records_failure_on_non_2xx() -> None:
    """A failed mint resolves the future with an exception (best-effort, no raise).

    The completion edge keys off that exception to fail fast instead of hanging
    on a child that was never created.
    """
    url = "/v1/sessions/parent1/events"
    client = _RecordingServerClient([_resp(500, url, {"error": "boom"})])
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    await runner_app_mod._mint_acp_subagent_child(
        client,  # type: ignore[arg-type]
        parent_id="parent1",
        child_key="a0ac9364",
        title="mathutils",
        task="t",
        child_id_future=fut,
    )
    assert fut.done()
    assert fut.exception() is not None


@pytest.mark.asyncio
async def test_complete_subagent_child_posts_idle_status_with_summary() -> None:
    """The success edge marks the child ``idle`` and attaches the summary as output."""
    status_url = "/v1/sessions/child_abc/events"
    client = _RecordingServerClient([_resp(200, status_url, {"ok": True})])
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    fut.set_result("child_abc")

    await runner_app_mod._complete_acp_subagent_child(
        client,  # type: ignore[arg-type]
        child_key="a0ac9364",
        ok=True,
        summary="3 tests pass",
        child_id_future=fut,
    )

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call.url == status_url
    assert call.body["type"] == "external_session_status"
    assert call.body["data"]["status"] == "idle"
    assert call.body["data"]["output"] == "3 tests pass"


@pytest.mark.asyncio
async def test_complete_subagent_child_marks_failed_when_not_ok() -> None:
    """A failed sub-agent marks the child ``failed`` (the server surfaces the detail)."""
    client = _RecordingServerClient([_resp(200, "/v1/sessions/child_abc/events", {})])
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    fut.set_result("child_abc")

    await runner_app_mod._complete_acp_subagent_child(
        client,  # type: ignore[arg-type]
        child_key="a0ac9364",
        ok=False,
        summary="blocked",
        child_id_future=fut,
    )
    assert client.calls[0].body["data"]["status"] == "failed"


@pytest.mark.asyncio
async def test_complete_subagent_child_skips_when_mint_failed() -> None:
    """If the start edge's mint failed, completion logs and skips — no status POST.

    Guards the correlation: a failed mint must never strand the turn or fire a
    status POST against a child id that was never created.
    """
    client = _RecordingServerClient([])  # any POST would fail loudly (empty queue)
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    fut.set_exception(RuntimeError("mint failed"))

    await runner_app_mod._complete_acp_subagent_child(
        client,  # type: ignore[arg-type]
        child_key="a0ac9364",
        ok=True,
        summary="x",
        child_id_future=fut,
    )
    assert client.calls == []
