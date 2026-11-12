"""Delivery tests for Codex forwarder."""

from __future__ import annotations

import asyncio
import errno
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.inner.databricks_executor import DatabricksAuthError
from omnigent.runtime import pending_inputs
from omnigent.server.routes._sessions.orchestration import _persist_external_conversation_item
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests._helpers.polling import poll_until_async
from tests.harnesses.codex_native.forwarder._support import (
    _RaisingPostClient,
    _RecordingClient,
)


@pytest.mark.asyncio
async def test_post_session_event_dead_letters_durable_event_on_permanent_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A permanently-failed durable event is dead-lettered to disk (#1120).

    Drives ``_post_session_event`` (the health-tracking wrapper) with a stubbed
    inner that returns an HTTP 500, and asserts the dropped
    ``external_conversation_item`` payload is appended to
    ``{bridge_dir}/dead_letter.jsonl``.

    :param tmp_path: Pytest temp dir standing in for the bridge dir.
    :param monkeypatch: Pytest patcher (auto-restores the stubbed inner).
    """
    import json as _json

    fwd._reset_forward_health()

    async def _failing_inner(client, session_id, *, event_type, data, max_attempts, timeout):
        return fwd._PostResult(
            response=httpx.Response(500, request=httpx.Request("POST", "http://test"))
        )

    monkeypatch.setattr(fwd, "_post_session_event_inner", _failing_inner)
    token = fwd._dead_letter_dir.set(tmp_path)
    try:
        data = {"item_type": "message", "item_data": {"role": "assistant"}}
        await fwd._post_session_event(
            MagicMock(),
            "conv_codex1",
            event_type="external_conversation_item",
            data=data,
        )
    finally:
        fwd._dead_letter_dir.reset(token)
        fwd._reset_forward_health()

    dl_path = tmp_path / "dead_letter.jsonl"
    lines = dl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = _json.loads(lines[0])
    assert record["session_id"] == "conv_codex1"
    assert record["event_type"] == "external_conversation_item"
    assert record["payload"] == data


@pytest.mark.asyncio
async def test_post_session_event_does_not_dead_letter_ephemeral_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An ephemeral (non-durable) event is NOT dead-lettered on failure (#1120).

    :param tmp_path: Pytest temp dir standing in for the bridge dir.
    :param monkeypatch: Pytest patcher (auto-restores the stubbed inner).
    """
    fwd._reset_forward_health()

    async def _failing_inner(client, session_id, *, event_type, data, max_attempts, timeout):
        return fwd._PostResult(
            response=httpx.Response(500, request=httpx.Request("POST", "http://test"))
        )

    monkeypatch.setattr(fwd, "_post_session_event_inner", _failing_inner)
    token = fwd._dead_letter_dir.set(tmp_path)
    try:
        await fwd._post_session_event(
            MagicMock(),
            "conv_codex1",
            event_type="external_output_text_delta",
            data={"delta": "hi"},
        )
    finally:
        fwd._dead_letter_dir.reset(token)
        fwd._reset_forward_health()

    assert not (tmp_path / "dead_letter.jsonl").exists()


@pytest.mark.asyncio
async def test_post_session_event_dead_letters_usage_on_permanent_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A permanently-failed ``external_session_usage`` event is dead-lettered (#1120).

    Usage is the other durable type alongside conversation items, so its
    transcript/usage data must also be recoverable on a sustained outage.

    :param tmp_path: Pytest temp dir standing in for the bridge dir.
    :param monkeypatch: Pytest patcher (auto-restores the stubbed inner).
    """
    import json as _json

    fwd._reset_forward_health()

    async def _failing_inner(client, session_id, *, event_type, data, max_attempts, timeout):
        return fwd._PostResult(
            response=httpx.Response(500, request=httpx.Request("POST", "http://test"))
        )

    monkeypatch.setattr(fwd, "_post_session_event_inner", _failing_inner)
    token = fwd._dead_letter_dir.set(tmp_path)
    try:
        data = {"context_tokens": 1234, "model": "databricks-claude-opus-4-7"}
        await fwd._post_session_event(
            MagicMock(),
            "conv_codex_usage",
            event_type="external_session_usage",
            data=data,
        )
    finally:
        fwd._dead_letter_dir.reset(token)
        fwd._reset_forward_health()

    dl_path = tmp_path / "dead_letter.jsonl"
    lines = dl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = _json.loads(lines[0])
    assert record["session_id"] == "conv_codex_usage"
    assert record["event_type"] == "external_session_usage"
    assert record["payload"] == data


@pytest.mark.asyncio
async def test_live_mirror_restart_replays_only_the_original_committed_source(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost acknowledgement and auth outage cannot duplicate history or consume newer input."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(
        title="Synthetic", labels={"omnigent.wrapper": "codex-native"}
    )
    content = [{"type": "input_text", "text": "first input"}]
    original_pending = pending_inputs.record(
        conv.id, content, stable_id="a" * 32, background_titles_enabled=False
    )
    phase = "commit_then_lose_ack"
    posts: list[dict] = []
    committed_posts: list[dict] = []
    credential_attempts = 0
    monkeypatch.setattr(fwd, "_LIVE_MIRROR_RETRY_BASE_SECONDS", 0.01)

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal phase, credential_attempts
        body = json.loads(request.content)
        posts.append(body)
        assert request.url.path == f"/v1/sessions/{conv.id}/events"
        if phase == "auth_unavailable":
            credential_attempts += 1
            raise DatabricksAuthError("synthetic outage")
        committed_posts.append(body)
        await _persist_external_conversation_item(
            conv.id, conv, SessionEventInput.model_validate(body), store, enabled=False
        )
        if phase == "commit_then_lose_ack":
            phase = "auth_unavailable"
            raise httpx.ReadTimeout("synthetic lost acknowledgement", request=request)
        return httpx.Response(200, json={}, request=request)

    token = fwd._dead_letter_dir.set(tmp_path)
    locks_token = fwd._conversation_item_locks.set({})
    newer: str | None = None
    try:
        async with httpx.AsyncClient(
            base_url="http://omnigent.test", transport=httpx.MockTransport(handler)
        ) as client:
            first = fwd._LiveMirrorRecovery(client, tmp_path)
            first_token = fwd._live_mirror_recovery.set(first)
            try:
                with pytest.raises(DatabricksAuthError):
                    await fwd._post_external_item(
                        client,
                        conv.id,
                        item_type="message",
                        item_data={"role": "user", "content": content},
                        response_id="native_turn_first",
                        source_id="native_thread:native_turn_first:native_user_first",
                    )
            finally:
                await first.close()
                fwd._live_mirror_recovery.reset(first_token)
            assert len(store.list_items(conv.id).data) == 1
            newer = pending_inputs.record(
                conv.id,
                [{"type": "input_text", "text": "newer input"}],
                stable_id="b" * 32,
                background_titles_enabled=False,
            )
            phase = "repaired"
            restarted = fwd._LiveMirrorRecovery(client, tmp_path)
            restarted.start()
            try:
                await poll_until_async(
                    lambda: not (tmp_path / "dead_letter.jsonl").exists(), timeout_s=3
                )
            finally:
                await restarted.close()

        assert 1 <= credential_attempts <= fwd._POST_MAX_ATTEMPTS
        assert len(posts) == credential_attempts + 2
        assert committed_posts == [posts[0], posts[0]]
        assert all(post == posts[0] for post in posts)
        [saved] = store.list_items(conv.id).data
        assert saved.data.content == content
        assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(conv.id)] == [newer]
    finally:
        pending_inputs.resolve(conv.id, original_pending)
        if newer is not None:
            pending_inputs.resolve(conv.id, newer)
        fwd._conversation_item_locks.reset(locks_token)
        fwd._dead_letter_dir.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["auth", "timeout", "http503"])
async def test_live_mirror_fifo_wait_is_scoped_to_its_original_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """One failed owner retains FIFO order while another owner's mirrors can recover."""
    monkeypatch.setattr(fwd, "_LIVE_MIRROR_RETRY_BASE_SECONDS", 0.01)
    monkeypatch.setattr(fwd, "_LIVE_MIRROR_POST_SECONDS", 0.05)
    ready = False
    other_received = asyncio.Event()
    complete = asyncio.Event()
    attempts: list[str] = []
    accepted: list[tuple[str, str]] = []
    for session_id, source_id in [
        ("conv_old", "old:0"),
        ("conv_old", "old:1"),
        ("conv_new", "new:0"),
    ]:
        fwd.append_dead_letter(
            tmp_path,
            session_id=session_id,
            event_type="external_conversation_item",
            payload={"source_id": source_id, "item_type": "message"},
            reason="synthetic outage",
            delivered_ambiguous=True,
        )

    async def handler(request: httpx.Request) -> httpx.Response:
        source_id = json.loads(request.content)["data"]["source_id"]
        attempts.append(source_id)
        if source_id.startswith("old:") and not ready:
            assert source_id == "old:0", "a later mirror overtook its failed predecessor"
            if failure == "auth":
                raise DatabricksAuthError("synthetic outage")
            if failure == "http503":
                return httpx.Response(503, request=request)
            await asyncio.Event().wait()
        accepted.append((request.url.path, source_id))
        if source_id == "new:0":
            other_received.set()
        if source_id == "old:1":
            complete.set()
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(
        base_url="http://omnigent.test", transport=httpx.MockTransport(handler)
    ) as client:
        recovery = fwd._LiveMirrorRecovery(client, tmp_path)
        recovery.start()
        try:
            await asyncio.wait_for(other_received.wait(), timeout=2)
            assert "old:1" not in attempts
            ready = True
            await asyncio.wait_for(complete.wait(), timeout=2)
            await poll_until_async(
                lambda: not (tmp_path / "dead_letter.jsonl").exists(), timeout_s=2
            )
        finally:
            await recovery.close()

    assert accepted == [
        ("/v1/sessions/conv_new/events", "new:0"),
        ("/v1/sessions/conv_old/events", "old:0"),
        ("/v1/sessions/conv_old/events", "old:1"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 403, 404])
async def test_live_mirror_permanent_rejections_and_legacy_ambiguity_remain_forensic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    """Permanent rejection retires retries; a fresh valid source can recover afterward."""
    monkeypatch.setattr(fwd, "_LIVE_MIRROR_RETRY_BASE_SECONDS", 0.01)
    for payload, ambiguous, recorded_status in [
        ({"item_type": "message"}, True, None),
        ({"source_id": "already_rejected"}, False, 400),
        ({"source_id": "reject_once"}, True, None),
    ]:
        fwd.append_dead_letter(
            tmp_path,
            session_id="conv_forensic",
            event_type="external_conversation_item",
            payload=payload,
            reason="synthetic failure",
            delivered_ambiguous=ambiguous,
            http_status=recorded_status,
        )
    sources: list[str] = []
    healthy = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        source_id = json.loads(request.content)["data"]["source_id"]
        sources.append(source_id)
        if source_id == "reject_once":
            return httpx.Response(status_code, request=request)
        assert source_id == "fresh_source"
        healthy.set()
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(
        base_url="http://omnigent.test", transport=httpx.MockTransport(handler)
    ) as client:
        recovery = fwd._LiveMirrorRecovery(client, tmp_path)
        recovery.start()
        try:
            await poll_until_async(lambda: recovery.idle_revision is not None, timeout_s=2)
            assert sources == ["reject_once"]
            retained = [
                json.loads(line)
                for line in (tmp_path / "dead_letter.jsonl").read_text().splitlines()
            ]
            assert len(retained) == 3
            assert retained[-1]["http_status"] == status_code
            fwd.append_dead_letter(
                tmp_path,
                session_id="conv_forensic",
                event_type="external_conversation_item",
                payload={"source_id": "fresh_source"},
                reason="synthetic pre-send failure",
            )
            recovery.note_pending("conv_forensic")
            await asyncio.wait_for(healthy.wait(), timeout=2)
            await poll_until_async(
                lambda: recovery.idle_revision == recovery._revision(), timeout_s=2
            )
        finally:
            await recovery.close()

    assert sources == ["reject_once", "fresh_source"]
    assert len((tmp_path / "dead_letter.jsonl").read_text().splitlines()) == 3


@pytest.mark.asyncio
async def test_live_mirror_cancelled_ack_can_recover_with_a_successor_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling after commit retains the same idempotent payload for the next worker."""
    from omnigent.native import _native_forwarder_health as health

    monkeypatch.setattr(fwd, "_LIVE_MIRROR_RETRY_BASE_SECONDS", 0.01)
    payload = {"source_id": "native:accepted", "item_type": "message"}
    fwd.append_dead_letter(
        tmp_path,
        session_id="conv_cancelled",
        event_type="external_conversation_item",
        payload=payload,
        reason="synthetic failure",
        delivered_ambiguous=True,
    )
    committed = asyncio.Event()
    received = asyncio.Event()
    repaired = False
    posts: list[dict] = []
    server_records: dict[str, dict] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)["data"]
        posts.append(data)
        server_records.setdefault(data["source_id"], data)
        if not repaired:
            committed.set()
            await asyncio.Event().wait()
        received.set()
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(
        base_url="http://omnigent.test", transport=httpx.MockTransport(handler)
    ) as client:
        first = fwd._LiveMirrorRecovery(client, tmp_path)
        first.start()
        await asyncio.wait_for(committed.wait(), timeout=2)
        assert health.has_pending_delivery("conv_cancelled")
        await first.close()
        assert not health.has_pending_delivery("conv_cancelled")
        assert (tmp_path / "dead_letter.jsonl").exists()
        stopped_task = first.task
        first.start()
        first.note_pending("conv_cancelled")
        assert first.task is stopped_task and stopped_task is not None and stopped_task.done()
        assert not health.has_pending_delivery("conv_cancelled")
        repaired = True
        successor = fwd._LiveMirrorRecovery(client, tmp_path)
        successor.start()
        try:
            await asyncio.wait_for(received.wait(), timeout=2)
            await poll_until_async(
                lambda: not (tmp_path / "dead_letter.jsonl").exists(), timeout_s=2
            )
        finally:
            await successor.close()

    assert posts == [payload, payload]
    assert server_records == {payload["source_id"]: payload}


@pytest.mark.asyncio
async def test_live_mirror_append_during_empty_probe_is_not_forgotten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A journal append during a no-pending scan invalidates the idle revision."""
    monkeypatch.setattr(fwd, "_LIVE_MIRROR_RETRY_BASE_SECONDS", 0.01)
    fwd.append_dead_letter(
        tmp_path,
        session_id="conv_probe",
        event_type="external_conversation_item",
        payload={"source_id": "native:first"},
        reason="synthetic failure",
    )
    original_probe = fwd.has_replayable_dead_letters
    appended = False
    sources: list[str] = []
    received = asyncio.Event()

    def pending_probe(*args, **kwargs):
        nonlocal appended
        pending = original_probe(*args, **kwargs)
        if not pending and not appended:
            appended = True
            fwd.append_dead_letter(
                tmp_path,
                session_id="conv_probe",
                event_type="external_conversation_item",
                payload={"source_id": "native:later"},
                reason="append while the scan result is returning",
            )
        return pending

    monkeypatch.setattr(fwd, "has_replayable_dead_letters", pending_probe)

    async def handler(request: httpx.Request) -> httpx.Response:
        source_id = json.loads(request.content)["data"]["source_id"]
        sources.append(source_id)
        if source_id == "native:later":
            received.set()
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(
        base_url="http://omnigent.test", transport=httpx.MockTransport(handler)
    ) as client:
        recovery = fwd._LiveMirrorRecovery(client, tmp_path)
        recovery.start()
        try:
            await asyncio.wait_for(received.wait(), timeout=2)
            await poll_until_async(
                lambda: not (tmp_path / "dead_letter.jsonl").exists(), timeout_s=2
            )
        finally:
            await recovery.close()
    assert sources == ["native:first", "native:later"]


@pytest.mark.asyncio
@pytest.mark.parametrize("old_journal", [False, True], ids=["new-disposition", "legacy-record"])
async def test_local_mirror_rejection_does_not_block_a_newer_valid_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old_journal: bool
) -> None:
    """An unchanged locally rejected envelope stays forensic instead of blocking valid history."""
    monkeypatch.setattr(fwd, "MAX_SESSION_EVENT_REQUEST_BYTES", 500)
    monkeypatch.setattr(fwd, "_LIVE_MIRROR_RETRY_BASE_SECONDS", 0.01)
    sources: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        sources.append(json.loads(request.content)["data"]["source_id"])
        return httpx.Response(200, request=request)

    token = fwd._dead_letter_dir.set(tmp_path)
    try:
        async with httpx.AsyncClient(
            base_url="http://omnigent.test", transport=httpx.MockTransport(handler)
        ) as client:
            recovery = fwd._LiveMirrorRecovery(client, tmp_path)
            recovery_token = fwd._live_mirror_recovery.set(recovery)
            try:
                large_item = {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "x" * 1000}],
                }
                if old_journal:
                    fwd.append_dead_letter(
                        tmp_path,
                        session_id="conv_limits",
                        event_type="external_conversation_item",
                        payload={
                            "item_type": "message",
                            "item_data": large_item,
                            "response_id": "native:large",
                            "source_id": "large_source",
                        },
                        reason="unclassified old local rejection",
                    )
                else:
                    assert not await fwd._post_external_item(
                        client,
                        "conv_limits",
                        item_type="message",
                        item_data=large_item,
                        response_id="native:large",
                        source_id="large_source",
                    )
                posted = await fwd._post_external_item(
                    client,
                    "conv_limits",
                    item_type="message",
                    item_data={"role": "user", "content": [{"type": "input_text", "text": "ok"}]},
                    response_id="native:small",
                    source_id="small_source",
                )
                assert posted is not old_journal
                recovery.start()
                await poll_until_async(lambda: sources == ["small_source"], timeout_s=2)
                await poll_until_async(lambda: recovery.idle_revision is not None, timeout_s=2)
            finally:
                await recovery.close()
                fwd._live_mirror_recovery.reset(recovery_token)
        [retained] = [
            json.loads(line) for line in (tmp_path / "dead_letter.jsonl").read_text().splitlines()
        ]
        assert retained["payload"]["source_id"] == "large_source"
        assert retained["rejection_reason"].startswith("session event request exceeds")
        assert sources == ["small_source"]
    finally:
        fwd._dead_letter_dir.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("late_first_claim", [False, True])
