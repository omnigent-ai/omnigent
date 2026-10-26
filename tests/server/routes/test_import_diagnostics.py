"""Import failures say what went wrong.

An unreadable session carries a fixed ``read_failure`` reason, a CLI import
naming a host this server never registered is rejected with
``host_not_connected``, and pre-stream rejections carry ``import_code``,
``host_id``, and 422 schema paths on the audit row.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from omnigent.db.utils import clear_engine_cache
from omnigent.debug_logging import current_request_audit_attrs, reset_request_audit_attrs
from omnigent.host import connect as host_connect
from omnigent.runtime import init as init_runtime
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import app as app_module
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.routes import imports as imports_module
from omnigent.session_import.errors import (
    SESSION_READ_FAILURE_REASONS,
    ImportErrorCode,
    SessionReadFailure,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from tests.server.import_tunnel_harness import (
    HOST_ID,
    FakeConversationStore,
    TunnelPair,
    client,
    host_record,
    imports_app,
    local_import_body,
    local_session,
    ndjson,
    serve_local_sessions,
)

_CLI_IMPORT_BODY: dict[str, Any] = {
    "source": "codex",
    "external_session_id": "0199a5c0-1234-7abc-8def-0123456789ab",
    "workspace": "/repo",
    "items": [
        {
            "type": "message",
            "response_id": "r",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        }
    ],
}


def _events(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    return [
        dict(getattr(record, "attributes", {}))
        for record in caplog.records
        if getattr(record, "event_name", None) == name
    ]


async def _stream(store: FakeConversationStore) -> list[dict[str, Any]]:
    pair = TunnelPair()
    app = imports_app(store, host_registry=pair.registry, host=host_record())
    async with pair, client(app) as http:
        response = await http.post("/v1/imports/local/stream", json=local_import_body())
    assert response.status_code == 200, response.text
    return ndjson(response)


async def test_each_unreadable_session_says_why_without_local_paths(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bad_utf8 = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    cases = {
        "missing": (
            FileNotFoundError(2, "No such file", "/Users/a/.codex/x.jsonl"),
            SessionReadFailure.FILE_MISSING,
        ),
        "denied": (
            PermissionError(13, "Permission denied", "/Users/a/.claude/y.jsonl"),
            SessionReadFailure.PERMISSION_DENIED,
        ),
        "encoding": (bad_utf8, SessionReadFailure.INVALID_ENCODING),
        "malformed": (
            json.JSONDecodeError("Expecting value", "{", 1),
            SessionReadFailure.MALFORMED_RECORD,
        ),
        "format": (
            ValueError("unexpected record /Users/a/secret"),
            SessionReadFailure.UNSUPPORTED_FORMAT,
        ),
        "disk": (OSError(5, "I/O error"), SessionReadFailure.READ_ERROR),
    }
    sessions: dict[str, Any] = {"good": local_session("good")}
    sessions.update({sid: exc for sid, (exc, _) in cases.items()})
    serve_local_sessions(monkeypatch, sessions)
    with caplog.at_level(logging.INFO, logger=imports_module.__name__):
        events = await _stream(FakeConversationStore())

    failed = {e["external_session_id"]: e for e in events if e["event"] == "failed"}
    assert set(failed) == set(cases)
    for sid, (_exc, read_failure) in cases.items():
        assert failed[sid]["code"] == ImportErrorCode.SESSION_UNREADABLE
        assert failed[sid]["read_failure"] == read_failure
        assert failed[sid]["reason"] == SESSION_READ_FAILURE_REASONS[read_failure]
    assert "/Users/" not in json.dumps(events)
    logged = {e["external_session_id"]: e for e in _events(caplog, "import_session_failed")}
    assert {sid: logged[sid]["read_failure"] for sid in cases} == {
        sid: read_failure for sid, (_exc, read_failure) in cases.items()
    }


async def test_server_drops_an_unknown_read_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_local_sessions(monkeypatch, {"bad": ValueError("x")})
    monkeypatch.setattr(
        host_connect,
        "_unreadable_session_failure",
        lambda _exc: {"reason": "Made up.", "read_failure": "made_up"},
    )
    events = await _stream(FakeConversationStore())
    failed = next(e for e in events if e["event"] == "failed")
    assert failed["read_failure"] is None


async def test_cli_import_with_an_unregistered_host_says_to_connect_it() -> None:
    store = FakeConversationStore()
    app = imports_app(store, host=None, user_id="u@x.com")
    reset_request_audit_attrs()
    async with client(app) as http:
        response = await http.post(
            "/v1/imports", json={**_CLI_IMPORT_BODY, "host_id": "host_never_connected_here"}
        )
    assert response.status_code == 404, response.text
    error = response.json()["error"]
    assert error["import_code"] == ImportErrorCode.HOST_NOT_CONNECTED
    assert "omnigent host --server" in error["message"]
    assert current_request_audit_attrs()["host_id"] == "host_never_connected_here"
    assert not store.conversations


async def test_another_users_host_is_still_refused() -> None:
    other = types.SimpleNamespace(**{**host_record().__dict__, "user_id": "someone@else.com"})
    app = imports_app(FakeConversationStore(), host=other, user_id="u@x.com")  # type: ignore[arg-type]
    async with client(app) as http:
        response = await http.post("/v1/imports", json={**_CLI_IMPORT_BODY, "host_id": HOST_ID})
    assert response.status_code == 403, response.text


class _AuditApp:
    """The real server app, recording each audit event's final attributes."""

    def __init__(self, hosts: HostStore, http: TestClient) -> None:
        self.hosts = hosts
        self.http = http
        self.audit: list[tuple[str, str, dict[str, str]]] = []

    def final(self, operation: str) -> dict[str, str]:
        return next(
            attrs
            for op, phase, attrs in reversed(self.audit)
            if op == operation and phase != "start"
        )


