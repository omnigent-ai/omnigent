"""Concurrent conflicting-decision truthfulness test (OMN-104).

Cross-vendor review, final round: ``_resolve_elicitation_durably``
(``omnigent/server/routes/_sessions/orchestration.py``) durably serializes
conflicting concurrent decision callbacks via ``session_outbox.record_decision``
(itself row-locked at the store layer — see
``tests/stores/test_session_lifecycle_store.py``'s own concurrency tests for
that layer), but until this fix it forwarded each caller's OWN raw request
payload to the live runner/harness instead of the PERSISTED WINNER's payload.
A losing racing callback could therefore make the LIVE runner consume its own
(losing) verdict while the durable ledger correctly kept the winner's — live
action, policy writes, and durable record diverging for the same elicitation.

This needs REAL concurrency, not a sequential simulation: two genuine OS
threads, each with its own asyncio event loop (a single event loop's
``asyncio.gather`` would NOT reproduce the race — ``session_outbox.
record_decision`` is a synchronous, non-awaited call inside the async
function under test, so within one loop the first coroutine runs it to
completion, with no yield point, before the second coroutine even starts).
Two threads racing via a ``threading.Barrier`` against a real backend (this
file uses whatever ``db_uri`` resolves to — SQLite locally, real Postgres in
CI's ``stores-postgres`` lane) is what makes the row-level lock at the store
layer, and therefore the decision race itself, genuine.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.server import session_outbox
from omnigent.server.routes._sessions.orchestration import _resolve_elicitation_durably
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent
from tests.server.integration.test_sessions_endpoints import _create_session

pytestmark = pytest.mark.asyncio


def _run_concurrently(fn_a, fn_b) -> tuple[Any, Any]:
    """Run two zero-arg callables on their own OS threads, synchronized to
    start together via a barrier, and return both return values (or
    re-raise the first exception either thread raised)."""
    barrier = threading.Barrier(2)
    results: list[Any] = [None, None]
    errors: list[BaseException | None] = [None, None]

    def _wrapped(index: int, fn) -> None:
        barrier.wait()
        try:
            results[index] = fn()
        except BaseException as exc:  # captured for the main thread to re-raise
            errors[index] = exc

    threads = [
        threading.Thread(target=_wrapped, args=(i, fn)) for i, fn in enumerate((fn_a, fn_b))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    for err in errors:
        if err is not None:
            raise err
    return results[0], results[1]


async def test_concurrent_conflicting_decisions_forward_only_the_winning_verdict(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Two conflicting decision callbacks (accept vs decline) racing for real
    against the SAME elicitation must converge on exactly one durable
    winner, AND the live runner must observe that SAME winning verdict from
    BOTH racing calls — never the loser's own (different) payload.
    """
    agent = await create_test_agent(client, "test-omn104-concurrent-conflict")
    session = await _create_session(client, agent["id"])
    session_id = session["id"]
    elicitation_id = "elicit_concurrent_conflict_test"

    session_outbox.record_elicitation_raised(
        workspace_id=0,
        session_id=session_id,
        elicitation_id=elicitation_id,
        request={"message": "Approve?", "mode": "form"},
    )

    forwarded_actions: list[str | None] = []
    forwarded_lock = threading.Lock()

    async def _fake_get_runner_client(
        _session_id: str, _runner_router: object
    ) -> httpx.AsyncClient:
        fake_runner = FastAPI()

        @fake_runner.post("/v1/sessions/{conversation_id}/events")
        async def _events(conversation_id: str, request: Request) -> JSONResponse:
            del conversation_id
            body = await request.json()
            action = (body.get("data") or {}).get("action")
            with forwarded_lock:
                forwarded_actions.append(action)
            return JSONResponse(
                status_code=200,
                content={"ok": True},
                headers={"X-Omnigent-Pending-Approval-Resolved": "true"},
            )

        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fake_runner), base_url="http://fake-runner"
        )

    # _resolve_elicitation_durably's runner forward resolves the runner
    # client via this module-level name lookup — patch it directly, the
    # same legitimate test-boundary seam
    # test_manager_webhook_decision_endpoint.py already uses for this exact
    # purpose. Set once, before either thread starts.
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.helpers._get_runner_client",
        _fake_get_runner_client,
    )

    conversation_store = SqlAlchemyConversationStore(db_uri)

    def _run(action: str, decided_by: str) -> object:
        return asyncio.run(
            _resolve_elicitation_durably(
                session_id,
                {"elicitation_id": elicitation_id, "action": action},
                None,
                conversation_store,
                decided_by=decided_by,
            )
        )

    result_accept, result_decline = _run_concurrently(
        lambda: _run("accept", "mgr-a@example.com"),
        lambda: _run("decline", "mgr-b@example.com"),
    )
    assert result_accept is not None and result_decline is not None

    # (1) Exactly one persisted winner in the durable ledger.
    final = session_outbox.get_elicitation(elicitation_id)
    assert final is not None and final.decision_payload is not None
    winning_decision = json.loads(final.decision_payload)
    winning_action = winning_decision["action"]
    assert winning_action in ("accept", "decline")

    # Both callers' returned decision_record must agree with the ledger —
    # neither believes ITS OWN verdict won when the other's actually did.
    assert result_accept.decision_record is not None
    assert result_decline.decision_record is not None
    assert json.loads(result_accept.decision_record.decision_payload)["action"] == winning_action
    assert json.loads(result_decline.decision_record.decision_payload)["action"] == winning_action

    # (2) The live runner received exactly two forwards (one per racing
    # call — the bug being fixed is not "only the winner forwards", it's
    # "every forward carries the winner's payload") and BOTH carry the
    # SAME winning action — never a mix of accept and decline. Pre-fix,
    # this would show one "accept" and one "decline" — the losing
    # callback's own raw request data, not the persisted winner.
    assert len(forwarded_actions) == 2, forwarded_actions
    assert forwarded_actions == [winning_action, winning_action], forwarded_actions