async def test_retiring_mirror_worker_cannot_renew_or_clear_its_successors_lease(
    tmp_path: Path, late_first_claim: bool
) -> None:
    """An old worker retry cannot reclaim an active replacement's delivery ownership."""
    from omnigent.native import _native_forwarder_health as health

    async with httpx.AsyncClient(base_url="http://omnigent.test") as client:
        old = fwd._LiveMirrorRecovery(client, tmp_path)
        successor = fwd._LiveMirrorRecovery(client, tmp_path)
        try:
            if not late_first_claim:
                old.note_pending("conv_successor")
            successor.note_pending("conv_successor")
            old.note_pending("conv_successor")
            await old.close()
            assert health.has_pending_delivery("conv_successor")
            await successor.close()
            assert not health.has_pending_delivery("conv_successor")
        finally:
            await old.close()
            await successor.close()


@pytest.mark.asyncio
async def test_duplicate_acknowledgement_cannot_pin_failed_journal_bookkeeping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One real ACK extends grace; repeated cleanup of that source does not renew it."""
    from types import SimpleNamespace

    from omnigent.native import _native_forwarder_health as health
    from omnigent.native import _native_post_delivery as delivery

    clock = [1000.0]
    monkeypatch.setattr(health, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(fwd, "_LIVE_MIRROR_RETRY_BASE_SECONDS", 0.01)
    payload = {"source_id": "native:acknowledged"}
    fwd.append_dead_letter(
        tmp_path,
        session_id="conv_bookkeeping",
        event_type="external_conversation_item",
        payload=payload,
        reason="synthetic failure",
    )
    repeated_cleanup = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_apply = delivery._apply_dead_letter_updates
    cleanup_attempts = 0
    posts: list[dict] = []

    def fail_bookkeeping(bridge_dir, updates):
        nonlocal cleanup_attempts
        if bridge_dir != tmp_path:
            return original_apply(bridge_dir, updates)
        cleanup_attempts += 1
        if cleanup_attempts >= 2:
            loop.call_soon_threadsafe(repeated_cleanup.set)
        raise OSError(errno.ENOSPC, "synthetic bookkeeping storage full")

    monkeypatch.setattr(delivery, "_apply_dead_letter_updates", fail_bookkeeping)

    async def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content)["data"])
        clock[0] = 1059.0
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(
        base_url="http://omnigent.test", transport=httpx.MockTransport(handler)
    ) as client:
        recovery = fwd._LiveMirrorRecovery(client, tmp_path)
        recovery.note_pending("conv_bookkeeping")
        recovery.start()
        try:
            await asyncio.wait_for(repeated_cleanup.wait(), timeout=2)
            clock[0] = 1061.0
            assert health.has_pending_delivery("conv_bookkeeping")
            clock[0] = 1120.0
            assert not health.has_pending_delivery("conv_bookkeeping")
            assert posts == [payload]
            assert (tmp_path / "dead_letter.jsonl").exists()
        finally:
            await recovery.close()
            health.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_down", [False, True])
@pytest.mark.parametrize("inverted_lock", [False, True])
async def test_failed_journal_append_keeps_the_owned_native_mirror_until_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auth_down: bool, inverted_lock: bool
) -> None:
    """ENOSPC cannot burn a native claim or overtake an older mirror during inline recovery."""
    monkeypatch.setattr(fwd, "_LIVE_MIRROR_RETRY_BASE_SECONDS", 0.01)
    monkeypatch.setattr(fwd, "_LIVE_MIRROR_POST_SECONDS", 0.05)
    older_source = "thread:turn:user_older"
    later_source = "thread:turn:user_later"
    fwd.append_dead_letter(
        tmp_path,
        session_id="conv_spool",
        event_type="external_conversation_item",
        payload={
            "item_type": "message",
            "item_data": {"role": "user", "content": [{"type": "input_text", "text": "older"}]},
            "response_id": "codex_turn",
            "source_id": older_source,
        },
        reason="synthetic original failure",
    )
    journal = tmp_path / "dead_letter.jsonl"
    original_open = Path.open
    failed_appends = 0

    def full_append(path, mode="r", *args, **kwargs):
        nonlocal failed_appends
        if path == journal and "a" in mode:
            failed_appends += 1
            raise OSError(errno.ENOSPC, "synthetic journal full")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", full_append)
    credentials_valid = not auth_down
    rejected = asyncio.Event()
    worker_entered = asyncio.Event()
    posted: list[str] = []
    accepted: dict[str, dict] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        source_id = body["data"]["source_id"]
        if not credentials_valid:
            rejected.set()
            raise DatabricksAuthError("synthetic credential outage")
        posted.append(source_id)
        accepted.setdefault(source_id, body["data"])
        return httpx.Response(200, request=request)

    async def restore_credentials() -> None:
        nonlocal credentials_valid
        await rejected.wait()
        credentials_valid = True

    locks_token = fwd._conversation_item_locks.set({})
    letter_token = fwd._dead_letter_dir.set(tmp_path)
    repair = asyncio.create_task(restore_credentials()) if auth_down else None
    try:
        async with httpx.AsyncClient(
            base_url="http://omnigent.test", transport=httpx.MockTransport(handler)
        ) as client:
            recovery = fwd._LiveMirrorRecovery(client, tmp_path)
            recovery_token = fwd._live_mirror_recovery.set(recovery)
            original_repost = recovery._repost

            async def observe_worker(record, blocked_sessions):
                worker_entered.set()
                return await original_repost(record, blocked_sessions)

            monkeypatch.setattr(recovery, "_repost", observe_worker)
            state = fwd._CodexForwarderState(parent_session_id="conv_spool")
            params = {
                "threadId": "thread",
                "turnId": "turn",
                "item": {
                    "type": "userMessage",
                    "id": "user_later",
                    "clientId": "b" * 32,
                    "content": [{"type": "text", "text": "later"}],
                },
            }
            try:
                async with fwd._conversation_item_delivery_scope("conv_spool"):
                    if inverted_lock:
                        recovery.start()
                        await asyncio.wait_for(worker_entered.wait(), timeout=2)
                    async with asyncio.timeout(3):
                        await fwd._handle_completed_item(
                            client, "conv_spool", params, forwarder_state=state
                        )
                        await fwd._handle_completed_item(
                            client, "conv_spool", params, forwarder_state=state
                        )
            finally:
                await recovery.close()
                fwd._live_mirror_recovery.reset(recovery_token)

        assert failed_appends > 0
        assert list(accepted) == [older_source, later_source]
        assert posted.count(later_source) == 1
        assert accepted[later_source]["item_data"]["content"] == [
            {"type": "input_text", "text": "later"}
        ]
        assert not journal.exists()
    finally:
        if repair is not None:
            repair.cancel()
            await asyncio.gather(repair, return_exceptions=True)
        fwd._conversation_item_locks.reset(locks_token)
        fwd._dead_letter_dir.reset(letter_token)


@pytest.mark.asyncio
@pytest.mark.parametrize("append_owner", ["conv_current", "conv_foreign"])
async def test_live_fifo_fence_defers_a_journal_change_without_blocking_other_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, append_owner: str
) -> None:
    """A changed empty-scan result cannot bypass FIFO or indefinitely pin an unrelated owner."""
    monkeypatch.setattr(fwd, "_LIVE_MIRROR_RETRY_BASE_SECONDS", 0.01)
    fwd.append_dead_letter(
        tmp_path,
        session_id="conv_forensic",
        event_type="external_session_usage",
        payload={"context_tokens": 1},
        reason="legacy ambiguity",
        delivered_ambiguous=True,
    )
    original_probe = fwd.has_replayable_dead_letters
    appended = False
    received = asyncio.Event()
    sources: list[str] = []

    def append_after_empty_scan(*args, **kwargs):
        nonlocal appended
        pending = original_probe(*args, **kwargs)
        if not pending and not appended:
            appended = True
            fwd.append_dead_letter(
                tmp_path,
                session_id=append_owner,
                event_type="external_conversation_item",
                payload={"source_id": "native:appended", "item_type": "message"},
                reason="concurrent append after the scan",
            )
        return pending

    monkeypatch.setattr(fwd, "has_replayable_dead_letters", append_after_empty_scan)

    async def handler(request: httpx.Request) -> httpx.Response:
        source_id = json.loads(request.content)["data"]["source_id"]
        if request.url.path == "/v1/sessions/conv_foreign/events":
            return httpx.Response(503, request=request)
        assert request.url.path == "/v1/sessions/conv_current/events"
        sources.append(source_id)
        if source_id == "native:current":
            received.set()
        return httpx.Response(200, request=request)

    token = fwd._live_mirror_recovery.set(None)
    try:
        async with httpx.AsyncClient(
            base_url="http://omnigent.test", transport=httpx.MockTransport(handler)
        ) as client:
            recovery = fwd._LiveMirrorRecovery(client, tmp_path)
            fwd._live_mirror_recovery.set(recovery)
            try:
                assert not await fwd._post_external_item(
                    client,
                    "conv_current",
                    item_type="message",
                    item_data={
                        "role": "user",
                        "content": [{"type": "input_text", "text": "current"}],
                    },
                    response_id="native:turn",
                    source_id="native:current",
                )
                assert sources == []
                recovery.start()
                await asyncio.wait_for(received.wait(), timeout=2)
                expected_records = 1 if append_owner == "conv_current" else 2
                await poll_until_async(
                    lambda: (
                        len((tmp_path / "dead_letter.jsonl").read_text().splitlines())
                        == expected_records
                    ),
                    timeout_s=2,
                )
                assert await fwd._post_external_item(
                    client,
                    "conv_current",
                    item_type="message",
                    item_data={
                        "role": "user",
                        "content": [{"type": "input_text", "text": "newer"}],
                    },
                    response_id="native:newer",
                    source_id="native:newer",
                )
            finally:
                await recovery.close()
    finally:
        fwd._live_mirror_recovery.reset(token)

    assert sources == (
        ["native:appended", "native:current", "native:newer"]
        if append_owner == "conv_current"
        else ["native:current", "native:newer"]
    )


@pytest.mark.asyncio
async def test_productive_live_mirror_batches_keep_the_base_retry_cadence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Healthy bounded batches drain a backlog without exponential failure backoff."""
    record_count = fwd._LIVE_MIRROR_MAX_RECORDS + 1
    expected = [f"native:{index}" for index in range(record_count)]
    for source_id in expected:
        fwd.append_dead_letter(
            tmp_path,
            session_id="conv_batches",
            event_type="external_conversation_item",
            payload={"source_id": source_id},
            reason="synthetic outage backlog",
        )
    delays: list[float] = []
    sources: list[str] = []
    received = asyncio.Event()
    original_sleep = fwd._sleep

    async def observe_delay(delay: float) -> None:
        delays.append(delay)
        await original_sleep(min(delay, 0.001))

    monkeypatch.setattr(fwd, "_sleep", observe_delay)

    async def handler(request: httpx.Request) -> httpx.Response:
        sources.append(json.loads(request.content)["data"]["source_id"])
        if len(sources) == record_count:
            received.set()
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(
        base_url="http://omnigent.test", transport=httpx.MockTransport(handler)
    ) as client:
        recovery = fwd._LiveMirrorRecovery(client, tmp_path)
        recovery.start()
        try:
            await asyncio.wait_for(received.wait(), timeout=2)
            await poll_until_async(
                lambda: not (tmp_path / "dead_letter.jsonl").exists(), timeout_s=2
            )
        finally:
            await recovery.close()
    assert sources == expected
    assert delays[:2] == [fwd._LIVE_MIRROR_RETRY_BASE_SECONDS] * 2


