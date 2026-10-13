"""Import outcome records: server finish/failure events and audit attributes."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest

from omnigent.debug_logging import current_request_audit_attrs, reset_request_audit_attrs
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes import imports as imports_module
from omnigent.session_import.errors import ImportErrorCode
from tests.server.import_tunnel_harness import (
    FakeConversationStore,
    TunnelPair,
    cli_import_body,
    client,
    fail_append_for,
    host_record,
    imports_app,
    local_import_body,
    local_session,
    serve_local_sessions,
    wait_until,
)


def _events(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    """The attributes of every server-side record for debug event ``name``."""
    return [
        dict(getattr(record, "attributes", {}))
        for record in caplog.records
        if record.name == imports_module.__name__ and getattr(record, "event_name", None) == name
    ]


def _silent_tunnel(monkeypatch: pytest.MonkeyPatch) -> TunnelPair:
    """A registered tunnel that has been silent long enough to be unreachable."""
    monkeypatch.setattr(imports_module, "PING_INTERVAL_S", 60.0)
    pair = TunnelPair()
    pair.conn.last_frame_at = time.time() - 600
    return pair


@pytest.mark.parametrize("failing", [None, "s1"], ids=["ok", "partial"])
async def test_stream_records_tally_and_each_failure_code(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failing: str | None
) -> None:
    """A stream records its outcome and tally, and one event per failed session (none if clean)."""
    store = FakeConversationStore()
    if failing is not None:
        fail_append_for(store, failing, RuntimeError("storage blew up"))
    pair = TunnelPair()
    app = imports_app(store, host_registry=pair.registry, host=host_record())
    serve_local_sessions(monkeypatch, {f"s{i}": local_session(f"s{i}") for i in range(3)})
    with caplog.at_level(logging.INFO, logger=imports_module.__name__):
        async with pair, client(app) as http:
            await http.post("/v1/imports/local/stream", json=local_import_body())

    (finished,) = _events(caplog, "import_local_finished")
    assert finished["route"] == "imports_local_stream"
    assert finished["code"] is None
    assert isinstance(finished["duration_ms"], int)
    failed_events = _events(caplog, "import_session_failed")
    if failing is None:
        assert finished["outcome"] == "ok"
        assert (finished["imported"], finished["failed"], finished["total"]) == (3, 0, 3)
        assert finished["failure_codes"] is None
        assert failed_events == []
        return
    assert finished["outcome"] == "partial"
    assert (finished["imported"], finished["failed"], finished["total"]) == (2, 1, 3)
    assert finished["failure_codes"] == f"{ImportErrorCode.INTERNAL}:1"
    (failed,) = failed_events
    assert (failed["code"], failed["external_session_id"], failed["retryable"]) == (
        ImportErrorCode.INTERNAL,
        "s1",
        True,
    )


async def test_whole_import_error_is_recorded_with_its_code(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A whole-import failure records outcome error with its code and error id."""
    pair = _silent_tunnel(monkeypatch)
    app = imports_app(FakeConversationStore(), host_registry=pair.registry, host=host_record())
    with caplog.at_level(logging.INFO, logger=imports_module.__name__):
        async with client(app) as http:
            await http.post("/v1/imports/local/stream", json=local_import_body())
    (finished,) = _events(caplog, "import_local_finished")
    assert finished["outcome"] == "error"
    assert finished["code"] == ImportErrorCode.HOST_UNREACHABLE
    assert str(finished["error_id"]).startswith("err_")


async def test_client_gone_mid_stream_is_recorded_as_interrupted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A stream cancelled by a departing client still records its outcome."""
    pair = TunnelPair()
    app = imports_app(FakeConversationStore(), host_registry=pair.registry, host=host_record())
    serve_local_sessions(monkeypatch, {"s0": local_session("s0")}, load_delay_s=1.0)
    with caplog.at_level(logging.INFO, logger=imports_module.__name__):
        async with pair, client(app) as http:
            request = asyncio.create_task(
                http.post("/v1/imports/local/stream", json=local_import_body())
            )
            await wait_until(lambda: bool(pair.conn.pending_import_local))
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
    (finished,) = _events(caplog, "import_local_finished")
    assert finished["outcome"] == "error"
    assert finished["code"] == ImportErrorCode.STREAM_INTERRUPTED
    assert finished["error_id"] is None


async def test_buffered_route_puts_the_import_code_on_the_audit_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The buffered route adds the failure's import code and tally to the audit attributes."""
    pair = _silent_tunnel(monkeypatch)
    app = imports_app(FakeConversationStore(), host_registry=pair.registry, host=host_record())
    reset_request_audit_attrs()  # what the server middleware does per request
    async with client(app) as http:
        await http.post("/v1/imports/local", json=local_import_body())
    attrs = current_request_audit_attrs()
    assert attrs["import_code"] == ImportErrorCode.HOST_UNREACHABLE
    assert attrs["error_id"].startswith("err_")
    assert attrs["imported"] == "0"


async def test_cli_import_failure_reaches_the_audit_envelope() -> None:
    """An unclassified ``/v1/imports`` storage failure is tagged internal on the audit envelope."""
    store = FakeConversationStore()

    def on_append(_conversation_id: str, _items: list[Any]) -> None:
        raise RuntimeError("unclassified storage failure")

    store.on_append = on_append
    app = imports_app(store, host_registry=HostRegistry(), host=host_record())
    reset_request_audit_attrs()
    async with client(app, raise_app_exceptions=False) as http:
        await http.post("/v1/imports", json=cli_import_body())
    assert current_request_audit_attrs()["import_code"] == ImportErrorCode.INTERNAL


async def test_cli_duplicate_reaches_the_audit_envelope() -> None:
    """A duplicate ``/v1/imports`` is tagged already_imported on the audit envelope."""
    store = FakeConversationStore()
    app = imports_app(store, host_registry=HostRegistry(), host=host_record())
    async with client(app) as http:
        assert (await http.post("/v1/imports", json=cli_import_body())).status_code == 201
        reset_request_audit_attrs()
        assert (await http.post("/v1/imports", json=cli_import_body())).status_code == 409
    assert current_request_audit_attrs()["import_code"] == ImportErrorCode.ALREADY_IMPORTED
