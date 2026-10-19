"""Subagents tests for Codex forwarder."""

from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from omnigent.harnesses.codex_native import bridge
from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.harnesses.codex_native.app_server import CodexAppServerResponseError
from tests.harnesses.codex_native.forwarder._support import (
    _RecordingClient,
)

# ---------------------------------------------------------------------------
# _thread_started_is_ephemeral
# ---------------------------------------------------------------------------


def _make_thread_started(thread: dict) -> dict:
    """Wrap a thread dict in a ``thread/started`` envelope."""
    return {"method": "thread/started", "params": {"thread": thread}}


def test_thread_started_is_ephemeral_true_for_ephemeral_system_thread() -> None:
    """The exact 0.150.1 ephemeral system event is classified as ephemeral."""
    event = _make_thread_started(
        {
            "id": "0195aaaa-system",
            "ephemeral": True,
            "path": None,
            "threadSource": "system",
            "source": "vscode",
        }
    )
    assert fwd._thread_started_is_ephemeral(event) is True


def test_thread_started_is_ephemeral_false_for_persistent_clear_thread() -> None:
    """A real ``/clear`` thread (``ephemeral=false``) is not ephemeral."""
    event = _make_thread_started(
        {
            "id": "0195bbbb-user-clear",
            "ephemeral": False,
            "path": "/rollout/0195bbbb.jsonl",
            "threadSource": "user",
        }
    )
    assert fwd._thread_started_is_ephemeral(event) is False


def test_thread_started_is_ephemeral_false_when_ephemeral_absent() -> None:
    """Missing ``ephemeral`` key is treated as non-ephemeral (safe default)."""
    event = _make_thread_started({"id": "0195cccc-no-ephemeral-key"})
    assert fwd._thread_started_is_ephemeral(event) is False


def test_thread_started_is_ephemeral_false_for_wrong_method() -> None:
    """Non-``thread/started`` events never count as ephemeral."""
    event = {"method": "thread/updated", "params": {"thread": {"id": "t", "ephemeral": True}}}
    assert fwd._thread_started_is_ephemeral(event) is False


def test_thread_started_is_ephemeral_false_for_missing_params() -> None:
    """Event with no params is not ephemeral."""
    event = {"method": "thread/started"}
    assert fwd._thread_started_is_ephemeral(event) is False


def test_thread_started_is_ephemeral_false_for_missing_thread() -> None:
    """Event with params but no thread is not ephemeral."""
    event = {"method": "thread/started", "params": {}}
    assert fwd._thread_started_is_ephemeral(event) is False


# ---------------------------------------------------------------------------
# _backfill_child_thread / _resume_child_thread_or_log
# ---------------------------------------------------------------------------


