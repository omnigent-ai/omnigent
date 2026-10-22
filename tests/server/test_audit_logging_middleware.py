"""Tests for the server request audit-logging helpers.

Covers the two pieces the ``_record_server_metrics`` middleware and the
exception handlers rely on: resolving the operation + session id from the
matched route before routing, and emitting a gated, table-only audit row.
"""

from __future__ import annotations

import logging
import threading

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from omnigent import debug_logging as dl
from omnigent.server.app import _resolve_audit_route


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/v1/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, str]:  # pragma: no cover - not called
        return {}

    @app.get("/v1/sessions")
    def list_sessions() -> list[str]:  # pragma: no cover - not called
        return []

    @app.get("/v1/hosts")
    def list_hosts() -> list[str]:  # pragma: no cover - not called
        return []

    return app


def _request(app: FastAPI, method: str, path: str) -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "app": app,
        "router": app.router,
    }
    return Request(scope)


def test_resolve_audit_route_session_scoped_extracts_id() -> None:
    app = _app()
    operation, template, session_id = _resolve_audit_route(
        _request(app, "GET", "/v1/sessions/abc123")
    )
    assert operation == "get_session"
    assert template == "/v1/sessions/{session_id}"
    assert session_id == "abc123"


def test_resolve_audit_route_list_has_no_session_id() -> None:
    # The list route must NOT capture a spurious session id (the loose path
    # regex would treat a literal next segment as one; the route param does not).
    app = _app()
    operation, _template, session_id = _resolve_audit_route(_request(app, "GET", "/v1/sessions"))
    assert operation == "list_sessions"
    assert session_id is None


def test_resolve_audit_route_non_session_route() -> None:
    app = _app()
    operation, _template, session_id = _resolve_audit_route(_request(app, "GET", "/v1/hosts"))
    assert operation == "list_hosts"
    assert session_id is None


def test_resolve_audit_route_unmatched() -> None:
    app = _app()
    assert _resolve_audit_route(_request(app, "GET", "/nope")) == (
        "unmatched",
        "<unmatched>",
        None,
    )


def test_emit_audit_event_is_noop_when_sink_disabled() -> None:
    # Gated on the debug sink: with no sink, building/emitting is skipped
    # entirely, so nothing reaches the audit logger.
    from omnigent.server.app import _emit_audit_event

    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Capture()
    audit_logger = dl.audit_event_logger()
    audit_logger.addHandler(handler)
    try:
        assert not dl.debug_sink_enabled()
        _emit_audit_event(
            "get_session", "start", session_id="s1", route="/v1/sessions/{session_id}"
        )
        assert captured == []
    finally:
        audit_logger.removeHandler(handler)


def test_emit_audit_event_ships_operation_and_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.server.app import _emit_audit_event

    monkeypatch.setattr(dl.DebugLogHandler, "_FLUSH_WAIT", 0.01)
    monkeypatch.setattr(dl, "_active_sink", None)
    batches: list[list[dl.DebugLogRow]] = []
    delivered = threading.Event()

    def send(batch: list[dl.DebugLogRow]) -> None:
        batches.append(batch)
        delivered.set()

    dl.attach_debug_log_sink([], source="server", level=logging.INFO, send=send)
    sink = dl._active_sink
    assert sink is not None
    try:
        _emit_audit_event(
            "get_session",
            "ok",
            session_id="conv_1",
            route="/v1/sessions/{session_id}",
            method="GET",
            status="200",
        )
        assert delivered.wait(timeout=1.0)
        row = batches[0][0]
        assert row["event_name"] == "get_session"
        assert row["session_id"] == "conv_1"
        assert row["attributes"]["phase"] == "ok"
        assert row["attributes"]["route"] == "/v1/sessions/{session_id}"
        assert row["attributes"]["status"] == "200"
    finally:
        dl.audit_event_logger().removeHandler(sink)
        dl.sse_event_logger().removeHandler(sink)
        sink.close()