class _SequencedPostClient:
    """Return or raise configured POST outcomes in order."""

    def __init__(self, outcomes: list[httpx.Response | Exception]) -> None:
        self._outcomes = outcomes
        self.calls: list[tuple[str, object, float | None]] = []

    async def post(
        self,
        url: str,
        *,
        json: object,
        timeout: float | None = None,
    ) -> httpx.Response:
        self.calls.append((url, json, timeout))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "data", "durable", "replayable", "prior_transport"),
    [
        (
            "external_conversation_item",
            {"item_type": "message", "source_id": "thread:turn:item"},
            True,
            True,
            False,
        ),
        (
            "external_conversation_item",
            {"item_type": "message", "source_id": "thread:turn:item"},
            True,
            True,
            True,
        ),
        ("external_conversation_item", {"item_type": "message"}, True, False, False),
        ("external_session_usage", {"context_tokens": 1234}, True, False, False),
        ("external_output_text_delta", {"delta": "preview"}, False, False, False),
        ("external_session_status", {"status": "idle"}, False, False, False),
    ],
)
async def test_auth_failure_preserves_durable_events_for_safe_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
    data: dict,
    durable: bool,
    replayable: bool,
    prior_transport: bool,
) -> None:
    """Credential failure retains durable data; only idempotent items replay."""
    failure = DatabricksAuthError("private credential service detail")
    outcomes: list[httpx.Response | Exception] = []
    if prior_transport:
        outcomes.append(
            httpx.ReadTimeout("response lost", request=httpx.Request("POST", "http://test"))
        )
    outcomes.append(failure)
    client = _SequencedPostClient(outcomes)
    monkeypatch.setattr(fwd, "_sleep", AsyncMock())
    fwd._reset_forward_health()
    token = fwd._dead_letter_dir.set(tmp_path)
    try:
        with pytest.raises(DatabricksAuthError) as caught:
            await fwd._post_session_event(
                client,  # type: ignore[arg-type]
                "conv_auth",
                event_type=event_type,
                data=data,
                max_attempts=1 + int(prior_transport),
            )
        assert caught.value is failure
        assert len(client.calls) == 1 + int(prior_transport)
    finally:
        fwd._dead_letter_dir.reset(token)
        fwd._reset_forward_health()

    dead_letter = tmp_path / "dead_letter.jsonl"
    if durable:
        record = json.loads(dead_letter.read_text())
        assert record["payload"] == data
        assert record["session_id"] == "conv_auth"
        assert record["event_type"] == event_type
        assert record["delivered_ambiguous"] is True
        assert record["transport_error"] == "DatabricksAuthError"
        assert record["http_status"] is None
        assert "private credential" not in dead_letter.read_text()
    else:
        assert not dead_letter.exists()

    recovered_client = _RecordingClient()
    await fwd._replay_dead_letters_before_resume(recovered_client, tmp_path)  # type: ignore[arg-type]
    await fwd._replay_dead_letters_before_resume(recovered_client, tmp_path)  # type: ignore[arg-type]
    assert recovered_client.posts == (
        [("/v1/sessions/conv_auth/events", {"type": event_type, "data": data})]
        if replayable
        else []
    )
    assert dead_letter.exists() is (durable and not replayable)


