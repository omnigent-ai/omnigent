"""Restart recovery preserves dispatch identity across paging and concurrent work."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runner import subagent_recovery, subagent_work
from omnigent.runner.subagent_recovery import SubagentRecovery, build_subagent_recovery

_PARENT = "parent-session"
_CHILD = "child-reviewer"
_DISPATCH = "dispatch-review"
_CHILDREN_PATH = f"/v1/sessions/{_PARENT}/child_sessions"
_ITEMS_PATH = f"/v1/sessions/{_CHILD}/items"
_ResponseHandler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


@pytest.fixture(autouse=True)
def _isolated_work_registries(monkeypatch: pytest.MonkeyPatch) -> None:
    # The recovery builder and work module share aliases to process-local state.
    for name in (
        "_subagent_work_by_child",
        "_subagent_work_by_parent",
        "_session_inboxes_ref",
        "_subagent_recovery_locks",
        "_in_flight_send_locks",
        "_drained_delivered_subagent_children",
        "_subagent_recovery_done",
    ):
        original = getattr(subagent_work, name)
        isolated = type(original)()
        monkeypatch.setattr(subagent_work, name, isolated)
        if hasattr(subagent_recovery, name):
            monkeypatch.setattr(subagent_recovery, name, isolated)


@dataclass
class _RecoveryHarness:
    recovery: SubagentRecovery
    app: FastAPI
    wakes: list[tuple[str, str]]
    background_tasks: set[asyncio.Task[Any]]
    recovery_tasks: dict[str, asyncio.Task[None]]

    def drain(self, parent_id: str = _PARENT) -> list[dict[str, Any]]:
        inbox = subagent_work._session_inboxes_ref[parent_id]
        messages = []
        while not inbox.empty():
            messages.append(inbox.get_nowait())
        return messages


@asynccontextmanager
async def _recovery_harness(handler: _ResponseHandler) -> AsyncIterator[_RecoveryHarness]:
    """Keep recovery real while replacing the remote sessions API with HTTP fixtures."""
    app = FastAPI()
    wakes: list[tuple[str, str]] = []
    background_tasks: set[asyncio.Task[Any]] = set()
    recovery_tasks: dict[str, asyncio.Task[None]] = {}

    async def unexpected_snapshot(session_id: str) -> Any:
        raise AssertionError(f"restart scans must use the sessions API, got {session_id}")

    def schedule_wake(entry: subagent_work._SubagentWorkEntry, *, is_rewake: bool = False) -> None:
        assert not is_rewake
        wakes.append((entry.child_session_id, entry.work_id))

    async with httpx.AsyncClient(
        base_url="http://server", transport=httpx.MockTransport(handler)
    ) as server_client:
        recovery = build_subagent_recovery(
            app,
            _background_tasks=background_tasks,
            _schedule_subagent_wake=schedule_wake,
            _session_inboxes=subagent_work._session_inboxes_ref,
            _session_snapshot=unexpected_snapshot,
            _session_sub_agent_names={},
            _subagent_recovery_tasks=recovery_tasks,
            server_client=server_client,
        )
        try:
            yield _RecoveryHarness(recovery, app, wakes, background_tasks, recovery_tasks)
        finally:
            pending = list(background_tasks)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)


def _child(
    child_id: str = _CHILD,
    *,
    status: str = "completed",
    dispatch_id: str = _DISPATCH,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "id": child_id,
        "tool": "reviewer",
        "session_name": "review",
        "current_task_status": status,
        "labels": {subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY: dispatch_id},
        **fields,
    }


def _assistant_message(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def _page(*items: dict[str, Any], after: str | None = None) -> dict[str, Any]:
    return {"data": list(items), "has_more": after is not None, "last_id": after}


async def test_recovery_reads_all_child_and_history_pages_in_order() -> None:
    second_child = "child-second-reviewer"
    requests: list[tuple[str, dict[str, str]]] = []
    pages = {
        (_CHILDREN_PATH, None): _page(_child(), after="children-page-2"),
        (_CHILDREN_PATH, "children-page-2"): _page(
            _child(second_child, dispatch_id="dispatch-second")
        ),
        (_ITEMS_PATH, None): _page(
            {"type": "function_call", "name": "inspect", "arguments": "{}"},
            {"type": "message", "role": "user", "content": []},
            after="history-page-2",
        ),
        (_ITEMS_PATH, "history-page-2"): _page(
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "First finding"},
                    {"type": "image", "text": "not assistant text"},
                    {"type": "output_text", "text": "Second finding"},
                ],
            },
            _assistant_message("older answer"),
            after="unused-older-page",
        ),
        (f"/v1/sessions/{second_child}/items", None): _page(
            _assistant_message("Independent result")
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        requests.append((request.url.path, dict(request.url.params)))
        return httpx.Response(200, json=pages[(request.url.path, request.url.params.get("after"))])

    async with _recovery_harness(handler) as harness:
        await harness.recovery.recover_undrained_subagent_results(_PARENT)
        assert [
            (message["conversation_id"], message["work_id"], message["output"])
            for message in harness.drain()
        ] == [
            (_CHILD, _DISPATCH, "First finding\nSecond finding"),
            (second_child, "dispatch-second", "Independent result"),
        ]
        assert harness.wakes == [(_CHILD, _DISPATCH), (second_child, "dispatch-second")]
        assert requests == [
            (_CHILDREN_PATH, {"limit": "1000"}),
            (_CHILDREN_PATH, {"limit": "1000", "after": "children-page-2"}),
            (_ITEMS_PATH, {"limit": "100", "order": "desc"}),
            (_ITEMS_PATH, {"limit": "100", "order": "desc", "after": "history-page-2"}),
            (f"/v1/sessions/{second_child}/items", {"limit": "100", "order": "desc"}),
        ]


@pytest.mark.parametrize("failure", ["http-error", "transport-error", "invalid-json"])
async def test_failed_child_page_can_be_retried_without_partial_delivery(failure: str) -> None:
    healthy = False

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _CHILDREN_PATH:
            if "after" not in request.url.params:
                return httpx.Response(200, json=_page(_child(), after="last-page"))
            assert request.url.params["after"] == "last-page"
            if not healthy:
                if failure == "http-error":
                    return httpx.Response(503)
                if failure == "transport-error":
                    raise httpx.ReadError("connection lost", request=request)
                return httpx.Response(200, content=b"not json")
            return httpx.Response(200, json=_page())
        assert request.url.path == _ITEMS_PATH
        return httpx.Response(200, json=_page(_assistant_message("Recovered answer")))

    async with _recovery_harness(handler) as harness:
        await harness.recovery.recover_undrained_subagent_results(_PARENT)
        assert harness.drain() == []
        assert subagent_work.list_subagent_work(_PARENT) == []
        assert harness.wakes == []

        healthy = True
        await harness.recovery.recover_undrained_subagent_results(_PARENT)
        assert [message["output"] for message in harness.drain()] == ["Recovered answer"]
        assert harness.wakes == [(_CHILD, _DISPATCH)]

        await harness.recovery.recover_undrained_subagent_results(_PARENT)
        assert harness.drain() == []
        assert harness.wakes == [(_CHILD, _DISPATCH)]


async def test_retry_after_later_child_failure_does_not_duplicate_earlier_result() -> None:
    second_child = "child-second-reviewer"
    healthy = False

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _CHILDREN_PATH:
            return httpx.Response(
                200,
                json=_page(_child(), _child(second_child, dispatch_id="dispatch-second")),
            )
        if request.url.path == _ITEMS_PATH:
            return httpx.Response(200, json=_page(_assistant_message("First result")))
        assert request.url.path == f"/v1/sessions/{second_child}/items"
        return (
            httpx.Response(200, json=_page(_assistant_message("Second result")))
            if healthy
            else httpx.Response(503)
        )

    async with _recovery_harness(handler) as harness:
        await harness.recovery.recover_undrained_subagent_results(_PARENT)
        assert [message["output"] for message in harness.drain()] == ["First result"]

        healthy = True
        await harness.recovery.recover_undrained_subagent_results(_PARENT)
        assert [message["output"] for message in harness.drain()] == ["Second result"]
        assert harness.wakes == [(_CHILD, _DISPATCH), (second_child, "dispatch-second")]


@pytest.mark.parametrize("race", ["new-dispatch", "live-completion", "already-drained"])
async def test_history_read_does_not_overwrite_work_changed_during_recovery(race: str) -> None:
    history_started = asyncio.Event()
    release_history = asyncio.Event()
    entry = subagent_work.register_subagent_work(
        parent_session_id=_PARENT,
        child_session_id=_CHILD,
        agent="reviewer",
        title="review",
        work_id=_DISPATCH,
    )
    entry.status = "waiting"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _CHILDREN_PATH:
            return httpx.Response(200, json=_page(_child()))
        assert request.url.path == _ITEMS_PATH
        history_started.set()
        await release_history.wait()
        return httpx.Response(200, json=_page(_assistant_message("Stale recovered answer")))

    async with _recovery_harness(handler) as harness:
        scan = harness.recovery.start_subagent_recovery(_PARENT)
        await asyncio.wait_for(history_started.wait(), timeout=5)
        if race == "new-dispatch":
            entry = subagent_work.register_subagent_work(
                parent_session_id=_PARENT,
                child_session_id=_CHILD,
                agent="reviewer",
                title="follow-up",
                work_id="dispatch-newer",
            )
        else:
            subagent_work.mark_subagent_work_terminal(
                _CHILD, status="completed", output="Authoritative live answer"
            )
            if race == "already-drained":
                assert [message["output"] for message in harness.drain()] == [
                    "Authoritative live answer"
                ]
                subagent_work.unregister_subagent_work(
                    _CHILD, work_id=_DISPATCH, remember_drained_delivery=True
                )
        release_history.set()
        await asyncio.wait_for(scan, timeout=5)

        assert harness.wakes == []
        if race == "live-completion":
            assert [message["output"] for message in harness.drain()] == [
                "Authoritative live answer"
            ]
            assert entry.status == "completed"
            assert entry.output == "Authoritative live answer"
        else:
            assert harness.drain() == []
            if race == "new-dispatch":
                assert subagent_work.get_subagent_work(_CHILD) is entry
                assert entry.work_id == "dispatch-newer"
                assert entry.status == "launching"
                assert entry.output is None
            else:
                assert subagent_work.get_subagent_work(_CHILD) is None


async def test_cancelling_one_inbox_reader_preserves_shared_recovery() -> None:
    history_started = asyncio.Event()
    release_history = asyncio.Event()
    history_reads = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal history_reads
        if request.url.path == _CHILDREN_PATH:
            return httpx.Response(200, json=_page(_child()))
        assert request.url.path == _ITEMS_PATH
        history_reads += 1
        history_started.set()
        await release_history.wait()
        return httpx.Response(200, json=_page(_assistant_message("Result survives cancellation")))

    async with _recovery_harness(handler) as harness:
        reader = asyncio.create_task(harness.recovery.recover_undrained_subagent_results(_PARENT))
        try:
            await asyncio.wait_for(history_started.wait(), timeout=5)
            reader.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reader

            release_history.set()
            await asyncio.wait_for(
                harness.recovery.recover_undrained_subagent_results(_PARENT), timeout=5
            )
            assert [message["output"] for message in harness.drain()] == [
                "Result survives cancellation"
            ]
            assert history_reads == 1
            assert harness.wakes == [(_CHILD, _DISPATCH)]
            assert harness.background_tasks == set()
            assert harness.recovery_tasks == {}
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


async def test_session_deletion_cancels_recovery_before_inbox_teardown() -> None:
    history_started = asyncio.Event()
    request_cancelled = asyncio.Event()
    release_history = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _CHILDREN_PATH:
            return httpx.Response(200, json=_page(_child()))
        assert request.url.path == _ITEMS_PATH
        history_started.set()
        try:
            await release_history.wait()
        except asyncio.CancelledError:
            request_cancelled.set()
            raise
        return httpx.Response(200, json=_page(_assistant_message("Recovered after recreation")))

    async with _recovery_harness(handler) as harness:
        harness.recovery.start_subagent_recovery(_PARENT)
        await asyncio.wait_for(history_started.wait(), timeout=5)
        await asyncio.wait_for(harness.recovery.cancel_subagent_recovery(_PARENT), timeout=5)
        assert request_cancelled.is_set()
        assert harness.background_tasks == set()
        assert harness.recovery_tasks == {}
        assert harness.drain() == []
        assert harness.wakes == []

        del subagent_work._session_inboxes_ref[_PARENT]
        release_history.set()
        await harness.recovery.recover_undrained_subagent_results(_PARENT)
        assert [message["output"] for message in harness.drain()] == ["Recovered after recreation"]
        assert harness.wakes == [(_CHILD, _DISPATCH)]


@pytest.mark.parametrize(
    ("initial_status", "error_code"),
    [
        ("in_progress", None),
        ("failed", "runner_disconnected"),
        ("failed", "runner_failed_to_start"),
    ],
)
async def test_reconcile_delivers_interrupted_dispatch_once_it_finishes(
    initial_status: str, error_code: str | None
) -> None:
    completed = False
    child_scans: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _CHILDREN_PATH:
            child_scans.append(request.url.path)
            child = (
                _child()
                if completed
                else _child(
                    status=initial_status,
                    last_task_error={"code": error_code, "message": "Runner went away"},
                )
            )
            return httpx.Response(200, json=_page(child))
        assert request.url.path == _ITEMS_PATH
        assert completed, "an interrupted dispatch has no final transcript to recover"
        return httpx.Response(200, json=_page(_assistant_message("Finished on another runner")))

    async with _recovery_harness(handler) as harness:
        await harness.recovery.recover_undrained_subagent_results(_PARENT)
        pending = subagent_work.get_subagent_work(_CHILD)
        assert pending is not None
        assert pending.status == "waiting"
        assert pending.work_id == _DISPATCH
        assert harness.drain() == []
        assert harness.wakes == []

        completed = True
        await harness.app.state.reconcile_pending_subagent_results()
        assert [
            (message["work_id"], message["status"], message["output"])
            for message in harness.drain()
        ] == [(_DISPATCH, "completed", "Finished on another runner")]
        assert subagent_work.get_subagent_work(_CHILD) is pending

        await harness.app.state.reconcile_pending_subagent_results()
        assert harness.drain() == []
        assert harness.wakes == [(_CHILD, _DISPATCH)]
        assert len(child_scans) == 2


async def test_stalled_recovery_does_not_block_or_deliver_into_another_parent() -> None:
    other_parent = "another-parent"
    other_child = "another-child"
    history_started = asyncio.Event()
    release_history = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _CHILDREN_PATH:
            return httpx.Response(200, json=_page(_child()))
        if request.url.path == f"/v1/sessions/{other_parent}/child_sessions":
            return httpx.Response(
                200, json=_page(_child(other_child, dispatch_id="dispatch-other"))
            )
        if request.url.path == _ITEMS_PATH:
            history_started.set()
            await release_history.wait()
            return httpx.Response(200, json=_page(_assistant_message("First parent's result")))
        assert request.url.path == f"/v1/sessions/{other_child}/items"
        return httpx.Response(200, json=_page(_assistant_message("Other parent's result")))

    async with _recovery_harness(handler) as harness:
        first_scan = harness.recovery.start_subagent_recovery(_PARENT)
        await asyncio.wait_for(history_started.wait(), timeout=5)
        await asyncio.wait_for(
            harness.recovery.recover_undrained_subagent_results(other_parent), timeout=5
        )
        assert not first_scan.done()
        assert harness.drain() == []
        assert [
            (message["conversation_id"], message["work_id"], message["output"])
            for message in harness.drain(other_parent)
        ] == [(other_child, "dispatch-other", "Other parent's result")]

        release_history.set()
        await asyncio.wait_for(first_scan, timeout=5)
        assert [message["output"] for message in harness.drain()] == ["First parent's result"]
        assert harness.drain(other_parent) == []
        assert harness.wakes == [(other_child, "dispatch-other"), (_CHILD, _DISPATCH)]


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"id": None}, id="missing-child-id"),
        pytest.param({"current_task_status": None}, id="missing-status"),
        pytest.param({"current_task_status": "queued"}, id="not-terminal"),
        pytest.param(
            {"labels": {subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY: ""}},
            id="empty-dispatch-id",
        ),
        pytest.param(
            {"labels": {subagent_work.SUBAGENT_DISPATCH_ID_LABEL_KEY: 123}},
            id="non-string-dispatch-id",
        ),
    ],
)
async def test_unrecoverable_child_records_never_read_or_deliver_transcripts(
    overrides: dict[str, Any],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == _CHILDREN_PATH
        return httpx.Response(200, json=_page(_child(**overrides)))

    async with _recovery_harness(handler) as harness:
        await harness.recovery.recover_undrained_subagent_results(_PARENT)
        assert harness.drain() == []
        assert harness.wakes == []
        assert subagent_work.list_subagent_work(_PARENT) == []


async def test_stale_server_dispatch_cannot_finish_a_newer_waiting_turn() -> None:
    entry = subagent_work.register_subagent_work(
        parent_session_id=_PARENT,
        child_session_id=_CHILD,
        agent="reviewer",
        title="follow-up",
        work_id="dispatch-newer",
    )
    entry.status = "waiting"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == _CHILDREN_PATH
        return httpx.Response(200, json=_page(_child()))

    async with _recovery_harness(handler) as harness:
        await harness.recovery.recover_undrained_subagent_results(_PARENT)
        assert harness.drain() == []
        assert harness.wakes == []
        assert subagent_work.get_subagent_work(_CHILD) is entry
        assert entry.status == "waiting"
        assert entry.work_id == "dispatch-newer"
        assert entry.output is None
