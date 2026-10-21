"""Scoped tunnel transport retains the durable client's delivery guarantees."""

from uuid import uuid4

import httpx
import pytest

from omnigent.host.frames import (
    HostPreferenceReplyFrame,
    HostPreferenceRequestFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.preference_broker import PreferenceBrokerTransport, PreferenceRead, PreferenceWrite
from omnigent.preference_client import PreferenceClient
from omnigent.preference_controls import PreferenceApply, PreferenceInspect
from omnigent.preference_sync import PreferenceMutation, SharedPreferenceKey
from omnigent.preference_workspace import ImportChange, ImportPreview
from omnigent.server.preference_broker import PreferenceBrokerGrant
from omnigent.stores.preference_store.sqlalchemy_store import SqlAlchemyPreferenceStore


@pytest.mark.parametrize(
    "fault", [None, "read_only", "identity", "installation", "unreviewed", "budget"]
)
async def test_broker_grant_limits_publication(db_uri, fault):
    store = SqlAlchemyPreferenceStore(db_uri, authority_id="broker")
    operation = PreferenceMutation(
        protocol_version=1,
        identity=store.identity("alice"),
        operation_id=uuid4(),
        installation_id="host",
        key=SharedPreferenceKey(kind="model", name="model"),
        expected_version=0,
        value="requested",
    )
    grant = PreferenceBrokerGrant(store, "alice", "host", PreferenceInspect(action="sync"))
    if fault == "read_only":
        grant.control = PreferenceInspect(action="status")
    elif fault == "identity":
        operation = operation.model_copy(update={"identity": store.identity("bob")})
    elif fault == "installation":
        operation = operation.model_copy(update={"installation_id": "another-host"})
    elif fault == "unreviewed":
        grant.control = PreferenceApply(
            action="apply_import", preview=ImportPreview(identity=operation.identity, changes=[])
        )
    elif fault == "budget":
        grant.remaining = 0
    response = await grant.execute(PreferenceWrite(operation=operation))
    assert response.status == (200 if fault is None else 429 if fault == "budget" else 403)
    assert store.read(user_id="alice").revision == (1 if fault is None else 0)
    assert store.read(user_id="bob").revision == 0
    if fault == "unreviewed":
        from omnigent.preference_sync import SharedPreferenceEntry

        grant.control = PreferenceApply(
            action="apply_import",
            preview=ImportPreview(
                identity=operation.identity,
                changes=[
                    ImportChange(
                        previous=SharedPreferenceEntry(key=operation.key, version=0, value=None),
                        operation=operation,
                    )
                ],
            ),
        )
        assert (await grant.execute(PreferenceWrite(operation=operation))).status == 200


async def test_typed_broker_lost_receipt_keeps_causal_retry(tmp_path, db_uri):
    store = SqlAlchemyPreferenceStore(db_uri, authority_id="broker")
    identity = store.identity("alice")
    client = PreferenceClient(tmp_path / "cache.db", identity=identity, installation_id="host")
    grant = PreferenceBrokerGrant(store, "alice", "host", PreferenceInspect(action="sync"))
    dropped = False
    sent = []

    async def send(request_id, request):
        nonlocal dropped
        decoded = decode_host_frame(
            encode_host_frame(HostPreferenceRequestFrame("parent", request_id, request))
        )
        response = await grant.execute(decoded.request)
        if isinstance(request, PreferenceWrite):
            sent.append(request.operation)
            if not dropped:
                dropped = True
                raise httpx.ReadError("reply lost after commit")
        decoded_reply = decode_host_frame(
            encode_host_frame(HostPreferenceReplyFrame("parent", request_id, response))
        )
        transport.deliver(request_id, decoded_reply.reply)

    transport = PreferenceBrokerTransport(send)
    async with httpx.AsyncClient(base_url="http://preferences", transport=transport) as http:
        await client.sync(http)
        key = SharedPreferenceKey(kind="model", name="model")
        client.enqueue(key, "first")
        client.enqueue(key, "second")
        with pytest.raises(httpx.ReadError, match="reply lost"):
            await client.sync(http)
        assert len(client.pending()) == 2
        assert store.read(user_id="alice").entries[0].value == "first"
        reopened = PreferenceClient(client.path, identity=identity, installation_id="host")
        result = await reopened.sync(http)
        assert sent[0] == sent[1]
        assert sent[0].protocol_version == 2
        assert sent[0].observed_revision == 0 and sent[2].observed_revision == 1
        assert result.revision == 2
        assert result.entries[0].value == "second" and result.entries[0].version == 2
        assert not reopened.pending()
        with pytest.raises(ValueError, match="Only preference"):
            await http.get("/v1/sessions")


async def test_broker_read_only_grant_can_page(db_uri):
    store = SqlAlchemyPreferenceStore(db_uri, authority_id="broker")
    grant = PreferenceBrokerGrant(store, "alice", "host", PreferenceInspect(action="status"))
    result = await grant.execute(PreferenceRead(limit=1))
    assert result.page.identity == store.identity("alice")
    assert result.page.entries == []


@pytest.mark.parametrize("change", [None, "value", "expected_version", "operation_id"])
async def test_import_grant_allows_only_unattempted_revision_refresh(db_uri, change):
    from omnigent.preference_sync import SharedPreferenceEntry

    store = SqlAlchemyPreferenceStore(db_uri, authority_id="broker")
    reviewed = PreferenceMutation(
        protocol_version=2,
        observed_revision=0,
        identity=store.identity("alice"),
        installation_id="host",
        operation_id=uuid4(),
        key=SharedPreferenceKey(kind="model", name="model"),
        expected_version=0,
        value="chosen",
    )
    store.apply(reviewed.model_copy(update={"operation_id": uuid4()}), user_id="alice")
    control = PreferenceApply(
        action="apply_import",
        preview=ImportPreview(
            identity=reviewed.identity,
            changes=[
                ImportChange(
                    previous=SharedPreferenceEntry(key=reviewed.key, version=0, value=None),
                    operation=reviewed,
                )
            ],
        ),
    )
    operation = reviewed.model_copy(update={"observed_revision": 1})
    if change is not None:
        operation = operation.model_copy(
            update={
                change: {
                    "value": "unreviewed",
                    "expected_version": 1,
                    "operation_id": uuid4(),
                }[change]
            }
        )
    grant = PreferenceBrokerGrant(store, "alice", "host", control)
    result = await grant.execute(PreferenceWrite(operation=operation))
    assert result.status == (200 if change is None else 403)
