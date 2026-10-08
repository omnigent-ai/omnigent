"""Regression: session usage must be priced by the ACTUAL provider, not the
harness DEFAULT provider.

Custom pricing is resolved through
``omnigent.llms.context_window.fetch_model_pricing_with_provider``. In the relay
accounting path (``_accumulate_session_usage``), the rate must come from the
provider the session was actually launched with (a named provider selected via
``executor.auth: {type: provider, name: ...}``), not the family DEFAULT.

When two providers serve the same family (anthropic) at different custom rates
and a session is bound to the *non-default* named one, its persisted
``total_cost_usd`` must reflect the NAMED provider's rate. This test binds a
session's agent to the expensive named provider, drives one real relay
accounting turn, and asserts the persisted cost is the NAMED rate, not the
cheaper DEFAULT rate.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.server.helpers import build_agent_bundle

# Two providers serving the SAME family (anthropic / claude-sdk) at DIFFERENT
# per-million custom rates: ``cheap-default`` is the family default,
# ``expensive-named`` a non-default provider a session may launch with.
_CHEAP_INPUT_PER_M = 1.0
_CHEAP_OUTPUT_PER_M = 2.0
_EXPENSIVE_INPUT_PER_M = 10.0
_EXPENSIVE_OUTPUT_PER_M = 20.0

_PROVIDER_CONFIG: dict[str, Any] = {
    "providers": {
        "cheap-default": {
            "kind": "key",
            "default": True,
            "anthropic": {
                "base_url": "https://cheap.example/v1",
                "api_key": "cheap-key",
                "pricing": {
                    "input_per_million": _CHEAP_INPUT_PER_M,
                    "output_per_million": _CHEAP_OUTPUT_PER_M,
                },
            },
        },
        "expensive-named": {
            "kind": "key",
            # NOT default — the named provider a session is launched with.
            "anthropic": {
                "base_url": "https://expensive.example/v1",
                "api_key": "expensive-key",
                "pricing": {
                    "input_per_million": _EXPENSIVE_INPUT_PER_M,
                    "output_per_million": _EXPENSIVE_OUTPUT_PER_M,
                },
            },
        },
    }
}

_INPUT_TOKENS = 1_000_000
_OUTPUT_TOKENS = 1_000_000
# What the session SHOULD cost: the named provider's rate.
_EXPECTED_NAMED_COST = (
    _INPUT_TOKENS * _EXPENSIVE_INPUT_PER_M / 1_000_000
    + _OUTPUT_TOKENS * _EXPENSIVE_OUTPUT_PER_M / 1_000_000
)
# What the buggy default-provider lookup produces (the wrong, cheaper cost).
_DEFAULT_PROVIDER_COST = (
    _INPUT_TOKENS * _CHEAP_INPUT_PER_M / 1_000_000
    + _OUTPUT_TOKENS * _CHEAP_OUTPUT_PER_M / 1_000_000
)


async def _create_agent_with_named_provider(client: httpx.AsyncClient) -> dict[str, Any]:
    """Create an agent bound to the ``expensive-named`` provider via
    ``executor.auth`` and return its metadata (with ``_session_id``)."""
    bundle = build_agent_bundle(
        name="named-provider-agent",
        executor={
            "config": {"harness": "claude-sdk"},
            "auth": {"type": "provider", "name": "expensive-named"},
        },
    )
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    assert resp.status_code == 201, f"agent create failed: {resp.status_code} {resp.text}"
    owning_session_id = resp.json()["session_id"]
    agent_resp = await client.get(f"/v1/sessions/{owning_session_id}/agent")
    assert agent_resp.status_code == 200, agent_resp.text
    agent = agent_resp.json()
    agent["_session_id"] = owning_session_id
    return agent


async def _create_session(client: httpx.AsyncClient, agent_id: str) -> dict[str, Any]:
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id})
    assert resp.status_code == 201, f"session create failed: {resp.status_code} {resp.text}"
    return resp.json()


def _read_session_usage(db_uri: str, session_id: str) -> dict[str, Any]:
    conv = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    return dict(conv.session_usage) if conv and conv.session_usage else {}


async def test_named_provider_session_priced_at_named_rate_not_default(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session launched with a named, non-default provider is priced at the
    named provider's custom rate — not the harness default provider's rate.

    Reproduces the relay accounting path resolves pricing via
    ``default_provider_for_harness()``, so it charges the DEFAULT provider's
    rate for a session actually served by a different named provider.
    """
    from omnigent.server.routes import sessions as sessions_routes

    # The catalog must not supply a competing price — provider-config custom
    # pricing is the only source under test.
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: None,
    )
    # Both the create-time provider resolution and the accounting path read the
    # provider config via load_config(); serve our two-provider config.
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.load_config",
        lambda: _PROVIDER_CONFIG,
    )

    agent = await _create_agent_with_named_provider(client)
    session = await _create_session(client, agent["id"])

    # Drive one real relay accounting turn (the ``response.completed`` usage the
    # relay loop feeds to _accumulate_session_usage).
    sessions_routes._accumulate_session_usage(
        {
            "usage": {
                "input_tokens": _INPUT_TOKENS,
                "output_tokens": _OUTPUT_TOKENS,
                "model": "claude-sonnet-4-6",
            }
        },
        session["id"],
        SqlAlchemyConversationStore(db_uri),
    )

    usage = _read_session_usage(db_uri, session["id"])
    cost = usage.get("total_cost_usd")
    assert cost is not None, f"turn was not priced at all: {usage}"

    # The bug: cost comes out at the default provider's rate. Guard against it
    # explicitly so a regression is legible, then assert the correct contract.
    assert cost != pytest.approx(_DEFAULT_PROVIDER_COST), (
        "session was priced at the DEFAULT provider's rate "
        f"({_DEFAULT_PROVIDER_COST}) instead of the named provider it was "
        f"launched with — Wrong provider used for pricing. usage={usage}"
    )
    assert cost == pytest.approx(_EXPECTED_NAMED_COST), (
        f"expected named-provider cost {_EXPECTED_NAMED_COST}, got {cost}. usage={usage}"
    )


