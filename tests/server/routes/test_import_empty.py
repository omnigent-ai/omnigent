"""Empty sessions are skipped, not failed, in import streams and buffered endpoints.

Server + host over a real tunnel: what each side reports for empty sessions.
Empty sessions raise SessionImportEmptyError, the host reports code 'session_empty',
the server counts them under 'skipped' instead of 'failed', and CLI/UI report them
as a note without failing the batch.
"""

from __future__ import annotations

import logging

import pytest

from omnigent.host.frames import HostImportLocalDoneFrame
from omnigent.server.routes import imports as imports_module
from omnigent.session_import.models import (
    SessionImportEmptyError,
    SessionImportNotFoundError,
)
from tests.server.import_tunnel_harness import (
    FakeConversationStore,
    TunnelPair,
    client,
    host_record,
    imports_app,
    local_session,
    ndjson,
    serve_local_sessions,
)


@pytest.mark.asyncio
async def test_stream_reports_empty_sessions_as_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stream endpoint reports empty sessions as skipped events with code 'session_empty'."""
    sessions = {
        "good-1": local_session("good-1"),
        "empty-1": SessionImportEmptyError("Codex session 'empty-1' has no importable history"),
        "bad-1": OSError("disk"),
        "empty-2": SessionImportEmptyError("Codex session 'empty-2' has no importable history"),
    }
    serve_local_sessions(monkeypatch, sessions)

    store = FakeConversationStore()
    pair = TunnelPair()
    app = imports_app(store, host_registry=pair.registry, host=host_record())
    async with pair:
        async with client(app) as http:
            response = await http.post(
                "/v1/imports/local/stream",
                json={
                    "host_id": "host_0123456789abcdef0123456789abcdef",
                    "source": "all",
                    "limit": 25,
                },
            )

    assert response.status_code == 200, response.text
    events = ndjson(response)
    done = events[-1]

    assert done["event"] == "done"
    assert (done["imported"], done["already_imported"], done["failed"], done["skipped"]) == (
        1,
        0,
        1,
        2,
    )
    assert done["complete"] is True
    assert len(store.conversations) == 1

    # Clients that predate `skipped` read only `failed` events and
    # `failures`, so the empty sessions never show up as errors there.
    failed = [e for e in events if e["event"] == "failed"]
    assert [e["external_session_id"] for e in failed] == ["bad-1"]
    assert [f["external_session_id"] for f in done["failures"]] == ["bad-1"]

    skipped = [e for e in events if e["event"] == "skipped"]
    assert sorted(e["external_session_id"] for e in skipped) == ["empty-1", "empty-2"]

    for entry in [*skipped, *done["skipped_sessions"]]:
        assert entry["code"] == "session_empty"
        assert entry["retryable"] is False
        assert entry["error_id"] is None
        assert entry["reason"].endswith("has no importable history")

    assert len(done["skipped_sessions"]) == done["skipped"]

    # Skipped lines follow the failures, before `done`.
    kinds = [e["event"] for e in events]
    assert kinds.index("failed") < kinds.index("skipped") < kinds.index("done")


@pytest.mark.asyncio
async def test_host_still_counts_empty_sessions_as_failed_for_older_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host reports empty sessions in failures list; old servers read only that."""
    sessions = {
        "good-1": local_session("good-1"),
        "empty-1": SessionImportEmptyError("Codex session 'empty-1' has no importable history"),
        "bad-1": OSError("disk"),
        "empty-2": SessionImportEmptyError("Codex session 'empty-2' has no importable history"),
    }
    serve_local_sessions(monkeypatch, sessions)

    store = FakeConversationStore()
    pair = TunnelPair()
    app = imports_app(store, host_registry=pair.registry, host=host_record())
    async with pair:
        async with client(app) as http:
            response = await http.post(
                "/v1/imports/local/stream",
                json={
                    "host_id": "host_0123456789abcdef0123456789abcdef",
                    "source": "all",
                    "limit": 25,
                },
            )

    assert response.status_code == 200
    (done,) = [f for f in pair.host_frames() if isinstance(f, HostImportLocalDoneFrame)]

    # A server that predates `session_empty` reads `failed`/`failures` only.
    assert done.failed == 3
    codes = {f["external_session_id"]: f.get("code") for f in done.failures}
    assert codes == {"empty-1": "session_empty", "bad-1": None, "empty-2": "session_empty"}
    assert all(isinstance(f["reason"], str) and f["reason"] for f in done.failures)


