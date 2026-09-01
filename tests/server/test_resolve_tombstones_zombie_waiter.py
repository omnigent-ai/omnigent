"""A resolve that lands on a live parked Future must also tombstone the verdict.

A proxy can sever a harness long-poll client-side while holding the backend
connection open: the harness abandons the chunk, but the server never observes
a disconnect, so the chunk's waiter stays registered as a zombie. A resolve
that finds that zombie sets its Future — written to a connection nobody reads —
and, before this fix, wrote NO pre-resolved tombstone (only the nothing-parked
branch did). The harness's next re-park of the same stable id then found
nothing and re-asked the gate; the operator's answer was lost.

These tests pin the resolve-side half of the fix: resolving a live registered
Future ALSO leaves a verdict-carrying tombstone for the same id, so a re-park
can adopt the verdict. The tombstone is session-scoped, TTL-pruned, and
fingerprinted with the answered question's request params, so a LATER,
different question that reuses the same harness id (harness request ids can
recur) can never inherit the stale approval.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from omnigent.server.routes import sessions as S
from omnigent.server.schemas import ElicitationRequestParams, ElicitationResult


def _params(message: str) -> ElicitationRequestParams:
    return ElicitationRequestParams(
        mode="form",
        message=message,
        requestedSchema={"type": "object", "properties": {"ok": {"type": "string"}}},
    )


@pytest.mark.asyncio
async def test_resolve_of_live_future_also_tombstones_the_verdict():
    sid = "conv_zombie_waiter"
    eid = "elicit_codex_33333333333333333333333333333333"
    fingerprint = S._harness_elicitation_request_fingerprint(_params("Overwrite?"))
    future: asyncio.Future[ElicitationResult] = asyncio.get_running_loop().create_future()
    S._harness_elicitation_registry[eid] = future
    S._harness_elicitation_owners[eid] = sid
    S._harness_parked_elicitations[eid] = S._ParkedHarnessElicitation(
        session_id=sid,
        tool_name=None,
        tool_input=None,
        resolved_elsewhere=asyncio.Event(),
        request_fingerprint=fingerprint,
    )
    S._harness_pre_resolved_elicitations.pop(eid, None)
    try:
        await S._resolve_elicitation(
            sid, {"elicitation_id": eid, "action": "accept", "content": {"ok": "go"}}, None
        )
        # The Future still gets the verdict (the delivered-poll path).
        assert future.done() and future.result().action == "accept"
        # And the tombstone carries the same verdict, so a re-park of the
        # stable id adopts it when the waiter turns out to be a zombie.
        tomb = S._harness_pre_resolved_elicitations.get(eid)
        assert tomb is not None, (
            "resolving a live parked Future must also tombstone the verdict; "
            "a zombie waiter otherwise swallows it and the gate re-asks"
        )
        assert tomb.session_id == sid
        assert tomb.result is not None and tomb.result.action == "accept"
        assert tomb.result.content == {"ok": "go"}
        # The answered question's fingerprint rides along for reuse safety.
        assert tomb.request_fingerprint == fingerprint
    finally:
        S._harness_elicitation_registry.pop(eid, None)
        S._harness_elicitation_owners.pop(eid, None)
        S._harness_parked_elicitations.pop(eid, None)
        S._harness_pre_resolved_elicitations.pop(eid, None)


@pytest.mark.asyncio
async def test_reused_id_with_a_different_question_does_not_inherit_the_verdict():
    # Harness ids can recur: a later, DIFFERENT request may derive the same
    # elicitation id (e.g. a reused JSON-RPC request id). Its re-park must
    # NOT adopt the earlier answer — same id, different fingerprint.
    sid = "conv_id_reuse"
    eid = "elicit_codex_66666666666666666666666666666666"
    answered = S._harness_elicitation_request_fingerprint(_params("Run `date`?"))
    later = S._harness_elicitation_request_fingerprint(_params("Run `rm -rf /`?"))
    assert answered != later
    S._harness_pre_resolved_elicitations[eid] = S._PreResolvedHarnessElicitation(
        session_id=sid,
        created_at=time.time(),
        result=ElicitationResult(action="accept"),
        request_fingerprint=answered,
    )
    try:
        stale = S._consume_pre_resolved_harness_elicitation(sid, eid, later)
        assert stale is None, (
            "a tombstone fingerprinted for one question must not be replayed "
            "to a different question that reused the same elicitation id"
        )
        # The mismatched tombstone is dropped, not left to hit a retry.
        assert S._harness_pre_resolved_elicitations.get(eid) is None
    finally:
        S._harness_pre_resolved_elicitations.pop(eid, None)


@pytest.mark.asyncio
async def test_gap_resolve_fingerprints_the_tombstone_from_the_pending_prompt():
    # The nothing-parked (detected-sever gap) branch: the pending index
    # still holds the answered prompt, so the tombstone must carry its
    # fingerprint too — a later different question reusing the id within
    # the TTL must not inherit the verdict from this path either.
    from omnigent.runtime import pending_elicitations

    sid = "conv_gap_fingerprint"
    eid = "elicit_codex_88888888888888888888888888888888"
    params = _params("Overwrite the file?")
    pending_elicitations.reset_for_tests()
    pending_elicitations.record_publish(
        sid,
        {
            "type": "response.elicitation_request",
            "elicitation_id": eid,
            "params": params.model_dump(),
        },
    )
    S._harness_pre_resolved_elicitations.pop(eid, None)
    try:
        await S._resolve_elicitation(sid, {"elicitation_id": eid, "action": "accept"}, None)
        tomb = S._harness_pre_resolved_elicitations.get(eid)
        assert tomb is not None and tomb.result is not None
        assert tomb.request_fingerprint == S._harness_elicitation_request_fingerprint(params)
    finally:
        S._harness_pre_resolved_elicitations.pop(eid, None)
        pending_elicitations.reset_for_tests()


@pytest.mark.asyncio
async def test_repark_of_the_same_question_adopts_the_verdict():
    # The intended consumer: a re-park of the SAME envelope (same params,
    # same fingerprint) adopts the gap-landing verdict.
    sid = "conv_same_question"
    eid = "elicit_codex_77777777777777777777777777777777"
    fingerprint = S._harness_elicitation_request_fingerprint(_params("Overwrite?"))
    S._harness_pre_resolved_elicitations[eid] = S._PreResolvedHarnessElicitation(
        session_id=sid,
        created_at=time.time(),
        result=ElicitationResult(action="accept"),
        request_fingerprint=fingerprint,
    )
    try:
        tomb = S._consume_pre_resolved_harness_elicitation(sid, eid, fingerprint)
        assert tomb is not None and tomb.result is not None
        assert tomb.result.action == "accept"
    finally:
        S._harness_pre_resolved_elicitations.pop(eid, None)


@pytest.mark.asyncio
async def test_resolve_from_wrong_session_tombstones_nothing():
    # The ownership guard must cover the tombstone too: a foreign session's
    # resolve may neither settle the Future nor plant a verdict for the id.
    sid = "conv_owner_session"
    eid = "elicit_codex_44444444444444444444444444444444"
    future: asyncio.Future[ElicitationResult] = asyncio.get_running_loop().create_future()
    S._harness_elicitation_registry[eid] = future
    S._harness_elicitation_owners[eid] = sid
    S._harness_pre_resolved_elicitations.pop(eid, None)
    try:
        await S._resolve_elicitation(
            "conv_intruder", {"elicitation_id": eid, "action": "accept"}, None
        )
        assert not future.done()
        assert S._harness_pre_resolved_elicitations.get(eid) is None
    finally:
        S._harness_elicitation_registry.pop(eid, None)
        S._harness_elicitation_owners.pop(eid, None)
        S._harness_pre_resolved_elicitations.pop(eid, None)


@pytest.mark.asyncio
async def test_resolve_with_invalid_payload_tombstones_nothing():
    # A malformed verdict must not settle the Future or leave a tombstone a
    # re-park would then try to honor.
    sid = "conv_bad_payload"
    eid = "elicit_codex_55555555555555555555555555555555"
    future: asyncio.Future[ElicitationResult] = asyncio.get_running_loop().create_future()
    S._harness_elicitation_registry[eid] = future
    S._harness_elicitation_owners[eid] = sid
    S._harness_pre_resolved_elicitations.pop(eid, None)
    try:
        await S._resolve_elicitation(
            sid, {"elicitation_id": eid, "action": "not-a-real-action"}, None
        )
        assert not future.done()
        assert S._harness_pre_resolved_elicitations.get(eid) is None
    finally:
        S._harness_elicitation_registry.pop(eid, None)
        S._harness_elicitation_owners.pop(eid, None)
        S._harness_pre_resolved_elicitations.pop(eid, None)