def test_handler_audit_attrs_ride_the_envelope_end_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End-to-end: attrs a handler sets via add_audit_attrs must survive
    # Starlette's BaseHTTPMiddleware (which runs the handler in a child
    # context) and land on the envelope end-event the middleware emits in its
    # finally. This is the propagation the whole enrich mechanism relies on.
    from fastapi.testclient import TestClient

    from omnigent.server.app import _emit_audit_event, _resolve_audit_route

    monkeypatch.setattr(dl.DebugLogHandler, "_FLUSH_WAIT", 0.01)
    monkeypatch.setattr(dl, "_active_sink", None)
    rows: list[dl.DebugLogRow] = []
    got_end = threading.Event()

    def send(batch: list[dl.DebugLogRow]) -> None:
        rows.extend(batch)
        for row in batch:
            if row["attributes"].get("phase") in ("ok", "error"):
                got_end.set()

    dl.attach_debug_log_sink([], source="server", level=logging.INFO, send=send)
    sink = dl._active_sink
    assert sink is not None

    app = FastAPI()

    @app.middleware("http")
    async def _audit(request: Request, call_next):  # type: ignore[no-untyped-def]
        operation, route, session_id = _resolve_audit_route(request)
        dl.set_current_session_id(session_id)
        dl.reset_request_audit_attrs()
        _emit_audit_event(operation, "start", session_id=session_id, route=route)
        response = await call_next(request)
        end = dl.current_request_audit_attrs()
        end.update(route=route, status=str(response.status_code))
        _emit_audit_event(operation, "ok", session_id=session_id, **end)
        return response

    @app.post("/v1/sessions/{session_id}/events")
    def post_event(session_id: str) -> dict[str, bool]:
        dl.add_audit_attrs(event_type="message", item_id="it_1")
        return {"queued": True}

    try:
        with TestClient(app) as client:
            client.post("/v1/sessions/conv_9/events")
        assert got_end.wait(timeout=1.0)
        end_rows = [r for r in rows if r["attributes"].get("phase") == "ok"]
        assert len(end_rows) == 1
        end_row = end_rows[0]
        assert end_row["event_name"] == "post_event"
        assert end_row["session_id"] == "conv_9"
        assert end_row["attributes"]["event_type"] == "message"
        assert end_row["attributes"]["item_id"] == "it_1"
        assert end_row["attributes"]["status"] == "200"
    finally:
        dl.set_current_session_id(None)
        dl.audit_event_logger().removeHandler(sink)
        dl.sse_event_logger().removeHandler(sink)
        sink.close()


def test_suppressed_request_emits_no_row(monkeypatch: pytest.MonkeyPatch) -> None:
    # A handler that calls mark_request_audit_suppressed() (transient POST /events
    # echoes) produces ZERO audit rows; a normal request on the same route
    # produces exactly one end row (no start row for post_event).
    from fastapi.testclient import TestClient

    from omnigent.server.app import _emit_audit_event, _resolve_audit_route

    monkeypatch.setattr(dl.DebugLogHandler, "_FLUSH_WAIT", 0.01)
    monkeypatch.setattr(dl, "_active_sink", None)
    rows: list[dl.DebugLogRow] = []
    delivered = threading.Event()

    def send(batch: list[dl.DebugLogRow]) -> None:
        rows.extend(batch)
        delivered.set()

    dl.attach_debug_log_sink([], source="server", level=logging.INFO, send=send)
    sink = dl._active_sink
    assert sink is not None

    app = FastAPI()
    _reserved = {
        "_suppress",
        "session_id",
        "phase",
        "route",
        "method",
        "request_id",
        "status",
        "duration_ms",
    }

    @app.middleware("http")
    async def _audit(request: Request, call_next):  # type: ignore[no-untyped-def]
        operation, route, session_id = _resolve_audit_route(request)
        dl.set_current_session_id(session_id)
        dl.reset_request_audit_attrs()
        if operation != "post_event":
            _emit_audit_event(operation, "start", session_id=session_id, route=route)
        response = await call_next(request)
        bag = dl.current_request_audit_attrs()
        if not (bag.get("_suppress")):
            end = {k: v for k, v in bag.items() if k not in _reserved}
            end.update(route=route, status=str(response.status_code))
            _emit_audit_event(operation, "ok", session_id=session_id, **end)
        return response

    @app.post("/v1/sessions/{session_id}/events")
    def post_event(session_id: str, transient: bool = False) -> dict[str, bool]:
        dl.add_audit_attrs(event_type="external_output_text_delta" if transient else "message")
        if transient:
            dl.mark_request_audit_suppressed()
        return {"queued": True}

    try:
        with TestClient(app) as client:
            client.post("/v1/sessions/conv_1/events?transient=true")
            client.post("/v1/sessions/conv_1/events")  # normal
        assert delivered.wait(timeout=1.0)
        ev = [r for r in rows if r["event_name"] == "post_event"]
        # Transient -> nothing; normal -> a single end row (no start for post_event).
        assert all(r["attributes"].get("event_type") != "external_output_text_delta" for r in ev)
        assert len(ev) == 1
        assert ev[0]["attributes"]["phase"] == "ok"
        assert ev[0]["attributes"]["event_type"] == "message"
    finally:
        dl.set_current_session_id(None)
        dl.audit_event_logger().removeHandler(sink)
        dl.sse_event_logger().removeHandler(sink)
        sink.close()