@pytest.mark.asyncio
async def test_old_host_text_only_failures_are_classified_as_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosts that predate SessionImportEmptyError send text-only; server classifies it."""
    sessions = {
        "good-1": local_session("good-1"),
        "empty-1": SessionImportNotFoundError("Codex session 'empty-1' has no importable history"),
        "gone-1": SessionImportNotFoundError("Codex session 'gone-1' was not found"),
    }
    serve_local_sessions(monkeypatch, sessions)

    store = FakeConversationStore()
    pair = TunnelPair(legacy_host=True)
    app = imports_app(store, host_registry=pair.registry, host=host_record())
    async with pair:
        async with client(app) as http:
            response = await http.post(
                "/v1/imports/local/stream",
                json={
                    "host_id": "host_0123456789abcdef0123456789abcdef",
                    "source": "all",
                    "limit": 25,
                },
            )

    assert response.status_code == 200
    (host_done,) = [f for f in pair.host_frames() if isinstance(f, HostImportLocalDoneFrame)]
    assert all("code" not in f for f in host_done.failures)

    done = ndjson(response)[-1]
    assert (done["imported"], done["failed"], done["skipped"]) == (1, 1, 1)
    assert [f["external_session_id"] for f in done["skipped_sessions"]] == ["empty-1"]
    assert done["skipped_sessions"][0]["code"] == "session_empty"
    assert [f["code"] for f in done["failures"]] == ["session_unreadable"]


@pytest.mark.asyncio
async def test_only_empty_sessions_is_a_complete_import_with_nothing_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batch of only empty sessions: complete=True, failed=0, skipped=1+."""
    sessions = {
        "empty-1": SessionImportEmptyError("Codex session 'empty-1' has no importable history"),
    }
    serve_local_sessions(monkeypatch, sessions)

    store = FakeConversationStore()
    pair = TunnelPair()
    app = imports_app(store, host_registry=pair.registry, host=host_record())
    async with pair:
        async with client(app) as http:
            response = await http.post(
                "/v1/imports/local/stream",
                json={
                    "host_id": "host_0123456789abcdef0123456789abcdef",
                    "source": "all",
                    "limit": 25,
                },
            )

    assert response.status_code == 200
    done = ndjson(response)[-1]
    assert (done["imported"], done["failed"], done["skipped"], done["complete"]) == (0, 0, 1, True)
    assert done["failures"] == []
    assert store.conversations == {}


@pytest.mark.asyncio
async def test_buffered_endpoint_reports_skipped_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Buffered /v1/imports/local endpoint also reports skipped_sessions."""
    sessions = {
        "good-1": local_session("good-1"),
        "empty-1": SessionImportEmptyError("Codex session 'empty-1' has no importable history"),
        "bad-1": OSError("disk"),
        "empty-2": SessionImportEmptyError("Codex session 'empty-2' has no importable history"),
    }
    serve_local_sessions(monkeypatch, sessions)

    store = FakeConversationStore()
    pair = TunnelPair()
    app = imports_app(store, host_registry=pair.registry, host=host_record())
    async with pair:
        async with client(app) as http:
            response = await http.post(
                "/v1/imports/local",
                json={
                    "host_id": "host_0123456789abcdef0123456789abcdef",
                    "source": "all",
                    "limit": 25,
                },
            )

    assert response.status_code == 200
    body = response.json()
    assert (body["imported"], body["failed"], body["skipped"]) == (1, 1, 2)
    assert [f["external_session_id"] for f in body["failures"]] == ["bad-1"]
    assert sorted(f["external_session_id"] for f in body["skipped_sessions"]) == [
        "empty-1",
        "empty-2",
    ]


@pytest.mark.asyncio
async def test_outcome_log_counts_skipped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Outcome log includes skipped count in the message."""
    sessions = {
        "good-1": local_session("good-1"),
        "empty-1": SessionImportEmptyError("Codex session 'empty-1' has no importable history"),
        "bad-1": OSError("disk"),
        "empty-2": SessionImportEmptyError("Codex session 'empty-2' has no importable history"),
    }
    serve_local_sessions(monkeypatch, sessions)

    store = FakeConversationStore()
    pair = TunnelPair()
    app = imports_app(store, host_registry=pair.registry, host=host_record())

    with caplog.at_level(logging.INFO, logger=imports_module.__name__):
        async with pair:
            async with client(app) as http:
                response = await http.post(
                    "/v1/imports/local/stream",
                    json={
                        "host_id": "host_0123456789abcdef0123456789abcdef",
                        "source": "all",
                        "limit": 25,
                    },
                )

    assert response.status_code == 200
    (finished,) = [r for r in caplog.records if "Local session import finished" in r.getMessage()]
    assert finished.getMessage().endswith("imported=1 already_imported=0 failed=1 skipped=2")
