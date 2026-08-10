"""Log-redaction tests for the manager-webhook dispatcher (OMN-104).

Covers the acceptance-test row "log lines for a delivery attempt assert
absence of payload body, secret, signature, full URL" (design doc §12,
§9). Reuses the fake-manager-via-``httpx.ASGITransport`` technique from
``tests/server/integration/test_manager_webhook_dispatcher.py`` — a real
in-process FastAPI app, no mocking libraries.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from omnigent.server import manager_webhook_dispatcher as dispatcher
from omnigent.server import manager_webhook_signing as signing
from omnigent.stores.session_lifecycle_store.sqlalchemy_store import (
    SqlAlchemySessionLifecycleStore,
)

_SECRET = "test-webhook-secret-marker-XYZ789"
_PAYLOAD_MARKER = "MARKER_PAYLOAD_CONTENT_12345"
_URL_PATH_MARKER = "very-specific-path-marker"
_ENDPOINT = f"https://fake-manager.test/webhook/{_URL_PATH_MARKER}"


def _hex_id(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:32]


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemySessionLifecycleStore:
    return SqlAlchemySessionLifecycleStore(db_uri)


@pytest.fixture(autouse=True)
def _webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(signing.SECRET_ENV_VAR, _SECRET)


def _insert_event(store: SqlAlchemySessionLifecycleStore, *, session_id: str) -> str:
    event, _inserted = store.record_lifecycle_event(
        event_id=_hex_id(f"evt:{session_id}"),
        session_id=session_id,
        event_type="session.completed",
        transition_key="turn:r1:completed",
        payload=json.dumps({"response_id": "r1", "marker": _PAYLOAD_MARKER}),
        now=int(time.time()),
    )
    return event.id


def _build_fake_manager(*, always_fail: bool) -> FastAPI:
    app = FastAPI()

    @app.post(f"/webhook/{_URL_PATH_MARKER}")
    async def webhook() -> JSONResponse:
        if always_fail:
            return JSONResponse(status_code=500, content={"error": "fail"})
        return JSONResponse(status_code=200, content={})

    return app


def _assert_log_records_are_clean(records: list[logging.LogRecord]) -> None:
    assert records, "expected at least one manager_webhook_dispatcher log record"
    for record in records:
        message = record.getMessage()
        # No payload body content.
        assert _PAYLOAD_MARKER not in message
        # No secret.
        assert _SECRET not in message
        # No full URL / path — only the bare host may appear (via the
        # dedicated endpoint_host field).
        assert _URL_PATH_MARKER not in message
        assert _ENDPOINT not in message
        # extra={...} fields land as record attributes — check those too,
        # not just the formatted message.
        for key, value in record.__dict__.items():
            if not isinstance(value, str):
                continue
            assert _PAYLOAD_MARKER not in value, f"payload leaked via {key!r}"
            assert _SECRET not in value, f"secret leaked via {key!r}"
            assert _URL_PATH_MARKER not in value, f"full URL leaked via {key!r}"
        # The one place a host IS expected to appear.
        if "endpoint_host" in record.__dict__:
            assert record.__dict__["endpoint_host"] == "fake-manager.test"
    # Confirm _log_attempt's extra dict only ever carries the documented
    # fields — no stray "payload"/"signature"/"secret" key ever makes it in.
    logged_keys = {
        k
        for r in records
        for k in r.__dict__
        if k
        not in (
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "taskName",
        )
    }
    forbidden = {"payload", "signature", "secret", "endpoint", "url", "raw_json_body"}
    leaked = logged_keys & forbidden
    assert not leaked, f"forbidden log fields present: {leaked}"


async def test_successful_delivery_log_is_redacted(
    store: SqlAlchemySessionLifecycleStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A successful delivery's log line never carries payload/secret/signature/full URL."""
    session_id = _hex_id("session-a")
    event_id = _insert_event(store, session_id=session_id)
    app = _build_fake_manager(always_fail=False)
    transport = httpx.ASGITransport(app=app)

    with caplog.at_level(logging.INFO, logger="omnigent.server.manager_webhook_dispatcher"):
        async with httpx.AsyncClient(transport=transport) as client:
            claimed = store.claim_batch(
                limit=10, now=int(time.time()), lease_owner="r1", lease_seconds=60
            )
            [event] = [c for c in claimed if c.id == event_id]
            await dispatcher._deliver_one(client, store, event, endpoint=_ENDPOINT, key_id="k1")

    records = [r for r in caplog.records if r.name == "omnigent.server.manager_webhook_dispatcher"]
    _assert_log_records_are_clean(records)
    # Sanity: the delivery actually succeeded (proves this isn't a vacuous
    # pass from an early-exit failure path).
    assert store.latest_delivery(session_id) is not None
    assert store.latest_delivery(session_id).status == "delivered"


async def test_failed_delivery_log_is_redacted(
    store: SqlAlchemySessionLifecycleStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed (500) delivery's log line is equally redacted — the error path is not exempt."""
    session_id = _hex_id("session-b")
    event_id = _insert_event(store, session_id=session_id)
    app = _build_fake_manager(always_fail=True)
    transport = httpx.ASGITransport(app=app)

    with caplog.at_level(logging.INFO, logger="omnigent.server.manager_webhook_dispatcher"):
        async with httpx.AsyncClient(transport=transport) as client:
            claimed = store.claim_batch(
                limit=10, now=int(time.time()), lease_owner="r1", lease_seconds=60
            )
            [event] = [c for c in claimed if c.id == event_id]
            await dispatcher._deliver_one(client, store, event, endpoint=_ENDPOINT, key_id="k1")

    records = [r for r in caplog.records if r.name == "omnigent.server.manager_webhook_dispatcher"]
    _assert_log_records_are_clean(records)
    assert store.latest_delivery(session_id).status in ("pending", "dead_letter")


async def test_missing_secret_log_is_redacted(
    store: SqlAlchemySessionLifecycleStore,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The "missing secret" error path (never signs/sends) still logs cleanly."""
    monkeypatch.delenv(signing.SECRET_ENV_VAR, raising=False)
    session_id = _hex_id("session-c")
    event_id = _insert_event(store, session_id=session_id)
    app = _build_fake_manager(always_fail=False)
    transport = httpx.ASGITransport(app=app)

    with caplog.at_level(logging.INFO, logger="omnigent.server.manager_webhook_dispatcher"):
        async with httpx.AsyncClient(transport=transport) as client:
            claimed = store.claim_batch(
                limit=10, now=int(time.time()), lease_owner="r1", lease_seconds=60
            )
            [event] = [c for c in claimed if c.id == event_id]
            await dispatcher._deliver_one(client, store, event, endpoint=_ENDPOINT, key_id="k1")

    records = [r for r in caplog.records if r.name == "omnigent.server.manager_webhook_dispatcher"]
    _assert_log_records_are_clean(records)
