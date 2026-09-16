"""Authorization, host correlation and ephemeral transport for settings setup."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI, Request, WebSocketDisconnect
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from omnigent.errors import OmnigentError
from omnigent.host.frames import (
    HostHelloFrame,
    HostSetupRequestFrame,
    HostSetupResultFrame,
    HostSetupTerminalFrame,
    SetupMethod,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.host_setup import create_host_setup_router, proxy_setup
from omnigent.server.routes.host_tunnel import _receive_loop

pytestmark = pytest.mark.asyncio


class Identity:
    def get_user_id(self, request):
        return request.headers.get("x-user")


def make_app(*, enabled=True, protocol=1, owner="alice", online=True):
    registry = HostRegistry()
    store = Mock()
    host = SimpleNamespace(host_id="host-a", user_id=owner, status="offline", last_seen_at=0)
    store.get_host.side_effect = lambda host_id: host if host_id == "host-a" else None
    conn = registry.register(
        "host-a",
        Mock(),
        HostHelloFrame("test", 1, "Host A", setup_protocol_version=protocol),
        owner,
    )
    if not online:
        registry.deregister("host-a", conn=conn)
    app = FastAPI()
    flags = FeatureFlags(frozenset({Feature.HARNESS_INSTALL}) if enabled else frozenset())
    app.include_router(
        create_host_setup_router(registry, store, auth_provider=Identity(), flags=flags),
        prefix="/v1",
    )

    @app.exception_handler(OmnigentError)
    async def handle_error(request: Request, exc: OmnigentError):
        return JSONResponse({"detail": exc.message}, status_code=exc.http_status)

    return app, registry, conn, store


async def respond(conn, payload):
    raw = await conn.outbound_queue.get()
    frame = decode_host_frame(raw)
    assert isinstance(frame, HostSetupRequestFrame)
    conn.pending_setup[frame.request_id].set_result({"payload": payload})
    return frame


@pytest.mark.parametrize(
    ("options", "headers", "path", "status"),
    [
        ({}, {}, "host-a/setup", 401),
        ({}, {"x-user": "bob"}, "host-a/setup", 403),
        ({}, {"x-user": "alice"}, "missing/setup", 404),
        ({"online": False}, {"x-user": "alice"}, "host-a/setup", 409),
        ({"protocol": 0}, {"x-user": "alice"}, "host-a/setup", 501),
    ],
)
async def test_inventory_rejections(options, headers, path, status):
    app, _, _, _ = make_app(**options)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get("/v1/hosts/" + path, headers=headers)
    assert result.status_code == status


async def test_inventory_stays_readable_with_flag_off():
    app, _, conn, _ = make_app(enabled=False)
    response_task = asyncio.create_task(respond(conn, {"providers": []}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get("/v1/hosts/host-a/setup", headers={"x-user": "alice"})
        for path in (
            "setup/actions",
            "setup/detect",
            "setup-operations",
            "setup-operations/operation/verify",
        ):
            denied = await client.post(
                "/v1/hosts/host-a/" + path, headers={"x-user": "alice"}, json={}
            )
            assert denied.status_code == 404
    assert result.json() == {"providers": [], "mutations_enabled": False, "feature_enabled": False}
    assert (await asyncio.wait_for(response_task, timeout=2)).method == SetupMethod.INVENTORY


async def test_action_secret_preserved_only_in_private_host_payload():
    app, _, conn, _ = make_app()
    responder = asyncio.create_task(respond(conn, {"ok": True}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/hosts/host-a/setup/actions",
            headers={"x-user": "alice"},
            json={
                "action": "add_key",
                "provider": "anthropic",
                "model": "fixture-model",
                "secret": "fixture-key-123",
            },
        )
    frame = await asyncio.wait_for(responder, timeout=2)
    assert frame.secret_payload["secret"] == "fixture-key-123"
    assert "fixture-key-123" not in repr(frame)
    assert "fixture-key-123" not in response.text
    assert response.status_code == 200


async def test_invalid_body_does_not_reflect_secrets():
    app, _, conn, _ = make_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/hosts/host-a/setup/actions",
            headers={"x-user": "alice"},
            json={
                "action": "unknown",
                "secret": "fixture-secret",
            },
        )
    assert response.status_code == 422
    assert "fixture-secret" not in response.text
    assert conn.outbound_queue.empty()


@pytest.mark.parametrize(
    ("options", "headers", "status"),
    [
        ({}, {}, 401),
        ({"online": False}, {"x-user": "alice"}, 409),
        ({"protocol": 0}, {"x-user": "alice"}, 501),
    ],
)
async def test_verify_rejects_unavailable_or_unauthenticated_host(options, headers, status):
    app, _, conn, _ = make_app(**options)
    queued_before = conn.outbound_queue.qsize()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post(
            "/v1/hosts/host-a/setup-operations/operation/verify", headers=headers
        )
    assert result.status_code == status
    assert conn.outbound_queue.qsize() == queued_before


async def test_verify_is_authorized_and_forwarded_to_selected_host():
    app, _, conn, _ = make_app()
    denied_path = "/v1/hosts/host-a/setup-operations/operation/verify"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.post(denied_path, headers={"x-user": "bob"})
        assert denied.status_code == 403
        responder = asyncio.create_task(respond(conn, {"state": "succeeded"}))
        accepted = await client.post(denied_path, headers={"x-user": "alice"})

    frame = await asyncio.wait_for(responder, timeout=2)
    assert accepted.status_code == 200
    assert accepted.json() == {"state": "succeeded"}
    assert frame.method == SetupMethod.VERIFY
    assert frame.operation_id == "operation"


async def test_replaced_host_fails_request_and_drops_output():
    _, registry, conn, _ = make_app()
    pending = asyncio.create_task(proxy_setup(registry, conn, SetupMethod.INVENTORY))
    await conn.outbound_queue.get()
    output = asyncio.Queue(maxsize=2)
    output.put_nowait({"type": "output", "data": "secret"})
    conn.setup_attachments["attachment"] = ("operation", output)
    registry.register("host-a", Mock(), conn.hello, "alice")
    with pytest.raises(Exception) as caught:
        await pending
    assert caught.value.status_code == 502
    assert await output.get() is None
    assert not conn.setup_attachments


class Incoming:
    def __init__(self):
        self.messages = asyncio.Queue()

    async def receive(self):
        return await self.messages.get()


async def test_result_and_terminal_frames_are_bound_to_host_and_correlation():
    _, registry, conn, store = make_app()
    other = registry.register("host-b", Mock(), conn.hello, "alice")
    target = asyncio.get_running_loop().create_future()
    conn.pending_setup["request"] = target
    output = asyncio.Queue(maxsize=2)
    conn.setup_attachments["attachment"] = ("operation", output)
    incoming = Incoming()
    receiver = asyncio.create_task(
        _receive_loop(incoming, other, "host-b", store, registry, None, None, None)
    )
    await incoming.messages.put(
        {
            "type": "websocket.receive",
            "text": encode_host_frame(HostSetupResultFrame("request", {"wrong": True})),
        }
    )
    await incoming.messages.put({"type": "websocket.disconnect"})
    with pytest.raises(WebSocketDisconnect):
        await receiver
    assert not target.done()
    incoming = Incoming()
    receiver = asyncio.create_task(
        _receive_loop(incoming, conn, "host-a", store, registry, None, None, None)
    )
    for frame in [
        HostSetupResultFrame("wrong-request", {}),
        HostSetupTerminalFrame("wrong-operation", "attachment", {"type": "output"}),
    ]:
        await incoming.messages.put(
            {"type": "websocket.receive", "text": encode_host_frame(frame)}
        )
    await incoming.messages.put(
        {
            "type": "websocket.receive",
            "text": encode_host_frame(HostSetupResultFrame("request", {"correct": True})),
        }
    )
    await incoming.messages.put({"type": "websocket.disconnect"})
    with pytest.raises(WebSocketDisconnect):
        await receiver
    assert target.result()["payload"] == {"correct": True}
    assert output.empty()


def ws_scope(headers):
    path = "/v1/hosts/host-a/setup-operations/operation/attach"
    return {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ([], 1008),
        ([(b"x-user", b"bob")], 1008),
        ([(b"x-user", b"alice"), (b"origin", b"https://evil.example")], 4403),
    ],
)
async def test_attachment_rejected_before_accept(monkeypatch, headers, expected):
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "true")
    app, _, conn, _ = make_app()
    comm = ApplicationCommunicator(app, ws_scope(headers))
    await comm.send_input({"type": "websocket.connect"})
    result = await comm.receive_output(timeout=1)
    assert result["type"] == "websocket.close"
    assert result["code"] == expected
    assert conn.outbound_queue.empty()
    await comm.wait()


async def test_setup_frames_never_enter_content_capture(monkeypatch):
    record = Mock()
    monkeypatch.setattr("omnigent.runtime.telemetry.record_message_payload", record)
    frames = [
        HostSetupRequestFrame("request", SetupMethod.ACTION, {"secret": "fixture"}),
        HostSetupResultFrame("request", {"ok": True}),
        HostSetupTerminalFrame("operation", "attachment", {"data": "fixture"}),
    ]
    for frame in frames:
        assert decode_host_frame(encode_host_frame(frame)) == frame
    record.assert_not_called()
    old = decode_host_frame(
        json.dumps(
            {"kind": "host.hello", "version": "1", "frame_protocol_version": 1, "name": "old"}
        )
    )
    assert old.setup_protocol_version == 0


async def test_http_secret_reaches_real_host_core_without_masking(monkeypatch, tmp_path):
    from omnigent.config import load_global_config
    from omnigent.host.setup_transport import HostSetupDispatcher

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    stored = {}
    monkeypatch.setattr(
        "omnigent.onboarding.secrets.store_secret",
        lambda name, value: stored.update({name: value}),
    )
    app, _, conn, _ = make_app()
    dispatcher = HostSetupDispatcher()

    async def host():
        request = decode_host_frame(await conn.outbound_queue.get())
        assert isinstance(request, HostSetupRequestFrame)
        result = await dispatcher.request(request, Mock())
        conn.pending_setup[request.request_id].set_result(
            {
                "payload": result.payload,
                "error_status": result.error_status,
                "error": result.error,
            }
        )

    host_task = asyncio.create_task(host())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/hosts/host-a/setup/actions",
            headers={"x-user": "alice"},
            json={
                "action": "add_key",
                "provider": "anthropic",
                "model": "fixture-model",
                "name": "test-key",
                "secret": "actual-fixture-value",
            },
        )
    await asyncio.wait_for(host_task, timeout=2)
    assert response.status_code == 200, response.text
    assert list(stored.values()) == ["actual-fixture-value"]
    assert load_global_config()["providers"]["test-key"]["anthropic"][
        "api_key_ref"
    ] == "keychain:" + next(iter(stored))
    assert "actual-fixture-value" not in response.text
    assert "actual-fixture-value" not in (tmp_path / "config.yaml").read_text()


async def test_attachment_bridges_binary_io_resize_and_detaches_without_cancel():
    import base64

    app, _, conn, _ = make_app()
    comm = ApplicationCommunicator(app, ws_scope([(b"x-user", b"alice")]))
    await comm.send_input({"type": "websocket.connect"})
    assert (await comm.receive_output(timeout=1))["type"] == "websocket.accept"
    attach = decode_host_frame(await conn.outbound_queue.get())
    assert attach.method == SetupMethod.ATTACH
    conn.pending_setup[attach.request_id].set_result({"payload": {"state": "running"}})
    output = conn.setup_attachments[attach.attachment_id][1]
    output.put_nowait(
        {
            "type": "output",
            "encoding": "base64",
            "data": base64.b64encode(b"Vendor prompt").decode(),
        }
    )
    assert (await comm.receive_output(timeout=1))["bytes"] == b"Vendor prompt"
    await comm.send_input({"type": "websocket.receive", "bytes": b"response\r"})
    terminal = decode_host_frame(await conn.outbound_queue.get())
    assert terminal.operation_id == "operation"
    assert terminal.attachment_id == attach.attachment_id
    assert base64.b64decode(terminal.secret_payload["data"]) == b"response\r"
    await comm.send_input(
        {
            "type": "websocket.receive",
            "text": json.dumps({"type": "resize", "cols": 80, "rows": 24, "command": "ignored"}),
        }
    )
    resize = decode_host_frame(await conn.outbound_queue.get())
    assert resize.secret_payload == {"type": "resize", "cols": 80, "rows": 24}
    await comm.send_input({"type": "websocket.disconnect"})
    await comm.wait(timeout=1)
    detach = decode_host_frame(await conn.outbound_queue.get())
    assert detach.method == SetupMethod.DETACH
    assert detach.attachment_id == attach.attachment_id
    assert not conn.setup_attachments
    assert conn.outbound_queue.empty()


async def test_offline_attachment_closes_before_accept():
    app, _, _, _ = make_app(online=False)
    comm = ApplicationCommunicator(app, ws_scope([(b"x-user", b"alice")]))
    await comm.send_input({"type": "websocket.connect"})
    result = await comm.receive_output(timeout=1)
    assert result["type"] == "websocket.close"
    assert result["code"] == 1008
    assert result["reason"] == "host is offline"
    await comm.wait()


@pytest.mark.parametrize(
    "body", [None, {"import_path": "/tmp/fixture-import.json", "import_source": "acpx"}]
)
async def test_detection_forwards_typed_optional_custom_file(body):
    app, _, conn, _ = make_app()
    responder = asyncio.create_task(respond(conn, {"imports": []}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post(
            "/v1/hosts/host-a/setup/detect", headers={"x-user": "alice"}, json=body
        )
    assert result.status_code == 200
    frame = await asyncio.wait_for(responder, timeout=2)
    assert frame.method == SetupMethod.DETECT
    assert frame.secret_payload == (body or {"import_path": None, "import_source": None})


async def test_detection_forwards_explicit_harness_status_to_selected_host():
    app, _, conn, store = make_app()
    status = {
        "harness_status": {"harness": "antigravity-native", "availability": "needs-auth"},
        "warnings": [],
    }
    responder = asyncio.create_task(respond(conn, status))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post(
            "/v1/hosts/host-a/setup/detect",
            headers={"x-user": "alice"},
            json={"harness": "antigravity-native"},
        )

    frame = await asyncio.wait_for(responder, timeout=2)
    assert result.status_code == 200
    assert result.json() == status
    assert frame.method == SetupMethod.DETECT
    assert frame.secret_payload == {
        "import_path": None,
        "import_source": None,
        "harness": "antigravity-native",
    }
    store.get_host.assert_called_once_with("host-a")


async def test_detection_forwards_opt_in_pi_default_only_when_requested():
    app, _, conn, _ = make_app()
    responder = asyncio.create_task(respond(conn, {"pi_default_checked": True}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post(
            "/v1/hosts/host-a/setup/detect",
            headers={"x-user": "alice"},
            json={"pi_default": True},
        )
    frame = await asyncio.wait_for(responder, timeout=2)
    assert result.status_code == 200
    assert frame.secret_payload == {
        "import_path": None,
        "import_source": None,
        "pi_default": True,
    }


async def test_failed_save_and_cleanup_returns_sanitized_http_status(monkeypatch, tmp_path):
    from omnigent.host.setup_transport import HostSetupDispatcher

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))

    def fail_save(*_args, **_kwargs):
        raise OSError("fixture-private-save-secret")

    def fail_cleanup(*_args):
        raise OSError("fixture-private-cleanup-secret")

    monkeypatch.setattr(
        "omnigent.onboarding.setup_operations.store_staged_secret", lambda *_: None
    )
    monkeypatch.setattr("omnigent.onboarding.setup_operations.save_setup_settings", fail_save)
    monkeypatch.setattr(
        "omnigent.onboarding.setup_operations.cleanup_unreferenced_secret", fail_cleanup
    )
    app, _, conn, _ = make_app()
    dispatcher = HostSetupDispatcher()

    async def host():
        request = decode_host_frame(await conn.outbound_queue.get())
        assert isinstance(request, HostSetupRequestFrame)
        result = await dispatcher.request(request, Mock())
        conn.pending_setup[request.request_id].set_result(
            {
                "payload": result.payload,
                "error_status": result.error_status,
                "error": result.error,
            }
        )
        return result

    host_task = asyncio.create_task(host())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/hosts/host-a/setup/actions",
            headers={"x-user": "alice"},
            json={
                "action": "add_gateway",
                "name": "fixture-gateway",
                "base_url": "https://gateway.example/v1",
                "families": ["openai"],
                "models": {"openai": "fixture-model"},
                "secret": "fixture-sensitive-value",
            },
        )
    frame = await asyncio.wait_for(host_task, timeout=2)
    safe_message = "Setup was not saved; stored secret cleanup did not complete"
    assert frame.error_status == 502
    assert frame.error == safe_message
    assert response.status_code == 502
    assert response.json() == {"detail": safe_message}
    assert "fixture-sensitive-value" not in repr(frame)
    assert "fixture-sensitive-value" not in response.text
    assert "fixture-private" not in response.text


@pytest.mark.parametrize(
    ("options", "headers", "harness", "expected"),
    [
        ({"enabled": False}, {"x-user": "alice"}, "antigravity-native", 404),
        ({}, {}, "antigravity-native", 401),
        ({}, {"x-user": "bob"}, "antigravity-native", 403),
        ({}, {"x-user": "alice"}, "arbitrary-command", 422),
    ],
)
async def test_explicit_harness_status_rejections_do_not_reach_host(
    options, headers, harness, expected
):
    app, _, conn, _ = make_app(**options)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post(
            "/v1/hosts/host-a/setup/detect",
            headers=headers,
            json={"harness": harness},
        )
    assert result.status_code == expected
    assert conn.outbound_queue.empty()
