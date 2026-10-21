"""Preference control authentication, typed tunnel transport and bounded waiting."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.host.frames import (
    CAP_PREFERENCE_BROKER,
    CAP_PREFERENCE_PREPARE,
    CAP_PREFERENCES,
    HostHelloFrame,
    HostPreferencesFrame,
    HostPreferencesResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.preference_controls import PreferenceControlResult
from omnigent.preference_sync import PreferenceSnapshot
from omnigent.server.host_registry import HostRegistry
from omnigent.server.preference_requests import PreferencePolicy
from omnigent.server.routes.preference_controls import create_preference_controls_router
from omnigent.stores.host_store import HostStore
from omnigent.stores.preference_store.sqlalchemy_store import SqlAlchemyPreferenceStore

HOST = "a828988dc0b441fb8d04dad3761773b9"
URL = f"/v1/hosts/{HOST}/preferences"


class Auth:
    def get_user_id(self, request):
        return request.headers.get("x-test-user")


@pytest.fixture
def preference_gate():
    return {"enabled": True}


@pytest.fixture
def controls(db_uri, preference_gate):
    registry = HostRegistry()
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(HOST, "machine", "owner")
    store = SqlAlchemyPreferenceStore(db_uri, authority_id="test")
    app = FastAPI()
    app.include_router(
        create_preference_controls_router(
            registry,
            hosts,
            store,
            auth_provider=Auth(),
            policy=PreferencePolicy(enabled=lambda: preference_gate["enabled"]),
        ),
        prefix="/v1",
    )

    @app.exception_handler(OmnigentError)
    async def errors(request: Request, exc: OmnigentError):
        return JSONResponse(status_code=exc.http_status, content={"detail": exc.message})

    connection = registry.register(
        HOST,
        AsyncMock(),
        HostHelloFrame(
            version="test",
            frame_protocol_version=1,
            name="machine",
            capabilities=[CAP_PREFERENCES, CAP_PREFERENCE_PREPARE],
        ),
        owner="owner",
    )
    return app, registry, connection, store


@pytest.mark.parametrize("broker", [False, True])
@pytest.mark.parametrize("user,status", [(None, 401), ("stranger", 403), ("owner", 200)])
async def test_owner_controls_cross_typed_wire(controls, user, status, broker):
    app, _, connection, store = controls
    if broker:
        connection.hello.capabilities.append(CAP_PREFERENCE_BROKER)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        task = asyncio.create_task(
            client.post(
                URL, json={"action": "status"}, headers={"x-test-user": user} if user else {}
            )
        )
        if status == 200:
            frame = decode_host_frame(
                (await asyncio.wait_for(connection.outbound_queue.get(), 2)).data
            )
            assert isinstance(frame, HostPreferencesFrame)
            assert frame.identity == store.identity("owner")
            assert frame.control.action == "status"
            assert frame.broker is broker
            assert (frame.request_id in connection.preference_brokers) is broker
            result = decode_host_frame(
                encode_host_frame(
                    HostPreferencesResultFrame(
                        frame.request_id, PreferenceControlResult(connected=False)
                    )
                )
            )
            connection.pending_preferences[frame.request_id].set_result(result)
        response = await task
    assert response.status_code == status
    assert not connection.pending_preferences
    assert not connection.preference_brokers
    assert connection.outbound_queue.empty()


@pytest.mark.parametrize(
    "failure",
    [
        "old_host",
        "old_prepare_host",
        "disconnect",
        "timeout",
        "wrong_identity",
        "invalid_reply",
        "body",
        "busy",
        "invalid_action",
    ],
)
async def test_control_failures_preserve_context_and_cleanup(controls, monkeypatch, failure):
    app, registry, connection, store = controls
    expected = 502
    action = {"action": "status"}
    if failure == "old_host":
        connection.hello.capabilities.clear()
        expected = 501
    elif failure == "old_prepare_host":
        connection.hello.capabilities.remove(CAP_PREFERENCE_PREPARE)
        action = {"action": "prepare"}
        expected = 501
    elif failure == "timeout":
        monkeypatch.setattr(
            "omnigent.server.routes.preference_controls.CONTROL_TIMEOUT_SECONDS", 0.02
        )
        expected = 503
    elif failure == "body":
        monkeypatch.setattr("omnigent.server.routes.preference_controls.MAX_CONTROL_BYTES", 1)
        expected = 413
    elif failure == "busy":
        for index in range(4):
            connection.pending_preferences[str(index)] = asyncio.get_running_loop().create_future()
        expected = 429
    elif failure == "invalid_action":
        action["path"] = "/private"
        expected = 422
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        task = asyncio.create_task(client.post(URL, json=action, headers={"x-test-user": "owner"}))
        if failure in {"disconnect", "wrong_identity", "invalid_reply"}:
            frame = decode_host_frame(
                (await asyncio.wait_for(connection.outbound_queue.get(), 2)).data
            )
            assert isinstance(frame, HostPreferencesFrame)
            if failure == "disconnect":
                registry.deregister(HOST)
                expected = 503
            else:
                result = (
                    PreferenceControlResult(
                        connected=True, shared=PreferenceSnapshot(identity=store.identity("other"))
                    )
                    if failure == "wrong_identity"
                    else None
                )
                connection.pending_preferences[frame.request_id].set_result(
                    HostPreferencesResultFrame(frame.request_id, result, error="private-secret")
                )
        response = await task
    assert response.status_code == expected, response.text
    assert "private-secret" not in response.text
    if failure != "busy":
        assert not connection.pending_preferences


@pytest.mark.parametrize("reply", ["valid", "missing", "changed"])
async def test_prepare_binds_authoritative_snapshot_and_requires_ack(controls, reply):
    from uuid import uuid4

    from omnigent.preference_startup import PreferenceLaunch
    from omnigent.preference_sync import PreferenceMutation, SharedPreferenceKey

    app, _, connection, store = controls
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        headers = {"x-test-user": "owner"}
        response = await client.post(URL, json={"action": "prepare"}, headers=headers)
        assert response.status_code == 409
        assert connection.outbound_queue.empty()
        store.apply(
            PreferenceMutation(
                protocol_version=1,
                identity=store.identity("owner"),
                operation_id=uuid4(),
                installation_id="source",
                expected_version=0,
                key=SharedPreferenceKey(kind="harness", name="default"),
                value="pi-native",
            ),
            user_id="owner",
        )
        task = asyncio.create_task(client.post(URL, json={"action": "prepare"}, headers=headers))
        frame = decode_host_frame(
            (await asyncio.wait_for(connection.outbound_queue.get(), 2)).data
        )
        assert isinstance(frame, HostPreferencesFrame)
        assert frame.shared.identity == store.identity("owner")
        assert frame.shared.entries[0].value == "pi-native"
        shared = (
            frame.shared if reply != "changed" else frame.shared.model_copy(update={"entries": []})
        )
        result = PreferenceControlResult(
            connected=True,
            launch=None
            if reply == "missing"
            else PreferenceLaunch(shared=shared, installation_id=HOST, state="current"),
        )
        connection.pending_preferences[frame.request_id].set_result(
            HostPreferencesResultFrame(frame.request_id, result)
        )
        response = await task
        assert response.status_code == (200 if reply == "valid" else 502)
        assert not connection.pending_preferences


async def test_disabled_controls_and_reconnect_do_not_contact_host(
    controls, preference_gate, monkeypatch
):
    from omnigent.server.routes.preference_controls import retry_host_preferences

    app, registry, connection, store = controls
    preference_gate["enabled"] = False
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        for action in ("status", "sync", "prepare", "connect"):
            response = await client.post(
                URL, json={"action": action}, headers={"x-test-user": "owner"}
            )
            assert response.status_code == 404
    monkeypatch.setattr("random.uniform", lambda *_: 0)
    await retry_host_preferences(
        registry, connection, store, "owner", PreferencePolicy(enabled=lambda: False)
    )
    assert connection.outbound_queue.empty()
    assert not connection.preference_brokers
    assert not connection.pending_preferences
    assert store.read(user_id="owner").revision == 0