@pytest.mark.asyncio
async def test_post_session_event_inner_classifies_ambiguous_skip() -> None:
    """
    An ambiguous conversation-item transport failure surfaces as ambiguous (#1579).

    The inner used to conflate this with a proven-undelivered failure (both
    returned ``None``); replay must be able to tell them apart.
    """
    client = _RaisingPostClient(
        httpx.ReadTimeout("response lost", request=httpx.Request("POST", "http://test"))
    )
    result = await fwd._post_session_event_inner(
        client,
        "conv_codex1",
        event_type="external_conversation_item",
        data={"item_type": "message"},
    )
    assert result.response is None
    assert result.delivered_ambiguous is True
    assert result.transport_error == "ReadTimeout"
    # Ambiguous items are abandoned immediately — no retries.
    assert client.calls == 1


@pytest.mark.asyncio
async def test_idempotent_conversation_item_retries_ambiguous_failure_until_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stable source id makes response-loss retries duplicate-safe."""
    monkeypatch.setattr(fwd, "_sleep", AsyncMock())
    request = httpx.Request("POST", "http://test")
    client = _SequencedPostClient(
        [
            httpx.ReadTimeout("response lost", request=request),
            httpx.Response(503, request=request),
            httpx.Response(202, request=request),
        ]
    )
    data = {
        "item_type": "message",
        "item_data": {"role": "assistant"},
        "source_id": "thread_1:turn_1:item_1",
    }

    result = await fwd._post_session_event_inner(
        client,  # type: ignore[arg-type]
        "conv_codex1",
        event_type="external_conversation_item",
        data=data,
        max_attempts=None,
        timeout=5.0,
    )

    assert result.response is not None
    assert result.response.status_code == 202
    assert [call[1] for call in client.calls] == [
        {"type": "external_conversation_item", "data": data},
    ] * 3
    assert [call[2] for call in client.calls] == [5.0, 5.0, 5.0]


@pytest.mark.asyncio
async def test_idempotent_conversation_item_bounds_long_source_id() -> None:
    """Long native ids are hashed into the server's source-id limit."""
    client = _RecordingClient()

    posted = await fwd._post_external_item(
        client,  # type: ignore[arg-type]
        "conv_x",
        item_type="message",
        item_data={"role": "assistant", "content": []},
        response_id="codex_turn_1",
        source_id="source:" + ("x" * 300),
    )

    assert posted is True
    source_id = client.posts[0][1]["data"]["source_id"]
    assert isinstance(source_id, str)
    assert source_id.startswith("codex:")
    assert len(source_id) <= 256


