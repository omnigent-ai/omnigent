"""Failed turns must log a usable reason.

The server-side broken-turn sink ``_publish_status`` funnels every
server-originated ``failed`` turn through one ERROR log line::

    session turn failed for <id>: <detail>

In production ``<detail>`` degraded into one of three undebuggable shapes --
each counted against the mid-session error KPI and none triageable by a
human:

1. the literal ``no detail`` -- the ``failed`` edge carried no error at all;
2. ``turn setup failed:`` with an empty reason -- the cause was dropped by
   the runner's ``f"turn setup failed: {exc}"`` when ``str(exc)`` was empty;
3. the assistant's *own successful* final message, verbatim, as the "error"
   -- a turn that produced output was then labelled a failure and the reason
   was overwritten by (or fabricated from) that output.

Each test below drives the real wire path that reaches ``_publish_status``
-- the native-forwarder ``external_session_status`` POST (shapes 1 and 3)
and the runner stream relay (shape 2) -- and captures the emitted ERROR
record from the ``omnigent.server.routes.sessions`` logger (the ticket's
``logger_name`` / ``func_name``).

The assertions encode the *desired* behaviour (a non-empty, non-prose,
diagnosable reason), so on the current build they FAIL -- reproducing the
bug -- and a fix that always populates a usable detail turns them green.

Runs against the in-process server harness in ``tests/server/conftest.py``
(the same ``client`` / ``db_uri`` fixtures the existing ``_publish_status``,
``external_session_status`` and relay tests use), because the observable is
a server-side log record captured with ``caplog`` -- there is no user-facing
screen for it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest

from omnigent.server.routes import sessions as sessions_module
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.server.helpers import create_test_agent
from tests.server.routes.test_sessions_runner_relay import (
    _TASK_TIMEOUT_S,
    _ScriptedRunnerClient,
)

pytestmark = pytest.mark.asyncio

_SESSIONS_LOGGER = "omnigent.server.routes.sessions"
_FAILED_PREFIX = "session turn failed for "


def _failed_turn_details(caplog: pytest.LogCaptureFixture, session_id: str) -> list[str]:
    """Return the ``<detail>`` from every ``session turn failed`` ERROR row.

    Isolates the ``_publish_status`` broken-turn log lines for one session
    from any other ERROR chatter the harness emits.

    :param caplog: Pytest log-capture fixture holding the emitted records.
    :param session_id: Session whose failure rows to extract.
    :returns: The detail substrings (everything after ``"<id>: "``), in
        emission order.
    """
    marker = f"{_FAILED_PREFIX}{session_id}: "
    details: list[str] = []
    for record in caplog.records:
        if record.name != _SESSIONS_LOGGER or record.levelno != logging.ERROR:
            continue
        message = record.getMessage()
        if message.startswith(marker):
            details.append(message[len(marker) :])
    return details


async def _create_top_level_session(client: httpx.AsyncClient) -> str:
    """Create a plain top-level session and return its id.

    :param client: In-process test HTTP client.
    :returns: The new session/conversation id.
    """
    agent = await create_test_agent(client, name="failed-turn-detail")
    resp = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _post_status(
    client: httpx.AsyncClient,
    session_id: str,
    status: str,
    *,
    response_id: str,
) -> None:
    """POST an ``external_session_status`` edge (the native-forwarder wire).

    :param client: In-process test HTTP client.
    :param session_id: Target session id.
    :param status: Status value, e.g. ``"running"`` / ``"failed"``.
    :param response_id: Turn/response id the forwarder attaches.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_session_status",
            "data": {"status": status, "response_id": response_id},
        },
    )
    assert resp.status_code == 202, resp.text