@pytest.fixture
def audit_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_AuditApp]:
    metadata_uri = f"sqlite:///{tmp_path / 'metadata.db'}"
    conversation_uri = f"sqlite:///{tmp_path / 'conversations.db'}"
    hosts = HostStore(metadata_uri)
    agents = SqlAlchemyAgentStore(metadata_uri, conversation_uri)
    conversations = SqlAlchemyConversationStore(metadata_uri, conversation_uri)
    files = SqlAlchemyFileStore(metadata_uri)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    cache = AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache")
    init_runtime(
        conversation_store=conversations,
        agent_store=agents,
        agent_cache=cache,
        file_store=files,
        artifact_store=artifacts,
    )
    app = create_app(
        agent_store=agents,
        conversation_store=conversations,
        file_store=files,
        artifact_store=artifacts,
        agent_cache=cache,
        host_store=hosts,
        auth_provider=UnifiedAuthProvider("header", local_single_user=False),
    )
    try:
        with TestClient(app, headers={"X-Forwarded-Email": "u@x.com"}) as http:
            recorder = _AuditApp(hosts, http)

            def record(
                operation: str, phase: str, *, session_id: str | None, **attributes: str
            ) -> None:
                recorder.audit.append((operation, phase, attributes))

            monkeypatch.setattr(app_module, "_emit_audit_event", record)
            yield recorder
    finally:
        clear_engine_cache()


def test_offline_host_409_names_the_import_code_and_host(audit_app: _AuditApp) -> None:
    audit_app.hosts.upsert_on_connect(HOST_ID, "studio-mac", "u@x.com")
    audit_app.hosts.set_offline(HOST_ID)
    response = audit_app.http.post(
        "/v1/imports/local/stream", json=local_import_body(host_id=HOST_ID)
    )
    assert response.status_code == 409, response.text
    attrs = audit_app.final("import_local_sessions_stream")
    assert attrs["import_code"] == ImportErrorCode.HOST_OFFLINE
    assert attrs["host_id"] == HOST_ID
    assert attrs["status"] == "409"


def test_validation_422_names_the_rejected_field(audit_app: _AuditApp) -> None:
    response = audit_app.http.post(
        "/v1/imports/local/stream",
        json=local_import_body(host_id=HOST_ID, limit=5000, source="nope"),
    )
    assert response.status_code == 422, response.text
    attrs = audit_app.final("import_local_sessions_stream")
    entries = attrs["validation_errors"].split(";")
    assert attrs["validation_error_count"] == str(len(entries))
    assert "body.limit:less_than_equal" in entries
    assert any(entry.startswith("body.source") for entry in entries)
    assert "5000" not in attrs["validation_errors"]
    assert "nope" not in attrs["validation_errors"]


def test_validation_paths_drop_values_and_indices() -> None:
    summary = app_module._validation_audit_errors(
        [
            {"loc": ("body", "items", 3, "data"), "type": "missing"},
            {"loc": ("body", "metadata", "user typed key!"), "type": "string_type"},
            {"loc": "not-a-tuple", "type": "x y"},
            *({"loc": ("body", f"f{n}"), "type": "missing"} for n in range(20)),
        ]
    )
    entries = summary.split(";")
    assert entries[:3] == ["body.items.*.data:missing", "body.metadata.?:string_type", "?:?"]
    assert len(entries) == 8


async def test_a_session_the_harness_archived_lands_archived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve_local_sessions(
        monkeypatch,
        {
            "kept": local_session("kept"),
            "archived": dataclasses.replace(local_session("archived"), archived=True),
        },
    )
    store = FakeConversationStore()
    events = await _stream(store)
    assert sum(e["event"] == "session" for e in events) == 2
    archived = {
        sid: getattr(store.conversations[store.external[sid]], "archived", False)
        for sid in ("kept", "archived")
    }
    assert archived == {"kept": False, "archived": True}
