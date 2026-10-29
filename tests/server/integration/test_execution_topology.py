"""The execution topology keeps native route identity and host ownership."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest
from httpx import ASGITransport, AsyncClient

from omnigent.server.host_accounts import HostAccountRegistry, execution_topology
from omnigent.server.routes import hosts
from tests.server.integration.test_hosts_api import host_api_app as host_api_app


def route_id(name):
    return uuid5(NAMESPACE_URL, f"execution-topology/{name}").hex


def registry_data():
    def account(account_id, harness, placements):
        return {
            "id": account_id,
            "label": account_id.title(),
            "provider": "openai" if harness == "codex-native" else "anthropic",
            "harness": harness,
            "placements": [
                {"host_id": route_id(h), "machine_id": m, "machine_label": m.title()}
                for h, m in placements
            ],
        }

    return {
        "version": 1,
        "accounts": [
            account("claude", "claude-native", [("shared", "desk"), ("private", "secret")]),
            account("codex", "codex-native", [("shared", "desk"), ("duplicate", "desk")]),
            account("other", "codex-native", [("private", "secret")]),
            account("offline", "claude-native", [("offline", "remote")]),
            account("sandbox", "claude-native", [("sandbox", "sandbox-machine")]),
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [None, "alice", "bob"])
async def test_topology_filters_by_host_owner(host_api_app, monkeypatch, owner):
    app, _registry, store, _conversations = host_api_app
    # Auth-disabled requests must use the reserved local owner, never every owner.
    monkeypatch.setattr(hosts, "require_user", lambda request, auth_provider: owner)
    for host_id, user_id, ready in [
        ("shared", "alice", {"claude-native": True, "codex-native": "needs-auth"}),
        ("duplicate", "alice", {"codex-native": True}),
        ("private", "bob", {"claude-native": True, "codex-native": True}),
        ("offline", "alice", {"claude-native": True}),
    ]:
        store.upsert_on_connect(route_id(host_id), host_id, user_id, configured_harnesses=ready)
    store.set_offline(route_id("offline"))
    original_list = store.list_hosts

    def list_hosts(user_id):
        return [
            *original_list(user_id),
            SimpleNamespace(host_id=route_id("sandbox"), sandbox_provider="test"),
        ]

    monkeypatch.setattr(store, "list_hosts", list_hosts)
    data = registry_data()
    registry = HostAccountRegistry.model_validate(data)
    app.state.account_registry = registry
    before = registry.model_dump()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/execution-topology")
    assert response.status_code == 200, response.text
    assert registry.model_dump() == before
    result = response.json()
    assert "sandbox" not in str(result)
    if owner is None:
        assert result == {"configured": True, "machines": [], "accounts": [], "bindings": []}
    elif owner == "alice":
        assert [m["id"] for m in result["machines"]] == ["desk", "remote"]
        assert [a["id"] for a in result["accounts"]] == ["claude", "codex", "offline"]
        assert [(b["host_id"], b["harness"]) for b in result["bindings"]] == [
            (route_id("shared"), "claude-native"),
            (route_id("shared"), "codex-native"),
            (route_id("duplicate"), "codex-native"),
            (route_id("offline"), "claude-native"),
        ]
        assert [b["available"] for b in result["bindings"]] == [True, False, True, False]
        assert [b["reason"] for b in result["bindings"]] == [
            None,
            "Harness is not ready",
            None,
            "Machine is offline",
        ]
        assert "secret" not in str(result) and "private" not in str(result)
    else:
        assert [m["id"] for m in result["machines"]] == ["secret"]
        assert {b["host_id"] for b in result["bindings"]} == {route_id("private")}
        assert [a["id"] for a in result["accounts"]] == ["claude", "other"]


@pytest.mark.asyncio
async def test_unconfigured_registry_lists_nothing(host_api_app):
    app, *_ = host_api_app
    app.state.account_registry = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        topology = await client.get("/v1/execution-topology")
    assert topology.json() == {"configured": False, "machines": [], "accounts": [], "bindings": []}


def test_normalization_does_not_mutate_filtered_view():
    view = {
        "configured": True,
        "accounts": [
            {
                "id": "account",
                "label": "Account",
                "provider": "openai",
                "harness": "codex-native",
                "placements": [
                    {
                        "host_id": "host-one",
                        "machine_id": "physical",
                        "machine_label": "Machine",
                        "available": False,
                        "reason": "Offline",
                    }
                ],
            }
        ],
    }
    before = deepcopy(view)
    result = execution_topology(view)
    assert view == before
    assert result["accounts"][0]["id"] == "account"
    assert result["bindings"][0]["host_id"] == "host-one"
    assert "placements" not in result["accounts"][0]
