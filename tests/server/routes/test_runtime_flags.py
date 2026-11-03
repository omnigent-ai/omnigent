"""``POST /v1/runtime-flags`` validation, authentication and provider plumbing."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import httpx
import pytest

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
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
    provider = _RecordingProvider({"a.enabled": True, "a.limit": "not an int", "a.unasked": True})
    async with _client(_app(db_uri, tmp_path, provider)) as client:
        assert (
            await client.post(_PATH, json=_request(("a.enabled", "bool", False)))
        ).status_code == 401
        response = await client.post(
            _PATH,
            headers=_ALICE,
            json=_request(("a.enabled", "bool", False), ("a.limit", "int", 3)),
        )
        info = (await client.get("/v1/info")).json()

    assert response.status_code == 200
    assert response.json() == {"values": {"a.enabled": True}, "refresh_after_s": 60}
    assert provider.calls == [
        (
            (
                RuntimeFlagQuery("a.enabled", "bool", False),
                RuntimeFlagQuery("a.limit", "int", 3),
            ),
            RuntimeFlagCaller(user_id="alice@example.test"),
        )
    ]
    assert info["runtime_flags"] is True


@pytest.mark.parametrize(
    "body",
    [
        _request(("a.limit", "int", True)),  # default of the wrong type
        _request(("a.limit", "int", "3")),
        _request(("bad name", "bool", False)),
        _request(("a.flag", "unknown", False)),
        _request(("a.flag", "bool", False), ("a.flag", "bool", False)),
        {"flags": [{"name": "a.flag", "type": "bool", "default": False, "extra": 1}]},
    ],
)
async def test_invalid_requests_are_rejected(db_uri, tmp_path, body):
    provider = _RecordingProvider({})
    async with _client(_app(db_uri, tmp_path, provider, auth=False)) as client:
        response = await client.post(_PATH, json=body)
    assert response.status_code == 422
    assert provider.calls == []


async def test_provider_failure_is_503(db_uri, tmp_path):
    provider = _RecordingProvider(RuntimeError("flag service down"))
    async with _client(_app(db_uri, tmp_path, provider, auth=False)) as client:
        response = await client.post(_PATH, json=_request(("a.enabled", "bool", False)))
    assert response.status_code == 503


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
