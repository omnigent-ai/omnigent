"""Kiro delivery state through the real hook, resolve, snapshot and event routes."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from omnigent.runtime import pending_elicitations
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_session_for_agent, create_test_agent
from tests.test_kiro_native_permissions import (
    _permission_msg,
    _permission_result_msg,
    _record_bytes,
)

pytestmark = pytest.mark.asyncio


async def _wait_for_state(elicitation_id: str, state: str) -> None:
    async with asyncio.timeout(5):
        while True:
            entry = pending_elicitations.lookup(elicitation_id)
            if entry and entry[1]["params"]["native_delivery"]["state"] == state:
                return
            await asyncio.sleep(0.01)


async def test_kiro_mirror_recovers_unconfirmed_hook_without_advancing_queue(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent import cli_auth
    from omnigent.harnesses.kiro_native import permissions as knp

    monkeypatch.setattr(knp, "_DELIVERY_CONFIRM_TIMEOUT_S", 0.02)
    monkeypatch.setattr(knp, "_POLL_INTERVAL_S", 0.01)
    agent = await create_test_agent(client, "kiro-transient-hook")
    sid = await create_session_for_agent(client, agent["id"])
    eid = knp.kiro_permission_elicitation_id(sid, "req-1")
    next_eid = knp.kiro_permission_elicitation_id(sid, "req-2")
    started = asyncio.Event()
    unconfirmed: list[bytes] = []
    sent: list[str] = []
    record_file = knp.acp_record_path(tmp_path)

    async def handle(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        if data.get("native_delivery", {}).get("state") == "unconfirmed":
            unconfirmed.append(request.content)
            assert data["elicitation_id"] == eid
            assert sent == ["accept"]
            assert request.content == unconfirmed[0]
            if len(unconfirmed) <= 2:
                return httpx.Response(429 if len(unconfirmed) == 1 else 503)
        return await client.post(request.url.path, json=data)

    @asynccontextmanager
    async def open_client(*args, **kwargs):
        async with httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(handle)
        ) as mirror_client:
            started.set()
            yield mirror_client

    def send(*args, action: str, **kwargs) -> None:
        sent.append(action)
        if len(sent) == 2:
            with record_file.open("ab") as stream:
                stream.write(_record_bytes(_permission_result_msg("req-1")))

    monkeypatch.setattr(cli_auth, "open_server_client", open_client)
    monkeypatch.setattr(knp, "send_kiro_permission_verdict", send)
    mirror = asyncio.create_task(
        knp.supervise_kiro_permission_mirror(
            base_url="http://test",
            headers={},
            session_id=sid,
            bridge_dir=tmp_path,
            poll_interval_s=0.005,
        )
    )
    try:
        async with asyncio.timeout(5):
            await started.wait()
        record_file.write_bytes(
            _record_bytes(_permission_msg("req-1")) + _record_bytes(_permission_msg("req-2"))
        )
        await _wait_for_state(eid, "ready")
        resolve_url = f"/v1/sessions/{sid}/elicitations/{eid}/resolve"
        assert (await client.post(resolve_url, json={"action": "accept"})).status_code == 202
        await _wait_for_state(eid, "unconfirmed")
        assert len(unconfirmed) == 3
        assert sent == ["accept"]
        assert not mirror.done()
        snapshot = (await client.get(f"/v1/sessions/{sid}")).json()
        assert [entry["elicitation_id"] for entry in snapshot["pending_elicitations"]] == [eid]
        delivery = snapshot["pending_elicitations"][0]["params"]["native_delivery"]
        assert delivery == json.loads(unconfirmed[0])["native_delivery"]
        response = await client.post(
            resolve_url,
            json={"action": "accept", "_meta": {"native_delivery_attempt": delivery["attempt"]}},
        )
        assert response.status_code == 202
        await _wait_for_state(next_eid, "ready")
        async with asyncio.timeout(5):
            while pending_elicitations.lookup(eid, conversation_id=sid) is not None:
                await asyncio.sleep(0.01)
        assert sent == ["accept", "accept"]
    finally:
        mirror.cancel()
        await asyncio.gather(mirror, return_exceptions=True)


async def test_kiro_delivery_stays_pending_and_retries_only_the_current_attempt(
    client: httpx.AsyncClient,
) -> None:
    agent = await create_test_agent(client, "kiro-delivery")
    session_id = await create_session_for_agent(client, agent["id"])
    eid = f"elicit_kiro_{session_id}"
    hook_url = f"/v1/sessions/{session_id}/hooks/native-permission-request"
    resolve_url = f"/v1/sessions/{session_id}/elicitations/{eid}/resolve"
    payload = {
        "elicitation_id": eid,
        "agent": "Kiro",
        "policy_name": "kiro_native_permission",
        "message": "Kiro wants approval for Running: pwd",
        "content_preview": "Running: pwd",
        "native_delivery": {"state": "ready", "attempt": "first"},
    }
    first = asyncio.create_task(client.post(hook_url, json=payload))
    await _wait_for_state(eid, "ready")
    response = await client.post(resolve_url, json={"action": "accept"})
    assert response.status_code == 202
    assert (await first).json()["action"] == "accept"
    await _wait_for_state(eid, "sending")
    snapshot = (await client.get(f"/v1/sessions/{session_id}")).json()
    assert len(snapshot["pending_elicitations"]) == 1
    detail = (await client.get(f"/v1/sessions/{session_id}/elicitations/{eid}")).json()
    assert detail["native_delivery"]["state"] == "sending"

    payload["native_delivery"] = {"state": "unconfirmed", "attempt": "second", "action": "accept"}
    retry = asyncio.create_task(client.post(hook_url, json=payload))
    try:
        await _wait_for_state(eid, "unconfirmed")
        unsupported = await client.post(resolve_url, json={"action": "accept"})
        assert unsupported.status_code == 400
        assert (
            "Update Omnigent or answer in the terminal" in unsupported.json()["error"]["message"]
        )
        assert not retry.done()
        stale = await client.post(
            resolve_url,
            json={
                "action": "accept",
                "_meta": {"native_delivery_attempt": "first"},
            },
        )
        assert stale.status_code == 400
        assert "Refresh the approval" in stale.json()["error"]["message"]
        assert not retry.done()
        changed_decision = await client.post(
            resolve_url,
            json={"action": "decline", "_meta": {"native_delivery_attempt": "second"}},
        )
        assert changed_decision.status_code == 400
        assert not retry.done()
        verdict = {"action": "accept", "_meta": {"native_delivery_attempt": "second"}}
        responses = await asyncio.gather(
            client.post(resolve_url, json=verdict),
            client.post(resolve_url, json=verdict),
        )
        assert all(response.status_code == 202 for response in responses)
        retried = (await retry).json()
        assert retried["action"] == "accept"
        assert retried["native_delivery_attempt"] == "second"
        await _wait_for_state(eid, "sending")
    finally:
        retry.cancel()
        await asyncio.gather(retry, return_exceptions=True)

    # A late receipt during the gap between hook calls clears the same card.
    confirmed = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_elicitation_resolved",
            "data": {"elicitation_id": eid},
        },
    )
    assert confirmed.status_code == 202
    assert pending_elicitations.lookup(eid) is None
    payload["native_delivery"] = {"state": "unconfirmed", "attempt": "third", "action": "accept"}
    async with asyncio.timeout(5):
        resolved = await client.post(hook_url, json=payload)
    assert resolved.status_code == 200
    assert resolved.content == b""
    assert pending_elicitations.lookup(eid) is None


async def test_kiro_terminal_receipt_cancels_a_waiting_retry(client: httpx.AsyncClient) -> None:
    agent = await create_test_agent(client, "kiro-late-receipt")
    session_id = await create_session_for_agent(client, agent["id"])
    eid = f"elicit_kiro_{session_id}"
    task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/native-permission-request",
            json={
                "elicitation_id": eid,
                "agent": "Kiro",
                "policy_name": "kiro_native_permission",
                "native_delivery": {
                    "state": "unconfirmed",
                    "attempt": "retry",
                    "action": "accept",
                },
            },
        )
    )
    try:
        await _wait_for_state(eid, "unconfirmed")
        await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "external_elicitation_resolved",
                "data": {"elicitation_id": eid},
            },
        )
        async with asyncio.timeout(5):
            response = await task
        assert response.content == b""
        assert pending_elicitations.lookup(eid) is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("receipt", [True, False], ids=["confirmed", "abandoned"])
@pytest.mark.parametrize("disconnected", [False, True], ids=["parked", "disconnected"])
async def test_kiro_child_delivery_stays_pending_in_parent_until_receipt(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    receipt: bool,
    disconnected: bool,
) -> None:
    from omnigent.server.routes import sessions

    monkeypatch.setattr(sessions, "_HARNESS_ELICITATION_REPARK_GRACE_S", 0.2)
    agent = await create_test_agent(client, "kiro-child-delivery")
    parent = await create_session_for_agent(client, agent["id"])
    child = (
        SqlAlchemyConversationStore(db_uri)
        .create_conversation(
            kind="sub_agent",
            title="Kiro:approval",
            parent_conversation_id=parent,
            agent_id=agent["id"],
        )
        .id
    )
    eid = f"elicit_kiro_{child}"
    parent_eid = f"elicit_kiro_{parent}"
    parent_hook = asyncio.create_task(
        client.post(
            f"/v1/sessions/{parent}/hooks/native-permission-request",
            json={
                "elicitation_id": parent_eid,
                "agent": "Kiro",
                "policy_name": "kiro_native_permission",
                "native_delivery": {"state": "ready", "attempt": "parent"},
            },
        )
    )
    await _wait_for_state(parent_eid, "ready")
    hook_url = f"/v1/sessions/{child}/hooks/native-permission-request"
    payload = {
        "elicitation_id": eid,
        "agent": "Kiro",
        "policy_name": "kiro_native_permission",
        "native_delivery": {"state": "ready", "attempt": "first"},
    }
    hook = asyncio.create_task(client.post(hook_url, json=payload))

    async def parent_delivery(state: str) -> dict:
        async with asyncio.timeout(5):
            while True:
                snapshot = (await client.get(f"/v1/sessions/{parent}")).json()
                entries = snapshot["pending_elicitations"]
                for entry in entries:
                    if (
                        entry["elicitation_id"] == eid
                        and entry["params"]["native_delivery"]["state"] == state
                    ):
                        return entry
                await asyncio.sleep(0.01)

    try:
        mirrored = await parent_delivery("ready")
        assert mirrored["params"]["target_session_id"] == child
        detail_url = f"/v1/sessions/{child}/elicitations/{eid}"
        assert (await client.get(detail_url)).json()["native_delivery"]["state"] == "ready"
        wrong_detail = await client.get(f"/v1/sessions/{parent}/elicitations/{eid}")
        assert wrong_detail.json()["status"] == "resolved"
        if disconnected:
            hook.cancel()
            await asyncio.gather(hook, return_exceptions=True)
            assert eid not in sessions._harness_elicitation_registry
        wrong_owner = await client.post(
            f"/v1/sessions/{parent}/elicitations/{eid}/resolve", json={"action": "accept"}
        )
        assert wrong_owner.status_code == 202
        assert (await parent_delivery("ready"))["params"]["target_session_id"] == child
        if not disconnected:
            assert not hook.done()
        response = await client.post(
            f"/v1/sessions/{child}/elicitations/{eid}/resolve", json={"action": "accept"}
        )
        assert response.status_code == 202
        if disconnected:
            hook = asyncio.create_task(client.post(hook_url, json=payload))
        async with asyncio.timeout(2):
            assert (await hook).json()["action"] == "accept"
        assert (await parent_delivery("sending"))["params"]["target_session_id"] == child
        assert (await client.get(detail_url)).json()["native_delivery"]["state"] == "sending"
        wrong_receipt = await client.post(
            f"/v1/sessions/{parent}/events",
            json={"type": "external_elicitation_resolved", "data": {"elicitation_id": eid}},
        )
        assert wrong_receipt.status_code == 400
        assert (await parent_delivery("sending"))["params"]["target_session_id"] == child
        if receipt:
            confirmed = await client.post(
                f"/v1/sessions/{child}/events",
                json={"type": "external_elicitation_resolved", "data": {"elicitation_id": eid}},
            )
            assert confirmed.status_code == 202
            assert pending_elicitations.count_for(child) == 0
            assert pending_elicitations.count_for(parent) == 1
        await asyncio.gather(*sessions._deferred_elicitation_clear_tasks)
        snapshot = (await client.get(f"/v1/sessions/{parent}")).json()
        assert [entry["elicitation_id"] for entry in snapshot["pending_elicitations"]] == [
            parent_eid
        ]
        assert pending_elicitations.count_for(parent) == 1
        assert pending_elicitations.count_for(child) == 0
        assert not parent_hook.done()
        assert (await client.get(detail_url)).json()["status"] == "resolved"
    finally:
        hook.cancel()
        parent_hook.cancel()
        await asyncio.gather(hook, parent_hook, return_exceptions=True)


@pytest.mark.parametrize(
    ("delivery", "policy_name"),
    [
        ({"state": [], "attempt": "retry"}, "kiro_native_permission"),
        ({"state": "unconfirmed", "attempt": "retry", "action": {}}, "kiro_native_permission"),
        ({"state": "sending", "attempt": "retry", "action": "accept"}, "kiro_native_permission"),
        ({"state": "ready", "attempt": ""}, "kiro_native_permission"),
        pytest.param(
            {"state": "ready", "attempt": "retry"}, "native_permission", id="wrong-policy"
        ),
    ],
)
async def test_invalid_kiro_delivery_metadata_is_rejected(
    client: httpx.AsyncClient, delivery: dict, policy_name: str
) -> None:
    from omnigent.server.routes import sessions

    agent = await create_test_agent(client, "kiro-invalid-delivery")
    sid = await create_session_for_agent(client, agent["id"])
    eid = f"elicit_kiro_{sid}"
    response = await client.post(
        f"/v1/sessions/{sid}/hooks/native-permission-request",
        json={
            "elicitation_id": eid,
            "agent": "Kiro",
            "policy_name": policy_name,
            "native_delivery": delivery,
        },
    )
    assert response.status_code == 400
    assert pending_elicitations.count_for(sid) == 0
    assert eid not in sessions._harness_elicitation_registry


@pytest.mark.parametrize("ending", ["disconnect", "sending", "repark", "receipt", "new_attempt"])
async def test_kiro_abandoned_wait_cleanup(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    from omnigent.server.routes import sessions

    monkeypatch.setattr(sessions, "_HARNESS_ELICITATION_REPARK_GRACE_S", 0.1)
    agent = await create_test_agent(client, "kiro-cleanup")
    sid = await create_session_for_agent(client, agent["id"])
    eid = f"elicit_kiro_{sid}"
    hook_url = f"/v1/sessions/{sid}/hooks/native-permission-request"
    payload = {
        "elicitation_id": eid,
        "policy_name": "kiro_native_permission",
        "native_delivery": {"state": "ready", "attempt": "first"},
    }
    hook = asyncio.create_task(client.post(hook_url, json=payload))
    retry = None
    try:
        await _wait_for_state(eid, "ready")
        existing_clears = set(sessions._deferred_elicitation_clear_tasks)
        if ending == "sending":
            response = await client.post(
                f"/v1/sessions/{sid}/elicitations/{eid}/resolve", json={"action": "accept"}
            )
            assert response.status_code == 202
            assert (await hook).json()["native_delivery_attempt"] == "first"
            await _wait_for_state(eid, "sending")
        else:
            hook.cancel()
            await asyncio.gather(hook, return_exceptions=True)
        clears = set(sessions._deferred_elicitation_clear_tasks) - existing_clears
        assert len(clears) == 1
        assert pending_elicitations.count_for(sid) == 1
        if ending in ("repark", "new_attempt"):
            if ending == "new_attempt":
                payload["native_delivery"] = {"state": "ready", "attempt": "second"}
            retry = asyncio.create_task(client.post(hook_url, json=payload))
            async with asyncio.timeout(5):
                while eid not in sessions._harness_elicitation_registry:
                    await asyncio.sleep(0.001)
            if ending == "new_attempt":
                await client.post(
                    f"/v1/sessions/{sid}/elicitations/{eid}/resolve", json={"action": "accept"}
                )
                await retry
                newer_clears = (
                    set(sessions._deferred_elicitation_clear_tasks) - clears - existing_clears
                )
                for task in newer_clears:
                    task.cancel()
                await asyncio.gather(*newer_clears, return_exceptions=True)
        elif ending == "receipt":
            await client.post(
                f"/v1/sessions/{sid}/events",
                json={"type": "external_elicitation_resolved", "data": {"elicitation_id": eid}},
            )
        await asyncio.gather(*clears)
        survives = ending in ("repark", "new_attempt")
        snapshot = (await client.get(f"/v1/sessions/{sid}")).json()
        assert bool(snapshot["pending_elicitations"]) == survives
        assert pending_elicitations.count_for(sid) == int(survives)
        detail_url = f"/v1/sessions/{sid}/elicitations/{eid}"
        detail = (await client.get(detail_url)).json()
        if not survives:
            assert detail == {
                "status": "resolved",
                "resolution": "confirmed" if ending == "receipt" else "unanswered",
            }
        if ending == "sending":
            payload["native_delivery"] = {
                "state": "unconfirmed",
                "attempt": "recovered",
                "action": "accept",
            }
            retry = asyncio.create_task(client.post(hook_url, json=payload))
            await _wait_for_state(eid, "unconfirmed")
            assert (await client.get(detail_url)).json()["native_delivery"] == payload[
                "native_delivery"
            ]
            assert pending_elicitations.native_resolution_for(sid, eid) is None
        if ending in ("sending", "disconnect"):
            await client.post(
                f"/v1/sessions/{sid}/events",
                json={"type": "external_elicitation_resolved", "data": {"elicitation_id": eid}},
            )
            if retry is not None:
                await retry
            assert (await client.get(detail_url)).json() == {
                "status": "resolved",
                "resolution": "confirmed",
            }
    finally:
        if retry is not None:
            await client.post(
                f"/v1/sessions/{sid}/events",
                json={"type": "external_elicitation_resolved", "data": {"elicitation_id": eid}},
            )
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)
        hook.cancel()
        await asyncio.gather(hook, return_exceptions=True)


@pytest.mark.parametrize("verdict", [{"action": "bogus"}, {"action": "accept", "content": []}])
async def test_kiro_malformed_approval_event_keeps_request_pending(
    client: httpx.AsyncClient, verdict: dict
) -> None:
    agent = await create_test_agent(client, "kiro-malformed-verdict")
    sid = await create_session_for_agent(client, agent["id"])
    eid = f"elicit_kiro_{sid}"
    task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{sid}/hooks/native-permission-request",
            json={
                "elicitation_id": eid,
                "policy_name": "kiro_native_permission",
                "native_delivery": {"state": "ready", "attempt": "first"},
            },
        )
    )
    try:
        await _wait_for_state(eid, "ready")
        response = await client.post(
            f"/v1/sessions/{sid}/events",
            json={"type": "approval", "data": {"elicitation_id": eid, **verdict}},
        )
        assert response.status_code == 400
        assert not task.done()
        await _wait_for_state(eid, "ready")
    finally:
        await client.post(
            f"/v1/sessions/{sid}/events",
            json={"type": "external_elicitation_resolved", "data": {"elicitation_id": eid}},
        )
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_new_kiro_worker_does_not_replay_legacy_server_verdict(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.harnesses.kiro_native import permissions as knp
    from omnigent.server.routes import sessions

    monkeypatch.setattr(knp, "_DELIVERY_CONFIRM_TIMEOUT_S", 0.02)
    monkeypatch.setattr(knp, "_POLL_INTERVAL_S", 0.001)
    sent = []
    monkeypatch.setattr(
        knp, "send_kiro_permission_verdict", lambda *a, **kw: sent.append(kw["action"])
    )
    agent = await create_test_agent(client, "kiro-legacy-server")
    sid = await create_session_for_agent(client, agent["id"])
    eid = f"elicit_kiro_{sid}"
    requests = []

    class LegacyHookClient:
        async def post(self, url, *, json, **kwargs):
            # Older hooks ignore delivery metadata. Exercise the production
            # legacy hook and its verdict tombstone, not a canned response.
            payload = {k: v for k, v in json.items() if k != "native_delivery"}
            requests.append((url, payload))
            return await client.post(url, json=payload, **kwargs)

    worker = asyncio.create_task(
        knp._run_one_permission(
            LegacyHookClient(),
            session_id=sid,
            bridge_dir=tmp_path,
            permission=knp.KiroPermissionRequest(
                "request-1", "tool-1", "Running: pwd", "yes", "no"
            ),
            elicitation_id=eid,
            record_offset=0,
        )
    )
    try:
        async with asyncio.timeout(5):
            while eid not in sessions._harness_elicitation_registry:
                await asyncio.sleep(0.001)
        await client.post(
            f"/v1/sessions/{sid}/elicitations/{eid}/resolve", json={"action": "accept"}
        )
        async with asyncio.timeout(5):
            await worker
        await asyncio.sleep(0.05)
        assert sent == ["accept"]
        assert len(requests) == 1
        url, payload = requests[0]
        async with asyncio.timeout(5):
            replay = await client.post(url, json=payload)
        assert replay.json()["action"] == "accept"
        assert "native_delivery_attempt" not in replay.json()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