def test_resolve_session_provider_entry_resolves_nested_sub_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NESTED sub-agent session prices on its own named provider.

    A sub-agent session's turns must price on the sub-agent's executor auth,
    resolved by a recursive spec search (as the policy builder does). A sub-agent
    nested below a direct child must still resolve its own provider rather than
    falling back to the root's — a shallow direct-children lookup would re-acquire
    the default-provider mispricing this change fixes for exactly those sessions.
    """
    from types import SimpleNamespace

    from omnigent.server.routes._sessions.orchestration import (
        _resolve_session_provider_entry,
    )
    from omnigent.spec.types import AgentSpec, ExecutorSpec, ProviderAuth

    leaf = AgentSpec(
        spec_version=1,
        name="leaf",
        executor=ExecutorSpec(
            auth=ProviderAuth(name="expensive-named"), config={"harness": "claude-sdk"}
        ),
    )
    mid = AgentSpec(
        spec_version=1,
        name="mid",
        executor=ExecutorSpec(config={"harness": "claude-sdk"}),
        sub_agents=[leaf],
    )
    root = AgentSpec(
        spec_version=1,
        name="root",
        executor=ExecutorSpec(
            auth=ProviderAuth(name="cheap-default"), config={"harness": "claude-sdk"}
        ),
        sub_agents=[mid],
    )

    agent = SimpleNamespace(
        id="a1", bundle_location="loc", session_id=None, operator_authored=True
    )
    monkeypatch.setattr(
        "omnigent.runtime.get_agent_store",
        lambda: SimpleNamespace(get=lambda _agent_id: agent),
    )
    monkeypatch.setattr(
        "omnigent.runtime.get_agent_cache",
        lambda: SimpleNamespace(load=lambda *a, **k: SimpleNamespace(spec=root)),
    )
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.load_config",
        lambda: _PROVIDER_CONFIG,
    )

    conv = SimpleNamespace(id="s1", agent_id="a1", sub_agent_name="leaf")
    entry = _resolve_session_provider_entry(conv)

    assert entry is not None, "nested sub-agent provider did not resolve"
    assert entry.name == "expensive-named", (
        f"nested sub-agent priced on {entry.name!r}, not its own provider — a "
        "shallow direct-children lookup fell back to the root's provider"
    )


def test_resolve_session_provider_entry_does_not_expand_user_bundle_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistent user bundle must not env-expand during pricing lookup.

    ``session_id is None`` alone does not make a bundle operator-authored: a
    user-uploaded agent can have no session yet still be tenant input. The
    resolver must gate env expansion on ``operator_authored`` so a tenant's
    ``${VAR}`` never captures a server secret into the cached spec.
    """
    from types import SimpleNamespace

    from omnigent.entities.agent import Agent
    from omnigent.server.routes._sessions.orchestration import (
        _resolve_session_provider_entry,
    )
    from omnigent.spec.types import AgentSpec, ExecutorSpec, ProviderAuth

    spec = AgentSpec(
        spec_version=1,
        name="user-agent",
        executor=ExecutorSpec(
            auth=ProviderAuth(name="expensive-named"), config={"harness": "claude-sdk"}
        ),
    )
    agent = Agent(
        id="a1", created_at=0, name="user-agent", bundle_location="loc", kind="user"
    )
    assert agent.session_id is None and agent.operator_authored is False

    captured: dict[str, Any] = {}

    def _fake_load(agent_id: str, bundle_location: str, *, expand_env: bool) -> Any:
        captured["expand_env"] = expand_env
        return SimpleNamespace(spec=spec)

    monkeypatch.setattr(
        "omnigent.runtime.get_agent_store",
        lambda: SimpleNamespace(get=lambda _agent_id: agent),
    )
    monkeypatch.setattr(
        "omnigent.runtime.get_agent_cache",
        lambda: SimpleNamespace(load=_fake_load),
    )
    monkeypatch.setattr(
        "omnigent.onboarding.provider_config.load_config",
        lambda: _PROVIDER_CONFIG,
    )

    conv = SimpleNamespace(id="s1", agent_id="a1", sub_agent_name=None)
    _resolve_session_provider_entry(conv)

    assert captured.get("expand_env") is False, (
        "pricing lookup expanded env for a non-operator-authored user bundle; "
        "a tenant ${VAR} could capture a server secret into the cached spec"
    )