@pytest.mark.asyncio
async def test_idempotent_conversation_items_keep_order_while_older_post_is_slow() -> None:
    """Concurrent resume/live delivery cannot let a newer item overtake an older one."""

    class _BlockingFirstPostClient(_RecordingClient):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def post(
            self,
            url: str,
            *,
            json: dict,
            timeout: float | None = None,
        ) -> httpx.Response:
            if not self.posts:
                self.entered.set()
                await self.release.wait()
            return await super().post(url, json=json, timeout=timeout)

    client = _BlockingFirstPostClient()
    token = fwd._conversation_item_locks.set({})
    try:
        older = asyncio.create_task(
            fwd._post_external_item(
                client,  # type: ignore[arg-type]
                "conv_x",
                item_type="message",
                item_data={"role": "assistant", "content": []},
                response_id="codex_turn_1",
                source_id="thread_1:turn_1:item_1",
            )
        )
        await asyncio.wait_for(client.entered.wait(), timeout=5.0)
        newer = asyncio.create_task(
            fwd._post_external_item(
                client,  # type: ignore[arg-type]
                "conv_x",
                item_type="message",
                item_data={"role": "assistant", "content": []},
                response_id="codex_turn_2",
                source_id="thread_1:turn_2:item_2",
            )
        )
        await asyncio.sleep(0)
        assert client.posts == []

        client.release.set()
        assert await asyncio.gather(older, newer) == [True, True]
    finally:
        fwd._conversation_item_locks.reset(token)

    assert [body["data"]["source_id"] for _url, body in client.posts] == [
        "thread_1:turn_1:item_1",
        "thread_1:turn_2:item_2",
    ]


