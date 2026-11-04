"""``POST /v1/runtime-flags`` validation, authentication and provider plumbing."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from omnigent.host.identity import MANAGED_HOST_TOKEN_HEADER
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime_flags import MAX_RUNTIME_FLAGS_PER_REQUEST
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.routes import runtime_flags as runtime_flags_route
from omnigent.server.routes.runtime_flags import RuntimeFlagsRequest, create_runtime_flags_router
from omnigent.server.runtime_flags import (
    RuntimeFlagCaller,
    RuntimeFlagQuery,
    StaticRuntimeFlagProvider,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

_PATH = "/v1/runtime-flags"
_ALICE = {"X-Forwarded-Email": "alice@example.test"}


class _RecordingProvider:
    """Records what the route passes in and serves preset values."""

    def __init__(self, values: Mapping[str, object] | Exception) -> None:
        self.values = values
        self.calls: list[tuple[tuple[RuntimeFlagQuery, ...], RuntimeFlagCaller]] = []

    async def evaluate(
        self, queries: Sequence[RuntimeFlagQuery], caller: RuntimeFlagCaller
    ) -> Mapping[str, object]:
        self.calls.append((tuple(queries), caller))
        if isinstance(self.values, Exception):
            raise self.values
        return self.values


def _app(db_uri, tmp_path, provider, *, auth: bool = True):
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        SqlAlchemyAgentStore(db_uri),
        SqlAlchemyFileStore(db_uri),
        SqlAlchemyConversationStore(db_uri),
        artifacts,
        AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        auth_provider=UnifiedAuthProvider(source="header") if auth else None,
        runtime_flag_provider=provider,
    )


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _request(*flags: tuple[str, str, object]) -> dict[str, object]:
    return {"flags": [{"name": n, "type": t, "default": d} for n, t, d in flags]}


async def test_provider_values_are_filtered_by_query_and_type(db_uri, tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "0")
    provider = _RecordingProvider(
        {"a.enabled": True, "a.limit": "not an int", "a.ratio": 10**400, "a.unasked": True}
    )
    async with _client(_app(db_uri, tmp_path, provider)) as client:
        assert (
            await client.post(_PATH, json=_request(("a.enabled", "bool", False)))
        ).status_code == 401
        response = await client.post(
            _PATH,
            headers=_ALICE,
            json=_request(
                ("a.enabled", "bool", False), ("a.limit", "int", 3), ("a.ratio", "float", 0.5)
            ),
        )
        info = (await client.get("/v1/info")).json()

    assert response.status_code == 200
    assert response.json() == {"values": {"a.enabled": True}, "refresh_after_s": 60}
    assert provider.calls == [
        (
            (
                RuntimeFlagQuery("a.enabled", "bool", False),
                RuntimeFlagQuery("a.limit", "int", 3),
                RuntimeFlagQuery("a.ratio", "float", 0.5),
            ),
            RuntimeFlagCaller(user_id="alice@example.test"),
        )
    ]
    assert info["runtime_flags"] is True


@pytest.mark.parametrize(
    "body",
    [
        _request(("a.limit", "int", "3")),
        _request(("a.ratio", "float", "1.5")),
        _request(("a.groups", "string_list", ["a", 1])),
        _request(("bad name", "bool", False)),
        _request(("a.flag", "unknown", False)),
        _request(("a.flag", "bool", False), ("a.flag", "bool", False)),
        {"flags": [{"name": "a.flag", "type": "bool", "default": False, "extra": 1}]},
        {"flags": [], "unexpected": True},
    ],
)
def test_invalid_request_bodies(body):
    with pytest.raises(ValidationError):
        RuntimeFlagsRequest.model_validate(body)


def test_request_accepts_at_most_the_batch_limit():
    flags = [(f"a.flag{index}", "bool", False) for index in range(MAX_RUNTIME_FLAGS_PER_REQUEST)]
    assert len(RuntimeFlagsRequest.model_validate(_request(*flags)).flags) == 500
    with pytest.raises(ValidationError):
        RuntimeFlagsRequest.model_validate(_request(*flags, ("a.extra", "bool", False)))


def _router_app(provider, host_store=None) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_runtime_flags_router(provider, UnifiedAuthProvider(source="header"), host_store),
        prefix="/v1",
    )
    return app


@pytest.mark.parametrize(
    "flag", [("a.limit", "int", True), ("a.ratio", "float", 10**400)], ids=["bool", "huge"]
)
async def test_invalid_request_is_422_without_calling_provider(flag):
    provider = _RecordingProvider({})
    async with _client(_router_app(provider)) as client:
        response = await client.post(_PATH, headers=_ALICE, json=_request(flag))
    assert response.status_code == 422
    assert provider.calls == []


@dataclass
class _ManagedHost:
    user_id: str


class _FakeHostStore:
    """Resolves one (host_id, launch token) pair to its owner."""

    def resolve_launch_token(self, host_id: str, token: str) -> _ManagedHost | None:
        if (host_id, token) == ("host_a", "launch-token-a"):
            return _ManagedHost("owner@example.test")
        return None


@pytest.mark.parametrize(
    ("token", "host_id", "status"),
    [
        ("launch-token-a", "host_a", 200),
        ("wrong-token", "host_a", 401),
        ("launch-token-a", "host_b", 401),
        ("launch-token-a", None, 401),
    ],
)
async def test_managed_host_launch_token_resolves_owner(token, host_id, status):
    provider = _RecordingProvider({"a.enabled": True})
    app = _router_app(provider, _FakeHostStore())
    body = {**_request(("a.enabled", "bool", False)), "host_id": host_id}
    async with _client(app) as client:
        response = await client.post(_PATH, headers={MANAGED_HOST_TOKEN_HEADER: token}, json=body)
    assert response.status_code == status
    if status == 200:
        assert response.json()["values"] == {"a.enabled": True}
        assert provider.calls[0][1] == RuntimeFlagCaller(user_id="owner@example.test")
    else:
        assert provider.calls == []


async def test_provider_failure_is_503():
    provider = _RecordingProvider(RuntimeError("flag service down"))
    async with _client(_router_app(provider)) as client:
        response = await client.post(
            _PATH, headers=_ALICE, json=_request(("a.enabled", "bool", False))
        )
    assert response.status_code == 503


async def test_hanging_provider_is_503(monkeypatch):
    monkeypatch.setattr(runtime_flags_route, "PROVIDER_TIMEOUT_S", 0.01)
    cancelled = asyncio.Event()

    class _HangingProvider:
        async def evaluate(self, queries, caller):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    async with _client(_router_app(_HangingProvider())) as client:
        response = await client.post(
            _PATH, headers=_ALICE, json=_request(("a.enabled", "bool", False))
        )
    assert response.status_code == 503
    assert cancelled.is_set()


async def test_without_provider_serves_no_values(db_uri, tmp_path):
    async with _client(_app(db_uri, tmp_path, None, auth=False)) as client:
        response = await client.post(_PATH, json=_request(("a.enabled", "bool", False)))
        info = (await client.get("/v1/info")).json()
    assert response.json() == {"values": {}, "refresh_after_s": 60}
    assert info["runtime_flags"] is False


async def test_static_provider_serves_configured_values():
    provider = StaticRuntimeFlagProvider({"a.enabled": True, "a.other": 1})
    served = await provider.evaluate(
        [RuntimeFlagQuery("a.enabled", "bool", False)], RuntimeFlagCaller(user_id=None)
    )
    assert served == {"a.enabled": True}