class _FakeChildResumeClient:
    """
    Test double for ``CodexAppServerClient`` exercising child backfill.

    Returns/raises a canned result per JSON-RPC method so a test can drive
    ``thread/resume`` and its ``thread/read`` fallback independently.

    :param resume_response: Response returned by ``thread/resume``, if any.
    :param resume_error: Exception raised by ``thread/resume``, if any.
    :param read_response: Response returned by ``thread/read``, if any.
    :param read_error: Exception raised by ``thread/read``, if any.
    """

    def __init__(
        self,
        *,
        resume_response: dict | None = None,
        resume_error: Exception | None = None,
        read_response: dict | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self.resume_response = resume_response
        self.resume_error = resume_error
        self.read_response = read_response
        self.read_error = read_error
        self.calls: list[tuple[str, dict]] = []

    async def request(self, method: str, params: dict) -> dict:
        """
        Record ``(method, params)`` and return/raise the canned outcome.

        :param method: JSON-RPC method, e.g. ``"thread/resume"``.
        :param params: JSON-RPC params.
        :returns: Canned response for the method.
        """
        self.calls.append((method, params))
        if method == "thread/resume":
            if self.resume_error is not None:
                raise self.resume_error
            assert self.resume_response is not None
            return self.resume_response
        if method == "thread/read":
            if self.read_error is not None:
                raise self.read_error
            assert self.read_response is not None
            return self.read_response
        raise AssertionError(f"unexpected method {method}")


def _child_resume_response(child_thread_id: str, **thread_extra: object) -> dict:
    """
    Build a minimal ``thread/resume``/``thread/read`` success envelope.

    :param child_thread_id: Codex child thread id, e.g. ``"thread_child"``.
    :param thread_extra: Extra ``Thread`` fields, e.g. ``agentNickname="scout"``.
    :returns: A ``{"result": {"thread": ...}}`` envelope with empty turns.
    """
    thread: dict[str, object] = {"id": child_thread_id, "turns": []}
    thread.update(thread_extra)
    return {"result": {"thread": thread}}


def _unloaded_subagent_error() -> CodexAppServerResponseError:
    """Build the multi-agent v2 "unloaded sub-agent" resume refusal."""
    return CodexAppServerResponseError(
        {
            "code": -32600,
            "message": (
                "cannot resume an unloaded multi-agent v2 sub-agent through its "
                "parent; resume the parent first, or use thread/read to inspect it"
            ),
        }
    )


@pytest.mark.asyncio
async def test_backfill_child_thread_falls_back_to_thread_read_on_unloaded_subagent() -> None:
    """
    A v2 "unloaded sub-agent" refusal falls back to ``thread/read``.

    Newer Codex refuses ``thread/resume`` for a sub-agent thread that has not
    been loaded through its parent; Codex's own hint (``thread/read`` with
    ``includeTurns``) must be used instead of surfacing the refusal as a
    failure, and its response replayed/upserted exactly like a resume.
    """
    client = _RecordingClient()
    codex_client = _FakeChildResumeClient(
        resume_error=_unloaded_subagent_error(),
        read_response=_child_resume_response("thread_child", agentNickname="scout"),
    )
    forwarder_state = fwd._CodexForwarderState()

    await fwd._backfill_child_thread(
        client,  # type: ignore[arg-type]
        codex_client,  # type: ignore[arg-type]
        parent_session_id="conv_parent",
        child_session_id="conv_child",
        child_thread_id="thread_child",
        forwarder_state=forwarder_state,
    )

    assert codex_client.calls == [
        ("thread/resume", {"threadId": "thread_child"}),
        ("thread/read", {"threadId": "thread_child", "includeTurns": True}),
    ]
    # No failed status: the child's own turn/agent-status events own its liveness.
    assert not any(post[1]["type"] == "external_session_status" for post in client.posts)
    # The nickname from the thread/read fallback was still upserted.
    assert any(
        post[1]["type"] == "external_codex_subagent_start"
        and post[1]["data"].get("agent_nickname") == "scout"
        for post in client.posts
    )
    assert forwarder_state.needs_child_thread_backfill("thread_child") is False


@pytest.mark.asyncio
async def test_backfill_child_thread_other_error_does_not_fail_or_retry() -> None:
    """
    A non-not-ready, non-refusal backfill error never fails the child.

    Backfill only mirrors history; posting ``failed`` here would double-count
    a healthy child alongside its own turn/agent-status events. The failure
    must also be marked done so a later collab-agent item does not retry the
    same doomed request.
    """
    client = _RecordingClient()
    codex_client = _FakeChildResumeClient(
        resume_error=CodexAppServerResponseError(
            {"code": -32601, "message": "list_turns is not supported yet"}
        ),
    )
    forwarder_state = fwd._CodexForwarderState()

    await fwd._backfill_child_thread(
        client,  # type: ignore[arg-type]
        codex_client,  # type: ignore[arg-type]
        parent_session_id="conv_parent",
        child_session_id="conv_child",
        child_thread_id="thread_child",
        forwarder_state=forwarder_state,
    )

    assert codex_client.calls == [("thread/resume", {"threadId": "thread_child"})]
    assert client.posts == []
    assert forwarder_state.needs_child_thread_backfill("thread_child") is False


@pytest.mark.asyncio
async def test_backfill_child_thread_read_fallback_error_does_not_fail_or_retry() -> None:
    """A ``thread/read`` fallback that also errors is terminal, not a failure."""
    client = _RecordingClient()
    codex_client = _FakeChildResumeClient(
        resume_error=_unloaded_subagent_error(),
        read_error=RuntimeError("boom"),
    )
    forwarder_state = fwd._CodexForwarderState()

    await fwd._backfill_child_thread(
        client,  # type: ignore[arg-type]
        codex_client,  # type: ignore[arg-type]
        parent_session_id="conv_parent",
        child_session_id="conv_child",
        child_thread_id="thread_child",
        forwarder_state=forwarder_state,
    )

    assert codex_client.calls == [
        ("thread/resume", {"threadId": "thread_child"}),
        ("thread/read", {"threadId": "thread_child", "includeTurns": True}),
    ]
    assert client.posts == []
    assert forwarder_state.needs_child_thread_backfill("thread_child") is False


@pytest.mark.asyncio
async def test_resume_child_thread_or_log_not_ready_still_retries() -> None:
    """A fresh child thread's not-ready gap keeps its retryable status."""
    forwarder_state = fwd._CodexForwarderState()
    codex_client = _FakeChildResumeClient(
        resume_error=RuntimeError("no rollout found for thread id thread_child"),
    )

    response = await fwd._resume_child_thread_or_log(
        codex_client,  # type: ignore[arg-type]
        child_thread_id="thread_child",
        forwarder_state=forwarder_state,
    )

    assert response is None
    assert codex_client.calls == [("thread/resume", {"threadId": "thread_child"})]
    assert forwarder_state.needs_child_thread_backfill("thread_child") is True


class CodexChildForwarderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.bridge_dir = Path(temporary.name)
        bridge.write_bridge_state(
            self.bridge_dir,
            bridge.CodexNativeBridgeState(
                session_id="parent",
                thread_id="parent-thread",
                socket_path="ws://localhost:1",
                codex_home=str(self.bridge_dir / "codex"),
                active_turn_id="parent-turn",
            ),
        )
        self.parent_bridge = (self.bridge_dir / "state.json").read_bytes()
        self.posts = []
        self.changed = asyncio.Event()

        def receive(request):
            # Only the external HTTP sink is substituted; the forwarder,
            # routing, coalescers, serialization, and bridge are real.
            self.posts.append((request.url.path, json.loads(request.content)))
            self.changed.set()
            return httpx.Response(200, json={}, request=request)

        self.client = httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(receive)
        )
        self.addAsyncCleanup(self.client.aclose)
        self.state = fwd._CodexForwarderState(parent_session_id="parent", mcp_startup_settled=True)
        self.state.note_child_thread("child-a", "session-a")
        self.state.note_child_thread("child-b", "session-b")
        self.deltas = fwd._OutputTextDeltaCoalescer(self.client, "parent")
        self.usage = fwd._SessionUsageCoalescer(self.client, "parent")
        self.elicitations = fwd._CodexElicitationTaskTracker()
        self.addAsyncCleanup(self.elicitations.close)
        self.addAsyncCleanup(self.usage.close)
        self.addAsyncCleanup(self.deltas.close)
        self.addAsyncCleanup(self.state.close_child_streams)

    async def event(self, method, thread="child-a", turn_id="turn-a", **params):
        await fwd._handle_event(
            self.client,
            session_id="parent",
            bridge_dir=self.bridge_dir,
            event={
                "method": method,
                "params": {"threadId": thread, "turnId": turn_id, **params},
            },
            expected_thread_id="parent-thread",
            forwarder_state=self.state,
            delta_coalescer=self.deltas,
            usage_coalescer=self.usage,
            elicitation_tracker=self.elicitations,
        )

    def events_of_type(self, event_type, session="session-a"):
        return [
            body["data"]
            for path, body in self.posts
            if path == f"/v1/sessions/{session}/events" and body["type"] == event_type
        ]

    async def wait_for_event(self, event_type, session="session-a"):
        async with asyncio.timeout(2):
            while not (events := self.events_of_type(event_type, session)):
                self.changed.clear()
                await self.changed.wait()
            return events

    async def test_child_command_start_and_output_are_live_and_completion_is_deduplicated(
        self,
    ):
        item = {
            "type": "commandExecution",
            "id": "command",
            "command": "printf child",
            "status": "inProgress",
        }
        await self.event("turn/started", turn={"id": "turn-a", "status": "inProgress"})
        await self.event("item/started", item=item)
        calls = self.events_of_type("external_conversation_item")
        self.assertEqual([call["item_type"] for call in calls], ["function_call"])
        await self.event("item/commandExecution/outputDelta", itemId="command", delta="child")
        outputs = await self.wait_for_event("external_tool_output_delta")
        self.assertEqual(outputs, [{"call_id": "command", "delta": "child"}])
        completed = {
            **item,
            "status": "completed",
            "aggregatedOutput": "child",
            "exitCode": 0,
        }
        await self.event("item/completed", item=completed)
        await self.event("item/completed", item=completed)
        self.assertEqual(
            [item["item_type"] for item in self.events_of_type("external_conversation_item")],
            ["function_call", "function_call_output"],
        )
        self.assertEqual((self.bridge_dir / "state.json").read_bytes(), self.parent_bridge)
        self.assertTrue(all(path == "/v1/sessions/session-a/events" for path, _ in self.posts))

    async def test_siblings_keep_text_reasoning_and_tool_output_separate(self):
        for thread, turn, text in (
            ("child-a", "turn-a", "alpha"),
            ("child-b", "turn-b", "beta"),
        ):
            await self.event("turn/started", thread, turn, turn={"id": turn})
            await self.event("item/agentMessage/delta", thread, turn, itemId="message", delta=text)
            await self.event(
                "item/reasoning/summaryTextDelta",
                thread,
                turn,
                itemId="reason",
                delta=text,
            )
            await self.event(
                "item/commandExecution/outputDelta",
                thread,
                turn,
                itemId="command",
                delta=text,
            )
        for session, text in (("session-a", "alpha"), ("session-b", "beta")):
            await self.wait_for_event("external_tool_output_delta", session)
            self.assertEqual(
                self.events_of_type("external_output_text_delta", session)[0]["delta"],
                text,
            )
            self.assertEqual(
                self.events_of_type("external_output_reasoning_delta", session)[0]["delta"],
                text,
            )
            self.assertTrue(
                self.events_of_type("external_output_reasoning_delta", session)[0]["started"]
            )
            self.assertEqual(
                self.events_of_type("external_tool_output_delta", session)[0]["delta"],
                text,
            )
        self.assertEqual((self.bridge_dir / "state.json").read_bytes(), self.parent_bridge)
        self.assertFalse(any(path == "/v1/sessions/parent/events" for path, _ in self.posts))

    async def test_child_file_changes_reach_shared_observer_without_parent_state_changes(self):
        (self.bridge_dir / "tool_relay.json").write_text(
            json.dumps({"url": "http://relay.local", "token": "test-relay-token"})
        )
        # Substitute only the runner's HTTP endpoint; observation payloads,
        # routing, completed-item deduplication, and the bridge remain real.
        with patch.object(
            fwd.urllib.request, "urlopen", side_effect=lambda *_args, **_kwargs: io.BytesIO(b"{}")
        ) as observer:
            for thread, turn, session, path in (
                ("child-a", "turn-a", "session-a", "/repo/alpha.py"),
                ("child-b", "turn-b", "session-b", "/repo/beta.py"),
            ):
                changes = [{"path": path, "kind": {"type": "add"}, "diff": "new\n"}]
                item = {
                    "type": "fileChange",
                    "id": "shared-patch-id",
                    "status": "completed",
                    "changes": changes,
                }
                await self.event("item/completed", thread, turn, item=item)
                await self.event("item/completed", thread, turn, item=item)
                items = self.events_of_type("external_conversation_item", session)
                self.assertEqual(
                    [item["item_type"] for item in items],
                    ["function_call", "function_call_output"],
                )
                self.assertEqual(
                    json.loads(items[0]["item_data"]["arguments"]), {"changes": changes}
                )
                self.assertEqual(items[1]["item_data"]["output"], f"add {path}")

        self.assertEqual(observer.call_count, 2)
        for call, path in zip(
            observer.call_args_list, ("/repo/alpha.py", "/repo/beta.py"), strict=True
        ):
            request = call.args[0]
            self.assertEqual(request.full_url, "http://relay.local/hook/observe-tool")
            self.assertEqual(request.headers["Authorization"], "Bearer test-relay-token")
            self.assertEqual(
                json.loads(request.data),
                {
                    "hook_event_name": "PostToolUse",
                    "tool_name": "apply_patch",
                    "tool_input": {"changes": [{"path": path, "kind": {"type": "add"}}]},
                    "tool_response": {"type": "success"},
                },
            )
        self.assertEqual((self.bridge_dir / "state.json").read_bytes(), self.parent_bridge)
        self.assertFalse(any(path == "/v1/sessions/parent/events" for path, _ in self.posts))

    async def test_interruption_flushes_and_persists_child_text_and_rejects_late_deltas(
        self,
    ):
        # The start may predate subscription, so the first delta is sufficient
        # live evidence until an explicit terminal edge retires its turn.
        await self.event("item/agentMessage/delta", itemId="message", delta="partial answer")
        await self.event("turn/completed", turn={"id": "turn-a", "status": "interrupted"})
        self.assertEqual(
            self.events_of_type("external_output_text_delta")[0]["delta"],
            "partial answer",
        )
        items = self.events_of_type("external_conversation_item")
        self.assertEqual(items[0]["item_data"]["content"][0]["text"], "partial answer")
        await self.event("turn/started", turn_id="turn-new", turn={"id": "turn-new"})
        before = len(self.posts)
        await self.event("turn/completed", turn={"id": "turn-a", "status": "completed"})
        await self.event("item/agentMessage/delta", itemId="message", delta="stale")
        await self.event("item/commandExecution/outputDelta", itemId="command", delta="stale")
        await self.state.close_child_streams()
        self.assertEqual(len(self.posts), before)
        self.assertEqual((self.bridge_dir / "state.json").read_bytes(), self.parent_bridge)

    async def test_unknown_threads_are_ignored_and_shutdown_drains_known_children(self):
        await self.event(
            "item/commandExecution/outputDelta",
            thread="unknown",
            itemId="command",
            delta="unrelated",
        )
        await self.event(
            "item/commandExecution/outputDelta", itemId="command", delta="last output"
        )
        await self.state.close_child_streams()
        self.assertEqual(
            self.events_of_type("external_tool_output_delta"),
            [{"call_id": "command", "delta": "last output"}],
        )
        self.assertEqual(len(self.posts), 1)
        self.assertFalse(self.state.child_streams)

    async def test_terminal_error_stops_deltas_but_allows_completion_cleanup(self):
        diff = "--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+new\n"
        await self.event("turn/started", turn={"id": "turn-a"})
        await self.event("turn/diff/updated", diff=diff)
        await self.event("error", willRetry=False, error={"message": "child failed"})
        await self.event("item/commandExecution/outputDelta", itemId="command", delta="stale")
        completed = {
            "id": "turn-a",
            "status": "failed",
            "error": {"message": "child failed"},
        }
        await self.event("turn/completed", turn=completed)
        await self.event("turn/completed", turn=completed)
        await self.state.close_child_streams()
        items = self.events_of_type("external_conversation_item")
        self.assertEqual(
            [item["item_type"] for item in items],
            ["function_call", "function_call_output"],
        )
        self.assertEqual(items[0]["item_data"]["name"], "turn_diff")
        self.assertEqual(items[1]["item_data"]["output"], diff)
        self.assertEqual(
            [event["status"] for event in self.events_of_type("external_session_status")],
            ["running", "failed"],
        )
        self.assertFalse(self.events_of_type("external_tool_output_delta"))
        self.assertEqual((self.bridge_dir / "state.json").read_bytes(), self.parent_bridge)
