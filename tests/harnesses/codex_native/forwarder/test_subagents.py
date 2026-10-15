"""Subagents tests for Codex forwarder."""

from __future__ import annotations

import pytest

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