def test_reserved_attr_keys_never_crash_and_session_id_binds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A handler that (mis)uses a reserved key like session_id/phase must not
    # collide with the envelope's own args (which would 500 the request); the
    # create-session pattern binds the id via set_current_session_id so the ok
    # row carries it in the session_id column even on a route with no path id.
    from fastapi.testclient import TestClient

    from omnigent.server.app import _emit_audit_event, _resolve_audit_route

    monkeypatch.setattr(dl.DebugLogHandler, "_FLUSH_WAIT", 0.01)
    monkeypatch.setattr(dl, "_active_sink", None)
    rows: list[dl.DebugLogRow] = []
    got_end = threading.Event()

    def send(batch: list[dl.DebugLogRow]) -> None:
        rows.extend(batch)
        for row in batch:
            if row["attributes"].get("phase") in ("ok", "error"):
                got_end.set()

    dl.attach_debug_log_sink([], source="server", level=logging.INFO, send=send)
    sink = dl._active_sink
    assert sink is not None

    app = FastAPI()
    _reserved = {"session_id", "phase", "route", "method", "request_id", "status", "duration_ms"}

    @app.middleware("http")
    async def _audit(request: Request, call_next):  # type: ignore[no-untyped-def]
        operation, route, session_id = _resolve_audit_route(request)
        dl.set_current_session_id(session_id)
        dl.reset_request_audit_attrs()
        _emit_audit_event(operation, "start", session_id=session_id, route=route)
        response = await call_next(request)
        bag = dl.current_request_audit_attrs()
        end_session_id = bag.get("session_id") or session_id
        end = {k: v for k, v in bag.items() if k not in _reserved}
        end.update(route=route, status=str(response.status_code))
        _emit_audit_event(operation, "ok", session_id=end_session_id, **end)
        return response

    @app.post("/v1/sessions")
    def create_session() -> dict[str, str]:
        # Mimic the real handler: surface the created id via the bag, and
        # deliberately shove reserved keys through it to prove they can't crash us
        # (a ContextVar set here would not survive the child->parent boundary).
        dl.add_audit_attrs(session_id="sess_created", phase="shadow", agent="polly")
        return {"id": "sess_created"}

    try:
        with TestClient(app) as client:
            resp = client.post("/v1/sessions")
            assert resp.status_code == 200  # no 500 from a reserved-key collision
        assert got_end.wait(timeout=1.0)
        end_row = next(r for r in rows if r["attributes"].get("phase") == "ok")
        assert end_row["event_name"] == "create_session"
        # session_id column comes from set_current_session_id (route has no path id).
        assert end_row["session_id"] == "sess_created"
        # The reserved keys were dropped; the real attribute rode along.
        assert end_row["attributes"]["agent"] == "polly"
        assert end_row["attributes"]["phase"] == "ok"
    finally:
        dl.set_current_session_id(None)
        dl.audit_event_logger().removeHandler(sink)
        dl.sse_event_logger().removeHandler(sink)
        sink.close()
