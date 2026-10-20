"""Tool completion waits for durable PR metadata and keeps failed snapshots replayable."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from filelock import FileLock

from omnigent.runner import session_pr_snapshot_sync, session_prs
from omnigent.runner.session_pr_sync import restore_session_prs, sync_session_prs
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry

URL = "https://github.com/example/repo/pull/42"


@pytest.mark.parametrize(
    "revision_change", [None, "head_sha", "base_sha", "unavailable", "partial"]
)
async def test_snapshot_enrichment_is_background_and_retries_without_provider_refetch(
    tmp_path, monkeypatch, revision_change
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    clock = 0.0
    monkeypatch.setattr(
        session_pr_snapshot_sync,
        "time",
        SimpleNamespace(monotonic=lambda: clock, time_ns=session_pr_snapshot_sync.time.time_ns),
    )
    registry = SessionPrRegistry("session")
    registry.record([PullRequestRef.from_url(URL)], relationship="created", source="test")
    started = threading.Event()
    proceed = threading.Event()
    provider_roots: list[str] = []
    reports: list[dict] = []
    snapshots: list[dict] = []
    revision = {"head_sha": "head-1", "base_sha": "base-1"}

    class Facet:
        def reference_info(self, root, entry):
            provider_roots.append(root)
            started.set()
            assert proceed.wait(5)
            if revision_change == "unavailable" and len(provider_roots) == 2:
                return {"pr": None}
            return {
                "object": "session.github.info",
                "provider": "github",
                "selected_pr_url": entry.url,
                "auth": {"token": "must-not-upload"},
                "pr": {
                    "number": entry.number,
                    "url": entry.url,
                    "title": "Created without opening the panel",
                    "body": "Saved description",
                    "comments": [{"body": "Saved comment", "author": "reviewer"}],
                    **revision,
                },
            }

        def changed_files(self, root, entry):
            if revision_change == "partial":
                return {"object": "list", "data": [], "warning": "Provider omitted files"}
            return {"object": "list", "data": [{"path": "app.py"}], "has_more": False}

        def pr_diff(self, root, entry):
            if revision_change in revision:
                revision[revision_change] = "revision-2"
            return {"object": "session.github.pr_diff", "patch": "+saved change"}

    monkeypatch.setattr("omnigent.git_providers.load_facet", lambda *_args: Facet())

    def receive(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.path == "/v1/sessions/session"
            return httpx.Response(200, json={"id": "session", "workspace": str(tmp_path)})
        body = json.loads(request.content)
        reports.append(body)
        if body["type"] == "external_session_pr_snapshot":
            snapshots.append(body["data"])
            return httpx.Response(503 if len(snapshots) == 1 else 200, json={"saved": True})
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receive), base_url="http://server"
    ) as client:
        await sync_session_prs("session", client)
        task = session_pr_snapshot_sync._tasks[(client, registry.path)]
        try:
            assert await asyncio.to_thread(started.wait, 5)
            assert not task.done()
            assert [report["type"] for report in reports] == ["external_session_prs"]
        finally:
            proceed.set()
        await task
        if revision_change not in (None, "partial"):
            assert snapshots == []
            clock += session_pr_snapshot_sync._RETRY_SECONDS
            await sync_session_prs("session", client)
            await session_pr_snapshot_sync._tasks[(client, registry.path)]
        reads_before_retry = 2 if revision_change in (None, "partial") else 4
        assert len(snapshots) == 1
        assert provider_roots == [str(tmp_path)] * reads_before_retry
        await sync_session_prs("session", client)
        await session_pr_snapshot_sync._tasks[(client, registry.path)]
        assert len(snapshots) == 1
        clock += session_pr_snapshot_sync._RETRY_SECONDS
        await sync_session_prs("session", client)
        await session_pr_snapshot_sync._tasks[(client, registry.path)]
        assert snapshots[0] == snapshots[1]
        assert provider_roots == [str(tmp_path)] * reads_before_retry
        assert snapshots[1]["info"]["pr"]["head_sha"] == revision["head_sha"]
        assert snapshots[1]["info"]["pr"]["base_sha"] == revision["base_sha"]
        assert snapshots[1]["info"]["pr"]["body"] == "Saved description"
        assert snapshots[1]["info"]["pr"]["comments"][0]["body"] == "Saved comment"
        assert "auth" not in snapshots[1]["info"]
        if revision_change == "partial":
            assert "changes" not in snapshots[1]
        else:
            assert snapshots[1]["changes"]["data"][0]["path"] == "app.py"
        assert snapshots[1]["diff"]["patch"] == "+saved change"
        clock += session_pr_snapshot_sync._RETRY_SECONDS
        await sync_session_prs("session", client)
        await session_pr_snapshot_sync._tasks[(client, registry.path)]
        assert len(snapshots) == 2
        clock += (
            session_pr_snapshot_sync._REFRESH_SECONDS - session_pr_snapshot_sync._RETRY_SECONDS
        )
        await sync_session_prs("session", client)
        await session_pr_snapshot_sync._tasks[(client, registry.path)]
        assert len(snapshots) == 3
        assert len(provider_roots) == reads_before_retry + 2
        registry.record([PullRequestRef.from_url(URL)], relationship="worked_on", source="test")
        await sync_session_prs("session", client)
        await session_pr_snapshot_sync._tasks[(client, registry.path)]
        assert len(snapshots) == 4
        assert len(provider_roots) == reads_before_retry + 4
        assert snapshots[-1]["captured_at_ns"] > snapshots[0]["captured_at_ns"]


@pytest.mark.parametrize("reattach", [False, True])
async def test_pending_snapshot_cannot_restore_removed_or_reattached_pr(
    tmp_path, monkeypatch, reattach
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    registry = SessionPrRegistry("session")
    registry.record([PullRequestRef.from_url(URL)], relationship="created", source="test")
    started = threading.Event()
    proceed = threading.Event()
    reports: list[dict] = []

    def read_resources(*_args):
        started.set()
        assert proceed.wait(5)
        return {"diff": {"object": "session.github.pr_diff", "patch": "+old change"}}

    monkeypatch.setattr(session_pr_snapshot_sync, "_read_resources", read_resources)

    def receive(request: httpx.Request) -> httpx.Response:
        reports.append(json.loads(request.content))
        return httpx.Response(200, json={"saved": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receive), base_url="http://server"
    ) as client:
        await sync_session_prs("session", client, workspace=tmp_path)
        task = session_pr_snapshot_sync._tasks[(client, registry.path)]
        try:
            assert await asyncio.to_thread(started.wait, 5)
            registry.remove(URL)
            if reattach:
                registry.record(
                    [PullRequestRef.from_url(URL)], relationship="attached", source="user"
                )
            await sync_session_prs("session", client, workspace=tmp_path)
        finally:
            proceed.set()
        await task
    assert all(report["type"] == "external_session_prs" for report in reports)


@pytest.mark.parametrize("source", ["session", "isolated_runner", "missing"])
async def test_snapshot_workspace_uses_session_or_runner_mapping_only(
    tmp_path, monkeypatch, source
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OMNIGENT_RUNNER_WORKSPACE", raising=False)
    monkeypatch.delenv("OMNIGENT_RUNNER_ISOLATE_SESSION", raising=False)
    metadata = {"id": "session"}
    expected = None
    if source == "session":
        expected = tmp_path / "selected"
        metadata["workspace"] = str(expected)
        monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "different"))
    elif source == "isolated_runner":
        monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path))
        monkeypatch.setenv("OMNIGENT_RUNNER_ISOLATE_SESSION", "1")
        expected = tmp_path / "session"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=metadata)),
        base_url="http://server",
    ) as client:
        assert await session_pr_snapshot_sync._workspace("session", client, None) == expected


async def test_snapshot_provider_concurrency_is_bounded_across_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    started = threading.Event()
    proceed = threading.Event()
    count_lock = threading.Lock()
    active = 0
    peak = 0

    def read_resources(*_args):
        nonlocal active, peak
        with count_lock:
            active += 1
            peak = max(peak, active)
            if active == 4:
                started.set()
        try:
            assert proceed.wait(5)
            return {}
        finally:
            with count_lock:
                active -= 1

    monkeypatch.setattr(session_pr_snapshot_sync, "_read_resources", read_resources)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
        base_url="http://server",
    ) as client:
        tasks = []
        for index in range(6):
            session_id = f"session-{index}"
            registry = SessionPrRegistry(session_id)
            registry.record([PullRequestRef.from_url(URL)], relationship="created", source="test")
            await sync_session_prs(session_id, client, workspace=tmp_path)
            tasks.append(session_pr_snapshot_sync._tasks[(client, registry.path)])
        try:
            assert await asyncio.to_thread(started.wait, 5)
            assert peak == 4
        finally:
            proceed.set()
        await asyncio.gather(*tasks)
    assert peak == 4


async def test_sync_retries_failure_skips_missing_and_publishes_final_removal(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    registry = SessionPrRegistry("session")
    requests: list[dict] = []
    await restore_session_prs("session", [{"url": "invalid"}])
    assert registry.snapshot() is None

    def receive(request: httpx.Request) -> httpx.Response:
        # The local file lock must be released while the server acknowledges the update.
        with FileLock(str(registry.path) + ".lock", timeout=0):
            requests.append(json.loads(request.content))
        assert request.url.path == "/v1/sessions/session/events"
        return httpx.Response(503 if len(requests) == 1 else 200)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receive), base_url="http://server"
    ) as client:
        await sync_session_prs("session", client)
        assert requests == []
        registry.record([PullRequestRef.from_url(URL)], relationship="created", source="test")
        await sync_session_prs("session", client)
        assert len(requests) == 1 and registry.list()[0].url == URL
        await sync_session_prs("session", client)
        assert requests[0] == requests[1]
        assert requests[1]["type"] == "external_session_prs"
        assert requests[1]["data"]["prs"][0]["url"] == URL
        await sync_session_prs("session", client)
        assert len(requests) == 2
        registry.remove(URL)
        await sync_session_prs("session", client)
        assert requests[-1]["data"]["prs"] == []
        assert requests[-1]["data"]["updated_at_ns"] > requests[0]["data"]["updated_at_ns"]
        registry.path.write_text("invalid json")
        await sync_session_prs("session", client)
        assert len(requests) == 3


async def test_sync_serializes_concurrent_snapshots_until_server_ack(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    registry = SessionPrRegistry("session")
    registry.record([PullRequestRef.from_url(URL)], relationship="created", source="test")
    receiving = asyncio.Event()
    acknowledge = asyncio.Event()
    requests: list[dict] = []

    async def receive(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            receiving.set()
            await acknowledge.wait()
        return httpx.Response(200)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receive), base_url="http://server"
    ) as client:
        first = asyncio.create_task(sync_session_prs("session", client))
        await receiving.wait()
        assert not first.done()
        monkeypatch.setattr(session_prs.time, "time_ns", lambda: 1)
        registry.remove(URL)
        second = asyncio.create_task(sync_session_prs("session", client))
        await asyncio.sleep(0)
        assert len(requests) == 1 and not second.done()
        acknowledge.set()
        await asyncio.gather(first, second)
        assert len(requests) == 2
        assert requests[0]["data"]["prs"][0]["url"] == URL
        assert requests[1]["data"]["prs"] == []
        assert requests[1]["data"]["updated_at_ns"] > requests[0]["data"]["updated_at_ns"]


def test_restore_preserves_server_rows_and_keeps_unversioned_local_state(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    source = SessionPrRegistry("source")
    source.record(
        [PullRequestRef.from_url(URL)], relationship="created", source="sdk", timestamp=123
    )
    source.update_titles({URL: "Created remotely"}, timestamp=456)
    entries = [entry.model_dump() for entry in source.list()]
    target = SessionPrRegistry("target")
    target.restore(None)
    assert target.snapshot() is None
    server_snapshot = source.snapshot()
    assert server_snapshot is not None
    remote_revision = server_snapshot[0] + 1_000_000_000
    monkeypatch.setattr(session_prs.time, "time_ns", lambda: server_snapshot[0] - 60_000_000_000)
    target.restore(entries, updated_at_ns=remote_revision)
    assert target.list() == source.list()
    restored = target.snapshot()
    assert restored is not None and restored[0] > remote_revision
    target.remove(URL)
    target.restore(entries)
    assert target.list() == []
    empty = SessionPrRegistry("empty")
    empty.restore([])
    empty.restore(entries)
    assert empty.snapshot() is not None
    assert empty.list() == []


@pytest.mark.parametrize("revision", [True, 2**63, "future"])
def test_restore_rejects_invalid_revision_before_creating_or_replacing_registry(
    tmp_path, revision
):
    registry = SessionPrRegistry("session", root=tmp_path / "registry")
    if revision == "future":
        revision = session_prs.time.time_ns() + 3_600_000_000_000
    with pytest.raises(ValueError):
        registry.restore([], updated_at_ns=revision)
    assert not registry.path.parent.exists()
    registry.record([PullRequestRef.from_url(URL)], relationship="created", source="test")
    original = registry.snapshot()
    with pytest.raises(ValueError):
        registry.restore([], updated_at_ns=revision)
    assert registry.snapshot() == original


def test_newer_server_snapshot_restores_returning_host_and_preserves_removals(tmp_path):
    first_host = SessionPrRegistry("session", root=tmp_path / "first")
    second_host = SessionPrRegistry("session", root=tmp_path / "second")
    original = PullRequestRef.from_url(URL)
    added = PullRequestRef.from_url(URL.replace("42", "43"))
    unsynced = PullRequestRef.from_url(URL.replace("42", "44"))
    first_host.record([original], relationship="created", source="sdk", observation_id="first")
    first_snapshot = first_host.snapshot()
    assert first_snapshot is not None
    second_host.restore(
        [entry.model_dump() for entry in first_snapshot[1]], updated_at_ns=first_snapshot[0]
    )
    second_host.record([added], relationship="created", source="sdk")
    second_snapshot = second_host.snapshot()
    assert second_snapshot is not None
    first_host.restore(
        [entry.model_dump() for entry in second_snapshot[1]], updated_at_ns=second_snapshot[0]
    )
    assert {entry.url for entry in first_host.list()} == {original.url, added.url}
    first_host.record([unsynced], relationship="created", source="sdk")
    first_host.restore([], updated_at_ns=second_snapshot[0])
    assert {entry.url for entry in first_host.list()} == {original.url, added.url, unsynced.url}
    local_snapshot = first_host.snapshot()
    assert local_snapshot is not None
    first_host.restore([], updated_at_ns=local_snapshot[0] + 1)
    first_host.record([original], relationship="created", source="sdk", observation_id="replay")
    assert first_host.list() == []
    empty_snapshot = first_host.snapshot()
    assert empty_snapshot is not None
    first_host.restore(
        [entry.model_dump() for entry in first_snapshot[1]],
        updated_at_ns=empty_snapshot[0] + 1,
    )
    assert first_host.list() == first_snapshot[1]
