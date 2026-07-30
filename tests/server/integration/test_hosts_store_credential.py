"""
Integration tests for ``POST /v1/hosts/{id}/harnesses/{harness}/credential``.

Wires a real host tunnel + REST router pair, drives a fake host that
auto-replies to ``host.store_secret`` frames, and exercises the endpoint's
contract end-to-end. Mirrors ``test_hosts_install_harness.py`` — the credential
write shares the same owner-scoped, flag-gated, host-forwarded design.

The security-sensitive property (the daemon writes the secret; the server is an
authz'd pass-through that never persists it) is exercised here at the route
layer: the fake host records the frame it receives and replies with a refreshed
readiness map, exactly as the real daemon would after writing the credential.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from omnigent.host.frames import (
    HostDetectCredentialsFrame,
    HostDetectCredentialsResultFrame,
    HostHelloFrame,
    HostStoreSecretFrame,
    HostStoreSecretResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.host_tunnel import create_host_tunnel_router
from omnigent.server.routes.hosts import create_hosts_router
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_store import HostStore

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.flaky(reruns=2, reruns_delay=1),
]

_HOST_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
_HOST_NAME = "credential-test-laptop"


@pytest.fixture(autouse=True)
def _enable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the feature flag for every test except the flag-off case."""
    monkeypatch.setenv("OMNIGENT_HARNESS_INSTALL_ENABLED", "1")


def _websocket_scope(path: str) -> dict[str, object]:
    return {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }


def _hello_text(name: str = _HOST_NAME) -> str:
    return encode_host_frame(
        HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name=name)
    )


async def _connect_mock_host(app: FastAPI, registry: HostRegistry) -> ApplicationCommunicator:
    comm = ApplicationCommunicator(app, _websocket_scope(f"/v1/hosts/{_HOST_ID}/tunnel"))
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept"
    await comm.send_input({"type": "websocket.receive", "text": _hello_text()})
    while registry.get(_HOST_ID) is None:
        await asyncio.sleep(0.01)
    return comm


@pytest.fixture()
def cred_app(
    db_uri: str,
) -> tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore]:
    registry = HostRegistry()
    host_store = HostStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    app = FastAPI()
    app.include_router(create_host_tunnel_router(registry, host_store), prefix="/v1")
    app.include_router(create_hosts_router(registry, host_store, conv_store), prefix="/v1")
    return app, registry, host_store, conv_store


