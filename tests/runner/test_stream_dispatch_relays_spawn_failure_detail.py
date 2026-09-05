"""Direct-stream dispatch must not discard harness spawn-failure detail.

When a harness fails to start, ``_stream_message_to_harness`` returns a
``JSONResponse`` carrying a real diagnosis
(``{"error": "harness_spawn_failed", "detail": ...}``). The background
dispatch path (``_run_turn_bg``) decodes that body into the relay's
terminal ``session.status: failed`` event and logs it at ERROR. The
direct-stream dispatch path (``POST /v1/sessions/{id}/events?stream=true``)
must do the same instead of publishing the fixed string
``"harness returned error response"`` and logging nothing: otherwise relay
subscribers cannot distinguish a spawn failure from any other
non-streaming outcome, and the HTTP response and the relay disagree about
the same failure.

Journey driven (real runner app, real started ``HarnessProcessManager``):
POST a user message to ``/v1/sessions/{conv}/events?stream=true`` naming a
harness that cannot spawn, then read the relay events the runner published
for that session — the same per-session queue the SSE
``GET /v1/sessions/{id}/stream`` endpoint drains for subscribers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runner import create_runner_app
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
from tests.runner.conftest import _runner_client
from tests.runner.helpers import NullServerClient

# A harness name no environment registers, so the REAL
# HarnessProcessManager.get_client raises the production spawn-failure
# RuntimeError and the dispatch path builds its 503 harness_spawn_failed
# JSONResponse — no mocked failure injection.
_UNSPAWNABLE_HARNESS = "omni-repro-unspawnable-harness"

# The fixed fallback string the direct-stream path must stop publishing
# unconditionally when a decodable error body is available.
_GENERIC_RELAY_MESSAGE = "harness returned error response"


@pytest.fixture
async def spawn_failing_manager() -> AsyncIterator[HarnessProcessManager]:
    """A real, started HarnessProcessManager with no test harness registered.

    Requesting :data:`_UNSPAWNABLE_HARNESS` (or the runner's fallback
    default, equally unregistered) makes ``get_client`` raise the real
    unknown-harness ``RuntimeError``, which the dispatch paths convert into
    the ``503 harness_spawn_failed`` JSONResponse under test.

    Uses a short ``/tmp`` parent rather than pytest's ``tmp_path`` because
    UDS paths on Linux are capped at 108 chars and the manager's
    per-conversation socket layout blows past that under pytest's tree.

    :returns: Async iterator yielding the started manager; shuts it down
        and removes its instance dir on teardown.
    """
    short_parent = Path(f"/tmp/oa-rtest-{uuid.uuid4().hex[:8]}")
    short_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    mgr = HarnessProcessManager(tmp_parent=short_parent)
    await mgr.start()
    try:
        yield mgr
    finally:
        await mgr.shutdown()
        shutil.rmtree(short_parent, ignore_errors=True)


def _build_app(manager: HarnessProcessManager) -> FastAPI:
    """Build a runner app whose harness spawn genuinely fails.

    :param manager: A real, started process manager with the requested
        harness unregistered.
    :returns: The runner FastAPI app under test.
    """
    return create_runner_app(
        process_manager=manager,
        spec_resolver=None,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )


async def _post_spawn_failing_turn(
    http: httpx.AsyncClient,
    conv: str,
    *,
    stream: bool,
) -> httpx.Response:
    """Send the user message that triggers the harness spawn failure.

    :param http: ASGI test client against the runner app.
    :param conv: Session/conversation identifier the turn belongs to.
    :param stream: ``True`` drives the direct-stream dispatch path
        (``?stream=true``); ``False`` drives the background 202 path.
    :returns: The runner's HTTP response for the turn request.
    """
    query = "?stream=true" if stream else ""
    return await http.post(
        f"/v1/sessions/{conv}/events{query}",
        json={
            "type": "message",
            "role": "user",
            "harness": _UNSPAWNABLE_HARNESS,
            "model": "fake/model",
            "content": [{"type": "input_text", "text": "hello"}],
        },
    )


async def _failed_relay_event(
    queues: dict[str, Any],
    conv: str,
    *,
    timeout: float = 2.0,
) -> dict[str, Any] | None:
    """Return the ``session.status: failed`` event the runner relayed.

    Reads the runner's per-session event queue
    (``app.state.session_event_queues``) — the same queue the SSE
    ``/stream`` endpoint drains for subscribers — rather than a concurrent
    SSE ``GET``, because ``httpx.ASGITransport`` does not interleave a
    streaming response with a concurrent ``POST`` on the same client.

    :param queues: The app's per-session event-queue dict, i.e.
        ``app.state.session_event_queues``.
    :param conv: Session/conversation identifier, e.g. ``"conv_abc123"``.
    :param timeout: Hard cap in seconds before giving up.
    :returns: The ``session.status: failed`` event dict, or ``None``.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        queue = queues.get(conv)
        drained = False
        while queue is not None and not queue.empty():
            event = queue.get_nowait()
            drained = True
            if (
                isinstance(event, dict)
                and event.get("type") == "session.status"
                and event.get("status") == "failed"
            ):
                return event
        if not drained:
            await asyncio.sleep(0.02)
    return None


@pytest.mark.asyncio
async def test_stream_spawn_failure_relays_error_detail(
    spawn_failing_manager: HarnessProcessManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The stream=true dispatch path must relay and log the spawn diagnosis.

    Drives the real journey: a subscriber-visible turn is started via
    ``POST /v1/sessions/{conv}/events?stream=true`` with a harness that
    cannot spawn. The HTTP caller gets the diagnosed
    ``503 harness_spawn_failed``; the relay subscriber and the runner's
    ERROR log must learn the same diagnosis instead of the fixed
    ``"harness returned error response"`` string.
    """
    app = _build_app(spawn_failing_manager)
    conv = f"conv_spawn_detail_{uuid.uuid4().hex[:8]}"

    async with _runner_client(app) as http:
        with caplog.at_level(logging.ERROR, logger="omnigent.runner.app"):
            response = await _post_spawn_failing_turn(http, conv, stream=True)

        # Journey sanity: the direct HTTP caller receives the diagnosed
        # spawn failure (this side has always been correct).
        assert response.status_code == 503
        body = response.json()
        assert body["error"] == "harness_spawn_failed"
        assert body["detail"]

        failed = await _failed_relay_event(app.state.session_event_queues, conv)

    assert failed is not None, "no session.status: failed event reached the relay"
    relayed_error = json.dumps(failed.get("error") or {})

    # The relay must not be fobbed off with the generic fallback while the
    # HTTP response carries a real diagnosis for the same failure.
    assert relayed_error != json.dumps({"message": _GENERIC_RELAY_MESSAGE}), (
        "relay subscribers still get the generic fallback instead of the "
        "harness_spawn_failed diagnosis"
    )
    # HTTP response and relay must agree on the failure: the relay error
    # carries the harness_spawn_failed code and the client-safe detail.
    assert "harness_spawn_failed" in relayed_error
    assert "see the runner log for details" in relayed_error

    # The dispatch outcome must be diagnosable from the runner log too: at
    # least one ERROR record names the decoded harness error, not only the
    # generic fallback (the background path has always logged its decode).
    error_lines = [
        record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR
    ]
    assert any("harness_spawn_failed" in line for line in error_lines), (
        f"no ERROR log line carries the decoded harness error body: {error_lines!r}"
    )