@pytest.mark.asyncio
async def test_cancelled_idempotent_item_is_dead_lettered_for_safe_replay(tmp_path: Path) -> None:
    """Shutdown during an in-flight completion keeps a replayable disk record."""

    class _BlockingPostClient:
        def __init__(self) -> None:
            self.entered = asyncio.Event()

        async def post(
            self,
            url: str,
            *,
            json: dict,
            timeout: float | None = None,
        ) -> httpx.Response:
            self.entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    client = _BlockingPostClient()
    dead_letter_token = fwd._dead_letter_dir.set(tmp_path)
    locks_token = fwd._conversation_item_locks.set({})
    try:
        task = asyncio.create_task(
            fwd._post_external_item(
                client,  # type: ignore[arg-type]
                "conv_x",
                item_type="message",
                item_data={"role": "assistant", "content": []},
                response_id="codex_turn_1",
                source_id="thread_1:turn_1:item_1",
            )
        )
        await asyncio.wait_for(client.entered.wait(), timeout=5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        fwd._conversation_item_locks.reset(locks_token)
        fwd._dead_letter_dir.reset(dead_letter_token)

    import json as _json

    record = _json.loads((tmp_path / "dead_letter.jsonl").read_text().splitlines()[0])
    assert record["delivered_ambiguous"] is True
    assert record["payload"]["source_id"] == "thread_1:turn_1:item_1"


@pytest.mark.asyncio
async def test_post_session_event_inner_classifies_proven_undelivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A connect failure exhausted after retries is proven-undelivered, not ambiguous.
    """
    monkeypatch.setattr(fwd, "_sleep", AsyncMock())
    client = _RaisingPostClient(
        httpx.ConnectError("refused", request=httpx.Request("POST", "http://test"))
    )
    result = await fwd._post_session_event_inner(
        client,
        "conv_codex1",
        event_type="external_conversation_item",
        data={"item_type": "message"},
    )
    assert result.response is None
    assert result.delivered_ambiguous is False
    assert result.transport_error == "ConnectError"
    # Connect failures are safe to retry, so all attempts are spent.
    assert client.calls == fwd._POST_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_post_session_event_dead_letters_ambiguous_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An ambiguous-skip drop is dead-lettered with ``delivered_ambiguous=True`` (#1579).

    :param tmp_path: Pytest temp dir standing in for the bridge dir.
    :param monkeypatch: Pytest patcher (auto-restores the stubbed inner).
    """
    import json as _json

    fwd._reset_forward_health()

    async def _ambiguous_inner(client, session_id, *, event_type, data, max_attempts, timeout):
        return fwd._PostResult(
            response=None, delivered_ambiguous=True, transport_error="ReadTimeout"
        )

    monkeypatch.setattr(fwd, "_post_session_event_inner", _ambiguous_inner)
    token = fwd._dead_letter_dir.set(tmp_path)
    try:
        await fwd._post_session_event(
            MagicMock(),
            "conv_codex1",
            event_type="external_conversation_item",
            data={"item_type": "message"},
        )
    finally:
        fwd._dead_letter_dir.reset(token)
        fwd._reset_forward_health()

    record = _json.loads((tmp_path / "dead_letter.jsonl").read_text().splitlines()[0])
    assert record["delivered_ambiguous"] is True
    assert record["http_status"] is None
    assert record["transport_error"] == "ReadTimeout"


@pytest.mark.asyncio
async def test_post_session_event_dead_letters_records_http_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A status-bearing failure records ``http_status`` and is not ambiguous (#1579).

    :param tmp_path: Pytest temp dir standing in for the bridge dir.
    :param monkeypatch: Pytest patcher (auto-restores the stubbed inner).
    """
    import json as _json

    fwd._reset_forward_health()

    async def _failing_inner(client, session_id, *, event_type, data, max_attempts, timeout):
        return fwd._PostResult(
            response=httpx.Response(503, request=httpx.Request("POST", "http://test"))
        )

    monkeypatch.setattr(fwd, "_post_session_event_inner", _failing_inner)
    token = fwd._dead_letter_dir.set(tmp_path)
    try:
        await fwd._post_session_event(
            MagicMock(),
            "conv_codex1",
            event_type="external_conversation_item",
            data={"item_type": "message"},
        )
    finally:
        fwd._dead_letter_dir.reset(token)
        fwd._reset_forward_health()

    record = _json.loads((tmp_path / "dead_letter.jsonl").read_text().splitlines()[0])
    assert record["http_status"] == 503
    assert record["delivered_ambiguous"] is False
    assert record["transport_error"] is None


@pytest.mark.asyncio
async def test_replay_dead_letters_before_resume_reposts_proven_undelivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Before resume, a proven-undelivered record is re-POSTed and removed (#1579).

    :param tmp_path: Pytest temp dir standing in for the bridge dir.
    :param monkeypatch: Pytest patcher (auto-restores the stubbed inner).
    """
    fwd.append_dead_letter(
        tmp_path,
        session_id="conv_codex1",
        event_type="external_conversation_item",
        payload={"item_type": "message"},
        reason="proven-undelivered transport failure after retries",
        delivered_ambiguous=False,
        http_status=None,
        transport_error="ConnectError",
    )

    posted: list[dict] = []

    async def _ok_inner(client, session_id, *, event_type, data, max_attempts, timeout):
        posted.append(
            {
                "session_id": session_id,
                "event_type": event_type,
                "data": data,
                "max_attempts": max_attempts,
                "timeout": timeout,
            }
        )
        return fwd._PostResult(
            response=httpx.Response(200, request=httpx.Request("POST", "http://test"))
        )

    monkeypatch.setattr(fwd, "_post_session_event_inner", _ok_inner)
    await fwd._replay_dead_letters_before_resume(MagicMock(), tmp_path)

    assert len(posted) == 1
    assert posted[0]["session_id"] == "conv_codex1"
    assert posted[0]["event_type"] == "external_conversation_item"
    assert posted[0]["data"] == {"item_type": "message"}
    # Replay re-POSTs with a single attempt and a short timeout so a large file
    # or a hung server cannot stall startup.
    assert posted[0]["max_attempts"] == 1
    assert posted[0]["timeout"] == fwd._REPLAY_POST_TIMEOUT_SECONDS
    # Delivered → record removed.
    assert not (tmp_path / "dead_letter.jsonl").exists()


@pytest.mark.asyncio
async def test_replay_dead_letters_before_resume_skips_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Before resume, an ambiguous record is never re-POSTed and is retained (#1579).

    :param tmp_path: Pytest temp dir standing in for the bridge dir.
    :param monkeypatch: Pytest patcher (auto-restores the stubbed inner).
    """
    fwd.append_dead_letter(
        tmp_path,
        session_id="conv_codex1",
        event_type="external_conversation_item",
        payload={"item_type": "message"},
        reason="ambiguous transport failure (may already be committed)",
        delivered_ambiguous=True,
    )

    called = False

    async def _inner(client, session_id, *, event_type, data, **_kwargs):
        nonlocal called
        called = True
        return fwd._PostResult(
            response=httpx.Response(200, request=httpx.Request("POST", "http://test"))
        )

    monkeypatch.setattr(fwd, "_post_session_event_inner", _inner)
    await fwd._replay_dead_letters_before_resume(MagicMock(), tmp_path)

    assert called is False
    # Ambiguous record retained as a forensic record.
    assert (tmp_path / "dead_letter.jsonl").exists()


class _RecordingPostClient:
    """Async client stub that records each ``post`` call's kwargs."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.calls: list[dict] = []

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append(kwargs)
        return self._response


@pytest.mark.asyncio
async def test_post_session_event_inner_single_attempt_and_timeout() -> None:
    """
    ``max_attempts=1`` makes one POST (no retry) and ``timeout`` is threaded through.

    Replay relies on both so a hung server fails fast and startup is bounded (#1579).
    """
    client = _RecordingPostClient(
        httpx.Response(503, request=httpx.Request("POST", "http://test"))
    )
    result = await fwd._post_session_event_inner(
        client,
        "conv_codex1",
        event_type="external_conversation_item",
        data={"item_type": "message"},
        max_attempts=1,
        timeout=5.0,
    )
    # A single attempt even though 503 is normally retryable.
    assert len(client.calls) == 1
    assert client.calls[0]["timeout"] == 5.0
    assert result.response is not None
    assert result.response.status_code == 503


async def test_post_session_event_records_connectivity_failure_for_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex's exhausted-retry POST failure is recorded for the idle watchdog.

    Writer half of issue #1119 for the codex forwarder: when every attempt to
    POST a session event raises a connect error, ``_post_session_event`` must
    record the failure in ``_native_forwarder_health`` (via
    ``_log_post_transport_failure``) so the harness idle-turn watchdog can name
    the connectivity cause instead of a generic "wedged LLM" reason.
    """
    from omnigent.native import _native_forwarder_health as health

    class _AlwaysConnectError:
        """Stub client whose every POST fails to connect."""

        async def post(self, url: str, *, json: object) -> httpx.Response:
            """Raise a connect error mimicking an unreachable server."""
            del json
            raise httpx.ConnectError("No route to host", request=httpx.Request("POST", url))

    async def _no_sleep(_seconds: float) -> None:
        """No-op sleep so the retry loop doesn't add real delay."""

    monkeypatch.setattr(fwd, "_sleep", _no_sleep)

    health.clear()
    try:
        result = await fwd._post_session_event(
            _AlwaysConnectError(),  # type: ignore[arg-type]
            "conv_x",
            event_type="external_session_status",
            data={},
        )
        assert result is None
        detail = health.recent_post_failure(60.0)
        assert detail is not None
        assert "external_session_status" in detail
        assert "No route to host" in detail
    finally:
        health.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_probe", ["revision", "read"])
async def test_admission_read_failure_retains_the_original_mirror_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_probe: str
) -> None:
    """An unreadable older journal cannot burn or prematurely settle a newer mirror."""
    old_payload = {"source_id": "native:older", "item_type": "message"}
    fwd.append_dead_letter(
        tmp_path,
        session_id="conv_read_repair",
        event_type="external_conversation_item",
        payload=old_payload,
        reason="synthetic original failure",
    )
    accepted: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        accepted.append(json.loads(request.content)["data"])
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(
        base_url="http://omnigent.test", transport=httpx.MockTransport(handler)
    ) as client:
        recovery = fwd._LiveMirrorRecovery(client, tmp_path)
        target = recovery if failed_probe == "revision" else fwd
        name = "_revision" if failed_probe == "revision" else "has_replayable_dead_letters"
        original_probe = getattr(target, name)
        repaired = False

        def pending_probe(*args, **kwargs):
            if not repaired:
                raise OSError(errno.EIO, "synthetic journal admission read failure")
            return original_probe(*args, **kwargs)

        monkeypatch.setattr(target, name, pending_probe)
        token = fwd._live_mirror_recovery.set(recovery)
        try:
            content = [{"type": "input_text", "text": "original newer input"}]
            posted = await fwd._post_external_item(
                client,
                "conv_read_repair",
                item_type="message",
                item_data={"role": "user", "content": content},
                response_id="codex_turn",
                source_id="native:newer",
            )
            retained = [
                json.loads(line)["payload"]
                for line in (tmp_path / "dead_letter.jsonl").read_text().splitlines()
            ]
            assert posted is False and accepted == []
            assert retained[0] == old_payload
            assert retained[1]["item_data"] == {"role": "user", "content": content}
            repaired = True
            await recovery._drain()
            assert accepted == retained
            assert not (tmp_path / "dead_letter.jsonl").exists()
        finally:
            await recovery.close()
            fwd._live_mirror_recovery.reset(token)
