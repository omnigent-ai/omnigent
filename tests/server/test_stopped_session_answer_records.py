"""Recovery records for answers on a stopped session: pruning and a reused elicitation id."""

from __future__ import annotations

import asyncio
import time

from starlette.requests import Request

from omnigent.entities import Conversation
from omnigent.runtime import pending_elicitations
from omnigent.server.routes._sessions import orchestration


def _record(started_at: float, *, delivering: bool) -> orchestration._StoppedSessionAnswer:
    record = orchestration._StoppedSessionAnswer(started_at=started_at, fingerprint="old")
    if delivering:
        record.delivery = asyncio.get_running_loop().create_future()
    return record


async def test_the_prune_keeps_running_and_recent_records() -> None:
    old = time.time() - orchestration._CLAUDE_NATIVE_PERMISSION_HOOK_TIMEOUT_S - 1
    records = {
        ("session", "old_done"): _record(old, delivering=False),
        ("session", "old_running"): _record(old, delivering=True),
        ("session", "recent"): _record(time.time(), delivering=False),
    }
    for key, record in records.items():
        orchestration._stopped_session_answers[key] = record
    try:
        orchestration._prune_stopped_session_answers()

        kept = {
            key for key in records if orchestration._stopped_session_answers.get(key) is not None
        }
        assert kept == {("session", "old_running"), ("session", "recent")}
    finally:
        for key in records:
            orchestration._stopped_session_answers.pop(key, None)


async def test_an_answer_prunes_old_records() -> None:
    old = time.time() - orchestration._CLAUDE_NATIVE_PERMISSION_HOOK_TIMEOUT_S - 1
    key = ("session", "old_done")
    orchestration._stopped_session_answers[key] = _record(old, delivering=False)
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    conv = Conversation(id="other", created_at=0, updated_at=0, root_conversation_id="other")

    async def _post_event(*args: object, **kwargs: object) -> dict[str, bool]:
        raise AssertionError("an out-of-scope answer must not be delivered")

    try:
        delivered = await orchestration._deliver_answer_to_stopped_session(
            request,
            "other",
            conv,
            {"elicitation_id": "elicit_x", "action": "decline"},
            None,
            _post_event,
        )

        assert delivered is None
        assert orchestration._stopped_session_answers.get(key) is None
    finally:
        orchestration._stopped_session_answers.pop(key, None)


async def test_a_reused_id_does_not_drop_a_running_delivery() -> None:
    session_id, elicitation_id = "conv_reused", "elicit_claude_" + "d" * 32
    pending_elicitations.record_publish(
        session_id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": elicitation_id,
            "params": {"mode": "form", "message": "a different question"},
        },
    )
    record = _record(time.time(), delivering=True)
    key = (session_id, elicitation_id)
    orchestration._stopped_session_answers[key] = record
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    conv = Conversation(id=session_id, created_at=0, updated_at=0, root_conversation_id=session_id)

    async def _post_event(*args: object, **kwargs: object) -> dict[str, bool]:
        raise AssertionError("a request that waits must not start its own delivery")

    try:
        answer = asyncio.create_task(
            orchestration._deliver_answer_to_stopped_session(
                request,
                session_id,
                conv,
                {"elicitation_id": elicitation_id, "action": "accept", "content": {}},
                None,
                _post_event,
            )
        )
        await asyncio.sleep(0)
        assert orchestration._stopped_session_answers.get(key) is record
        record.delivery.set_result({"queued": True})
        assert await asyncio.wait_for(answer, 5.0) == {"queued": True}
    finally:
        orchestration._stopped_session_answers.pop(key, None)
        pending_elicitations.resolve(session_id, elicitation_id)
