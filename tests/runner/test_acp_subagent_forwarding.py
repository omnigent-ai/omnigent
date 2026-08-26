"""Runner forwarding of a harness's sub-agents to child sessions.

The runner intercepts the runner-internal ``subagent.started`` / ``subagent.completed``
SSE events (which the adapter emits from an ACP agent's normalized sub-agent
lifecycle — see :mod:`omnigent.inner.acp_subagents`) and mints / fills a child
session via ``external_acp_subagent_start``, ``external_conversation_item``, and
``external_session_status``.

Two properties these pin, both user-visible bugs when they regress:

* the mint uses the **ACP** start type, not the native ``external_subagent_start``
  — that one stamps a claude-native wrapper label, which titles the child
  "Claude Code" whatever the real harness is; and
* the child's transcript is seeded (task in, summary out), so opening the row
  shows the sub-agent's work instead of an empty chat.
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


def _ok(url: str, body: dict[str, Any] | None = None) -> httpx.Response:
    """A 200 for *url*."""
    return _resp(200, url, body or {})


@pytest.mark.asyncio
async def test_mint_subagent_child_uses_the_acp_start_type() -> None:
    """The start edge POSTs ``external_acp_subagent_start`` and resolves the child id.

    **What breaks if this fails**: using the native ``external_subagent_start``
    stamps a claude-native wrapper label on the child, so the UI titles a Devin
    sub-agent "Claude Code" — the exact bug this type exists to avoid.
    """
    parent_url = "/v1/sessions/parent1/events"
    child_url = "/v1/sessions/child_abc/events"
    client = _RecordingServerClient(
        [_ok(parent_url, {"child_session_id": "child_abc"}), _ok(child_url)]
    )
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
    mint = client.calls[0]
    assert mint.url == parent_url
    assert mint.body["type"] == "external_acp_subagent_start"
    assert mint.body["data"] == {
        "subagent_id": "a0ac9364",
        "title": "mathutils",
        "description": "create mathutils.py",
    }
    # The native path's keys must not leak into the ACP payload.
    assert "agent_type" not in mint.body["data"]
    assert "tool_use_id" not in mint.body["data"]


@pytest.mark.asyncio
async def test_mint_subagent_child_seeds_the_task_into_the_child_chat() -> None:
    """The delegated task lands in the child's transcript as a user message.

    **What breaks if this fails**: the row appears but opening it shows an empty
    chat — the reported symptom.
    """
    parent_url = "/v1/sessions/parent1/events"
    child_url = "/v1/sessions/child_abc/events"
    client = _RecordingServerClient(
        [_ok(parent_url, {"child_session_id": "child_abc"}), _ok(child_url)]
    )
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    await runner_app_mod._mint_acp_subagent_child(
        client,  # type: ignore[arg-type]
        parent_id="parent1",
        child_key="a0ac9364",
        title="mathutils",
        task="create mathutils.py",
        child_id_future=fut,
    )

    assert len(client.calls) == 2, "expected the mint plus the task message"
    item = client.calls[1]
    assert item.url == child_url, "the task must be addressed to the CHILD, not the parent"
    assert item.body["type"] == "external_conversation_item"
    assert item.body["data"]["item_type"] == "message"
    assert item.body["data"]["item_data"] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "create mathutils.py"}],
    }
    assert item.body["data"]["response_id"]


@pytest.mark.asyncio
async def test_mint_subagent_child_skips_the_seed_without_a_task() -> None:
    """No task text → no transcript POST (the row is still minted)."""
    parent_url = "/v1/sessions/p/events"
    client = _RecordingServerClient([_ok(parent_url, {"child_session_id": "c"})])
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    await runner_app_mod._mint_acp_subagent_child(
        client,  # type: ignore[arg-type]
        parent_id="p",
        child_key="k1",
        title="",
        task="",
        child_id_future=fut,
    )

    assert fut.result() == "c"
    assert len(client.calls) == 1
    # A blank title still sends a usable label (the child_key).
    assert client.calls[0].body["data"]["title"] == "k1"


@pytest.mark.asyncio
async def test_mint_subagent_child_records_failure_on_non_2xx() -> None:
    """A failed mint resolves the future with an exception and posts no transcript.

    The completion edge keys off that exception to fail fast instead of hanging
    on a child that was never created.
    """
    parent_url = "/v1/sessions/parent1/events"
    client = _RecordingServerClient([_resp(500, parent_url, {"error": "boom"})])
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    await runner_app_mod._mint_acp_subagent_child(
        client,  # type: ignore[arg-type]
        parent_id="parent1",
        child_key="a0ac9364",
        title="mathutils",
        task="t",
        child_id_future=fut,
    )
    assert fut.done() and fut.exception() is not None
    assert len(client.calls) == 1, "a failed mint must not attempt the transcript POST"


@pytest.mark.asyncio
async def test_complete_subagent_child_posts_summary_then_idle_status() -> None:
    """The success edge writes the summary into the child chat, then marks it idle.

    The summary item MUST carry an ``agent`` — ``MessageData`` rejects an
    assistant message without one, so an item that omits it 400s and the summary
    silently never lands (the reported "assistant response missing" bug). The
    author is the sub-agent's title.
    """
    child_url = "/v1/sessions/child_abc/events"
    client = _RecordingServerClient([_ok(child_url), _ok(child_url)])
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    fut.set_result("child_abc")

    await runner_app_mod._complete_acp_subagent_child(
        client,  # type: ignore[arg-type]
        child_key="a0ac9364",
        ok=True,
        summary="3 tests pass",
        child_id_future=fut,
        title="mathutils",
    )

    assert len(client.calls) == 2
    msg, status = client.calls
    assert msg.url == child_url
    assert msg.body["data"]["item_data"] == {
        "role": "assistant",
        "agent": "mathutils",
        "content": [{"type": "output_text", "text": "3 tests pass"}],
    }
    assert status.body["type"] == "external_session_status"
    assert status.body["data"]["status"] == "idle"
    assert status.body["data"]["output"] == "3 tests pass"


@pytest.mark.asyncio
async def test_summary_author_falls_back_to_child_key_without_a_title() -> None:
    """With no title, the assistant author is the child_key — never blank.

    A blank ``agent`` fails the same ``MessageData`` validator, so the fallback
    must be non-empty for the summary to persist at all.
    """
    child_url = "/v1/sessions/child_abc/events"
    client = _RecordingServerClient([_ok(child_url), _ok(child_url)])
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    fut.set_result("child_abc")

    await runner_app_mod._complete_acp_subagent_child(
        client,  # type: ignore[arg-type]
        child_key="a0ac9364",
        ok=True,
        summary="done",
        child_id_future=fut,
    )
    assert client.calls[0].body["data"]["item_data"]["agent"] == "a0ac9364"


@pytest.mark.asyncio
async def test_complete_subagent_child_marks_failed_when_not_ok() -> None:
    """A failed sub-agent marks the child ``failed`` (the server surfaces the detail)."""
    child_url = "/v1/sessions/child_abc/events"
    client = _RecordingServerClient([_ok(child_url), _ok(child_url)])
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    fut.set_result("child_abc")

    await runner_app_mod._complete_acp_subagent_child(
        client,  # type: ignore[arg-type]
        child_key="a0ac9364",
        ok=False,
        summary="blocked",
        child_id_future=fut,
    )
    assert client.calls[-1].body["data"]["status"] == "failed"


@pytest.mark.asyncio
async def test_complete_subagent_child_skips_when_mint_failed() -> None:
    """If the start edge's mint failed, completion logs and skips — no POST at all.

    Guards the correlation: a failed mint must never strand the turn or fire a
    POST against a child id that was never created.
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


@pytest.mark.asyncio
async def test_transcript_failure_does_not_break_the_turn() -> None:
    """A rejected transcript POST is swallowed — the row still works.

    The panel entry is the load-bearing part; a transcript hiccup must not raise
    into the turn or lose the minted child.
    """
    parent_url = "/v1/sessions/parent1/events"
    child_url = "/v1/sessions/child_abc/events"
    client = _RecordingServerClient(
        [_ok(parent_url, {"child_session_id": "child_abc"}), _resp(500, child_url, {"e": 1})]
    )
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    await runner_app_mod._mint_acp_subagent_child(
        client,  # type: ignore[arg-type]
        parent_id="parent1",
        child_key="a0ac9364",
        title="mathutils",
        task="t",
        child_id_future=fut,
    )
    assert fut.result() == "child_abc", "the child id must survive a transcript failure"