async def test_failed_status_with_no_reason_logs_no_detail(
    client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Shape 1: a ``failed`` edge carrying no error logs a usable reason.

    A native forwarder reports the turn ``failed`` without attaching a
    reason and with no persisted assistant output to fall back on. The
    server publishes ``failed`` with ``error=None`` and logs the literal
    ``no detail`` -- which cannot be triaged.

    :param client: In-process test HTTP client.
    :param caplog: Pytest log-capture fixture.
    """
    session_id = await _create_top_level_session(client)

    with caplog.at_level(logging.ERROR, logger=_SESSIONS_LOGGER):
        await _post_status(client, session_id, "running", response_id="turn_no_detail")
        await _post_status(client, session_id, "failed", response_id="turn_no_detail")

    details = _failed_turn_details(caplog, session_id)
    assert details, "expected a 'session turn failed' ERROR row for the failed turn"
    detail = details[-1]
    assert detail not in ("", "no detail"), (
        "the failed turn was logged with an undebuggable reason "
        f"({detail!r}); a failed turn must carry a usable, non-empty detail"
    )


async def test_failed_status_reuses_the_assistants_own_output_as_error(
    client: httpx.AsyncClient,
    db_uri: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Shape 3: a turn that produced output is failed with its own prose.

    The turn streamed a normal, successful assistant message and was then
    labelled ``failed`` with no reason of its own. The server backfills the
    "error" from the latest persisted assistant text, so the logged failure
    detail is the assistant's own successful sentence -- a false/undebuggable
    failure.

    :param client: In-process test HTTP client.
    :param db_uri: SQLite URI shared with the app's store, for seeding.
    :param caplog: Pytest log-capture fixture.
    """
    session_id = await _create_top_level_session(client)
    success_prose = "All conflicts resolved. Continue the sync:"

    with caplog.at_level(logging.ERROR, logger=_SESSIONS_LOGGER):
        await _post_status(client, session_id, "running", response_id="turn_output_as_error")
        # The turn produced a normal, successful assistant reply.
        seed = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "external_assistant_message",
                "data": {
                    "agent": "assistant",
                    "text": success_prose,
                    "response_id": "turn_output_as_error",
                },
            },
        )
        assert seed.status_code == 202, seed.text
        # ...and is then labelled failed without any error reason attached.
        await _post_status(client, session_id, "failed", response_id="turn_output_as_error")

    # Sanity: the successful message really is persisted, so the enrichment
    # path had it to (mis)use.
    store = SqlAlchemyConversationStore(db_uri)
    messages = [item for item in store.list_items(session_id).data if item.type == "message"]
    assert any(success_prose in str(getattr(m.data, "content", "")) for m in messages), (
        "the successful assistant message should be persisted for this session"
    )

    details = _failed_turn_details(caplog, session_id)
    assert details, "expected a 'session turn failed' ERROR row for the failed turn"
    detail = details[-1]
    assert detail != success_prose, (
        "the failed turn's logged detail is the assistant's own successful "
        f"message ({detail!r}); a successful turn's output must not be "
        "published/logged as the failure reason"
    )


async def test_relay_failed_status_drops_the_setup_failure_reason(
    db_uri: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Shape 2: a runner ``turn setup failed:`` with an empty reason.

    The runner catches a setup exception whose ``str(exc)`` is empty and
    forwards ``{"code": "runner_error", "message": "turn setup failed: "}``.
    The relay republishes it verbatim, so the server logs
    ``session turn failed for <id>: turn setup failed:`` -- the actual cause
    was dropped and nothing after the prefix is diagnosable.

    Drives the real relay stream (``_ensure_runner_relay_ready`` +
    ``_ScriptedRunnerClient``) the way ``test_sessions_runner_relay.py`` does.

    :param db_uri: SQLite URI for the conversation store.
    :param caplog: Pytest log-capture fixture.
    """
    from omnigent.runtime import session_stream

    sessions_module._runner_relay_tasks.clear()
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation()
    session_id = conv.id
    # A running turn is what the relay-close/failure path is meaningful for.
    sessions_module._session_status_cache[session_id] = "running"

    release = asyncio.Event()
    # The runner's empty-reason setup failure, verbatim off the wire.
    events: list[dict[str, Any]] = [
        {
            "type": "session.status",
            "status": "failed",
            "error": {"code": "runner_error", "message": "turn setup failed: "},
        },
    ]
    fake_runner = _ScriptedRunnerClient(release, events)

    try:
        with caplog.at_level(logging.ERROR, logger=_SESSIONS_LOGGER):
            handle = await sessions_module._ensure_runner_relay_ready(
                session_id,
                "runner_relay_setup_failed",
                fake_runner,  # type: ignore[arg-type]
                conversation_store=store,
            )
            assert handle is not None
            release.set()
            await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)

        details = _failed_turn_details(caplog, session_id)
        assert details, "expected a 'session turn failed' ERROR row from the relay"
        detail = details[-1]
        # The message is "turn setup failed: <reason>"; the reason was dropped.
        reason = detail.split("turn setup failed:", 1)[-1].strip() if ":" in detail else detail
        assert reason, (
            "the failed turn's logged detail dropped the actual cause "
            f"({detail!r}); 'turn setup failed:' must carry a non-empty reason"
        )
    finally:
        release.set()
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None:
            await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)