@pytest.fixture()
async def cred_setup(
    cred_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> AsyncIterator[
    tuple[FastAPI, HostRegistry, list[HostStoreSecretFrame], dict[str, dict[str, Any]]]
]:
    """Connect a mock host that records store_secret frames and auto-replies.

    ``received`` collects every ``host.store_secret`` frame the route forwards
    (so tests can assert the server passed the credential through faithfully),
    and ``replies`` lets a test override the reply per harness (default: ok +
    the harness flipped ready).
    """
    app, registry, _hs, _cs = cred_app
    comm = await _connect_mock_host(app, registry)
    received: list[HostStoreSecretFrame] = []
    replies: dict[str, dict[str, Any]] = {}
    stop_drain = asyncio.Event()

    async def _drain() -> None:
        while not stop_drain.is_set():
            try:
                output = await comm.receive_output(timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if output.get("type") != "websocket.send":
                continue
            text = output.get("text")
            if not isinstance(text, str):
                continue
            frame = decode_host_frame(text)
            if isinstance(frame, HostDetectCredentialsFrame):
                # Fixed non-secret descriptor set for the adopt-detect test.
                await comm.send_input(
                    {
                        "type": "websocket.receive",
                        "text": encode_host_frame(
                            HostDetectCredentialsResultFrame(
                                request_id=frame.request_id,
                                credentials=[
                                    {
                                        "family": "anthropic",
                                        "source": "$ANTHROPIC_API_KEY",
                                        "env_var": "ANTHROPIC_API_KEY",
                                    }
                                ],
                            )
                        ),
                    }
                )
                continue
            if not isinstance(frame, HostStoreSecretFrame):
                continue
            received.append(frame)
            reply = replies.get(frame.harness, {})
            reply_frame = HostStoreSecretResultFrame(
                request_id=frame.request_id,
                status=reply.get("status", "ok"),
                configured_harnesses=reply.get("configured_harnesses", {frame.harness: True}),
                error=reply.get("error"),
            )
            await comm.send_input(
                {"type": "websocket.receive", "text": encode_host_frame(reply_frame)}
            )

    drain_task = asyncio.create_task(_drain())
    try:
        yield app, registry, received, replies
    finally:
        stop_drain.set()
        try:
            await asyncio.wait_for(drain_task, timeout=1.0)
        except asyncio.TimeoutError:
            drain_task.cancel()


# ── Happy path ──────────────────────────────────────────


async def test_store_key_forwards_frame_and_returns_readiness(
    cred_setup: tuple[
        FastAPI, HostRegistry, list[HostStoreSecretFrame], dict[str, dict[str, Any]]
    ],
) -> None:
    """A key write forwards the credential to the host and returns readiness."""
    app, _reg, received, _replies = cred_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/harnesses/claude/credential",
            json={"kind": "key", "secret": "sk-ant-SECRET", "default_model": "claude-sonnet-4-6"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "harness_credential"
    assert body["harness"] == "claude"
    assert body["configured_harnesses"]["claude"] is True
    # The server forwarded the credential faithfully (pass-through).
    assert len(received) == 1
    assert received[0].harness == "claude"
    assert received[0].kind == "key"
    assert received[0].secret_value == "sk-ant-SECRET"


async def test_store_gateway_forwards_base_url(
    cred_setup: tuple[
        FastAPI, HostRegistry, list[HostStoreSecretFrame], dict[str, dict[str, Any]]
    ],
) -> None:
    """A gateway write forwards base_url + wire_api to the host."""
    app, _reg, received, _replies = cred_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/harnesses/codex/credential",
            json={
                "kind": "gateway",
                "secret": "gw-TOKEN",
                "base_url": "https://openrouter.ai/api/v1",
                "wire_api": "chat",
            },
        )
    assert resp.status_code == 200
    assert received[0].kind == "gateway"
    assert received[0].base_url == "https://openrouter.ai/api/v1"
    assert received[0].wire_api == "chat"


async def test_adopt_forwards_env_var_without_secret(
    cred_setup: tuple[
        FastAPI, HostRegistry, list[HostStoreSecretFrame], dict[str, dict[str, Any]]
    ],
) -> None:
    """An adopt request forwards the env var name and no secret value."""
    app, _reg, received, _replies = cred_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/harnesses/codex/credential",
            json={"kind": "adopt", "env_var": "OPENAI_API_KEY"},
        )
    assert resp.status_code == 200
    assert received[0].kind == "adopt"
    assert received[0].env_var == "OPENAI_API_KEY"
    assert received[0].secret_value is None


async def test_concurrent_writes_to_one_host_are_serialized(
    cred_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """Two overlapping credential writes to one host don't interleave.

    The daemon's write is a non-atomic read-modify-write of config.yaml, so the
    route serializes writes per host (credential_write_lock). This drives a host
    that HOLDS its first reply until both requests are in flight, then asserts
    the second frame only reaches the host after the first completes — i.e. the
    lock kept them from overlapping.
    """
    app, registry, _hs, _cs = cred_app
    comm = await _connect_mock_host(app, registry)
    conn = registry.get(_HOST_ID)
    assert conn is not None

    arrivals: list[str] = []
    release_first = asyncio.Event()
    stop = asyncio.Event()

    async def _drain() -> None:
        first_seen = False
        while not stop.is_set():
            try:
                output = await comm.receive_output(timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if output.get("type") != "websocket.send":
                continue
            text = output.get("text")
            if not isinstance(text, str):
                continue
            frame = decode_host_frame(text)
            if not isinstance(frame, HostStoreSecretFrame):
                continue
            arrivals.append(frame.kind)
            # Hold the FIRST write's reply until released, so if the lock were
            # missing the second frame would arrive while the first is pending.
            if not first_seen:
                first_seen = True
                await release_first.wait()
            await comm.send_input(
                {
                    "type": "websocket.receive",
                    "text": encode_host_frame(
                        HostStoreSecretResultFrame(
                            request_id=frame.request_id,
                            status="ok",
                            configured_harnesses={frame.harness: True},
                        )
                    ),
                }
            )

    drain_task = asyncio.create_task(_drain())
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = asyncio.create_task(
                client.post(
                    f"/v1/hosts/{_HOST_ID}/harnesses/codex/credential",
                    json={"kind": "key", "secret": "sk-1"},
                )
            )
            second = asyncio.create_task(
                client.post(
                    f"/v1/hosts/{_HOST_ID}/harnesses/codex/credential",
                    json={"kind": "gateway", "secret": "sk-2", "base_url": "https://gw/v1"},
                )
            )
            # Give both requests time to reach the route; only the first frame
            # should have been forwarded (the second is blocked on the lock).
            await asyncio.sleep(0.2)
            assert arrivals == ["key"], f"second write leaked past the lock: {arrivals}"
            release_first.set()
            r1, r2 = await asyncio.gather(first, second)
            assert r1.status_code == 200 and r2.status_code == 200
            # Both eventually processed, in order — no interleave.
            assert arrivals == ["key", "gateway"]
    finally:
        stop.set()
        release_first.set()
        try:
            await asyncio.wait_for(drain_task, timeout=1.0)
        except asyncio.TimeoutError:
            drain_task.cancel()


# ── Validation / gating ─────────────────────────────────


async def test_route_hidden_when_flag_off(
    cred_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the flag off the route is 404 — the feature is invisible."""
    monkeypatch.setenv("OMNIGENT_HARNESS_INSTALL_ENABLED", "0")
    app, _reg, _hs, _cs = cred_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/harnesses/claude/credential",
            json={"kind": "key", "secret": "x"},
        )
    assert resp.status_code == 404


# cursor/goose/kimi/hermes are never installable; opencode/qwen ARE installable
# but env-auth (not credential-configurable) — all must get a clean 400 (not a
# 502 from the host bouncing a forwarded frame).
@pytest.mark.parametrize("harness", ["cursor", "goose", "kimi", "hermes", "opencode", "qwen"])
async def test_rejects_non_ui_configurable_harness(
    cred_setup: tuple[
        FastAPI, HostRegistry, list[HostStoreSecretFrame], dict[str, dict[str, Any]]
    ],
    harness: str,
) -> None:
    """A harness outside the UI-auth allowlist is rejected with 400, no frame."""
    app, _reg, received, _replies = cred_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/harnesses/{harness}/credential",
            json={"kind": "key", "secret": "x"},
        )
    assert resp.status_code == 400
    assert received == []


async def test_rejects_unknown_kind(
    cred_setup: tuple[
        FastAPI, HostRegistry, list[HostStoreSecretFrame], dict[str, dict[str, Any]]
    ],
) -> None:
    """An unknown credential kind is rejected with 400 before any frame."""
    app, _reg, received, _replies = cred_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/harnesses/claude/credential",
            json={"kind": "bogus", "secret": "x"},
        )
    assert resp.status_code == 400
    assert received == []


async def test_host_side_failure_maps_to_502(
    cred_setup: tuple[
        FastAPI, HostRegistry, list[HostStoreSecretFrame], dict[str, dict[str, Any]]
    ],
) -> None:
    """A host-side write failure surfaces as 502 with the non-secret reason."""
    app, _reg, _received, replies = cred_setup
    replies["codex"] = {"status": "failed", "error": "a gateway requires a base_url"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/harnesses/codex/credential",
            json={"kind": "gateway", "secret": "x"},
        )
    assert resp.status_code == 502
    assert "a gateway requires a base_url" in resp.json()["detail"]


async def test_unknown_host_returns_404(
    cred_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """Configuring an unknown host returns 404."""
    app, _reg, _hs, _cs = cred_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/hosts/7139b7e896ef9478abca6480107d1677/harnesses/claude/credential",
            json={"kind": "key", "secret": "x"},
        )
    assert resp.status_code == 404


async def test_offline_host_returns_409(
    cred_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """A registered-but-offline host returns 409 (no live tunnel to forward on)."""
    app, _reg, host_store, _cs = cred_app
    host_store.upsert_on_connect(host_id=_HOST_ID, name=_HOST_NAME, user_id="local")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/harnesses/claude/credential",
            json={"kind": "key", "secret": "x"},
        )
    assert resp.status_code == 409


async def test_non_owner_returns_403(
    cred_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """A host owned by another user returns 403 — not configurable by non-owners."""
    from typing import Any as _Any

    from omnigent.server.auth import AuthProvider

    _app, _reg, host_store, conv_store = cred_app

    class _Stub(AuthProvider):
        def get_user_id(self, request: _Any) -> str | None:
            return request.headers.get("X-Test-User")

    auth = _Stub()
    auth_app = FastAPI()
    registry = HostRegistry()
    auth_app.include_router(
        create_host_tunnel_router(registry, host_store, auth_provider=auth), prefix="/v1"
    )
    auth_app.include_router(
        create_hosts_router(registry, host_store, conv_store, auth_provider=auth), prefix="/v1"
    )
    host_store.upsert_on_connect(host_id=_HOST_ID, name=_HOST_NAME, user_id="alice@example.com")

    async with AsyncClient(
        transport=ASGITransport(app=auth_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/harnesses/claude/credential",
            json={"kind": "key", "secret": "x"},
            headers={"X-Test-User": "bob@example.com"},
        )
    assert resp.status_code == 403


# ── Adopt-detect ────────────────────────────────────────


async def test_detect_credentials_returns_non_secret_descriptors(
    cred_setup: tuple[
        FastAPI, HostRegistry, list[HostStoreSecretFrame], dict[str, dict[str, Any]]
    ],
) -> None:
    """The detect route returns the host's adoptable creds (non-secret)."""
    app, _reg, _received, _replies = cred_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/v1/hosts/{_HOST_ID}/credentials/detected")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "detected_credentials"
    assert body["credentials"] == [
        {"family": "anthropic", "source": "$ANTHROPIC_API_KEY", "env_var": "ANTHROPIC_API_KEY"}
    ]


async def test_detect_credentials_hidden_when_flag_off(
    cred_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the flag off the detect route is 404."""
    monkeypatch.setenv("OMNIGENT_HARNESS_INSTALL_ENABLED", "0")
    app, _reg, _hs, _cs = cred_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/v1/hosts/{_HOST_ID}/credentials/detected")
    assert resp.status_code == 404
