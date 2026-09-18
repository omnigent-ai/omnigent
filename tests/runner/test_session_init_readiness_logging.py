"""Session init must emit a session-attributed readiness lifecycle marker.

Launch-to-ready telemetry keys readiness off non-ERROR, session-attributed
log records. Before this marker existed, the only readiness proxies were
turn-driven (``post_session_events``) or native-terminal-only
(``_auto_create_claude_terminal`` / ``_ensure_native_terminal``), so a
connected SDK-harness session that had not yet received a turn produced a
connect record and nothing else — a successful launch indistinguishable from
a silent failure.

These tests drive the runner's session-init endpoint with the standard fakes
and assert the funnel contract directly: one non-ERROR readiness record,
attributed to the initialized session, on success — and none on a rejected
init.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import FastAPI

from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)

AGENT_ID = "ag_readiness_test"
SESSION_ID = "conv_readiness_test"

_READINESS_MARKER = "ready to serve"


class _EmptyHistoryServerClient:
    """Server client stub: a fresh session with no persisted items."""

    class _Resp:
        status_code = 200

        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def json(self) -> dict[str, Any]:
            return self._payload

        def raise_for_status(self) -> None:
            pass

    async def get(self, url: str, **kwargs: Any) -> _Resp:
        del kwargs
        if url.rstrip("/").endswith("/items"):
            return self._Resp({"object": "list", "data": [], "has_more": False})
        return self._Resp({})

    async def post(self, url: str, **kwargs: Any) -> _Resp:
        del url, kwargs
        return self._Resp({})

    async def patch(self, url: str, **kwargs: Any) -> _Resp:
        del url, kwargs
        return self._Resp({})


def _build_sdk_app() -> FastAPI:
    spec = AgentSpec(spec_version=1, name="t")

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    return create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_EmptyHistoryServerClient(),  # type: ignore[arg-type]
    )


def _readiness_records(
    caplog: pytest.LogCaptureFixture, session_id: str
) -> list[logging.LogRecord]:
    """Records the launch funnel would count as readiness for *session_id*.

    Mirrors the telemetry sink's contract: a non-ERROR record whose
    ``session_id`` attribute (threaded via ``extra``) names the session and
    whose message marks the session ready.
    """
    return [
        rec
        for rec in caplog.records
        if rec.levelno < logging.ERROR
        and getattr(rec, "session_id", None) == session_id
        and _READINESS_MARKER in rec.getMessage()
    ]


@pytest.mark.asyncio
async def test_successful_session_init_emits_session_attributed_readiness_marker(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A successful init emits exactly one session-attributed readiness record.

    The record must be non-ERROR (the funnel discards ERROR lines), carry the
    session id both as the attribution attribute and in the message text, and
    fire without any user turn — readiness must not depend on the first turn.
    """
    app = _build_sdk_app()
    caplog.set_level(logging.INFO, logger="omnigent.runner.app")

    async with _runner_client(app) as client:
        init_resp = await client.post(
            "/v1/sessions",
            json={"session_id": SESSION_ID, "agent_id": AGENT_ID},
        )
        assert init_resp.status_code == 201, init_resp.text
        assert init_resp.json().get("status") == "idle"

    records = _readiness_records(caplog, SESSION_ID)
    assert len(records) == 1, (
        "expected exactly one session-attributed readiness record after a "
        f"successful init (bounded lifecycle marker), got {len(records)}: "
        f"{[rec.getMessage() for rec in records]}"
    )
    message = records[0].getMessage()
    assert SESSION_ID in message, (
        f"readiness message must name the session so unkeyed consumers can "
        f"correlate it, got: {message!r}"
    )


@pytest.mark.asyncio
async def test_rejected_session_init_emits_no_readiness_marker(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An init the runner rejects must not claim readiness."""
    app = _build_sdk_app()
    caplog.set_level(logging.INFO, logger="omnigent.runner.app")

    async with _runner_client(app) as client:
        init_resp = await client.post(
            "/v1/sessions",
            json={"session_id": SESSION_ID},
        )
        assert init_resp.status_code == 400, init_resp.text

    assert not _readiness_records(caplog, SESSION_ID), (
        "a rejected session init must not emit a readiness marker"
    )
