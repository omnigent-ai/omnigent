"""Tests for the server's routing-client builders.

Covers :func:`_build_external_routing_client` (the external
``routes:select`` provider) and :func:`_build_local_llm_routing_client`
(the built-in judge), including config validation that degrades to
``None`` (routing disabled) rather than raising.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from omnigent.cli import _build_external_routing_client, _build_local_llm_routing_client
from omnigent.server.smart_routing import ExternalRoutingClient, LLMRoutingClient


def test_external_builds_client() -> None:
    cfg = {
        "provider": "external",
        "base_url": "https://host/ai-gateway/routing/v1",
        "router_name": "task_v0",
    }
    client = _build_external_routing_client(cfg)
    assert isinstance(client, ExternalRoutingClient)
    assert client._url == "https://host/ai-gateway/routing/v1/routes:select"
    assert client._router_name == "task_v0"
    assert client._auth is None  # no profile -> unauthenticated
    assert client._model_prefixes == []  # no prefix -> catalog ids sent verbatim


def test_external_threads_model_prefix_scalar() -> None:
    """A single ``model_prefix`` string is wrapped into a one-element list."""
    cfg = {
        "provider": "external",
        "base_url": "https://host/v1",
        "router_name": "task_v0",
        "model_prefix": "databricks-",
    }
    client = _build_external_routing_client(cfg)
    assert isinstance(client, ExternalRoutingClient)
    assert client._model_prefixes == ["databricks-"]


def test_external_threads_model_prefix_list() -> None:
    """A list of prefixes threads through verbatim (blanks dropped)."""
    cfg = {
        "provider": "external",
        "base_url": "https://host/v1",
        "router_name": "task_v0",
        "model_prefix": ["databricks-", "system.ai.", ""],
    }
    client = _build_external_routing_client(cfg)
    assert isinstance(client, ExternalRoutingClient)
    assert client._model_prefixes == ["databricks-", "system.ai."]


def test_external_defers_profile_auth_to_per_call() -> None:
    """A ``profile`` is threaded through for lazy per-call token minting.

    The client mints a fresh bearer per request (OAuth refresh) rather
    than resolving a token at build time — a token captured once here
    would 401 after ~1h. So the builder neither resolves the profile
    eagerly nor sets a static ``_auth``; it stores the profile instead.
    """
    cfg = {
        "provider": "external",
        "base_url": "https://host/v1",
        "router_name": "task_v0",
        "profile": "staging",
    }
    with patch(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
    ) as resolve:
        client = _build_external_routing_client(cfg)
    resolve.assert_not_called()  # deferred to per-call, not resolved at build
    assert isinstance(client, ExternalRoutingClient)
    assert client._auth is None  # static auth unused; token minted per call
    assert client._databricks_profile == "staging"


def test_external_api_key_expands_env(monkeypatch: Any) -> None:
    """api_key is provider-agnostic and ${ENV}-expanded into a bearer header."""
    import httpx

    monkeypatch.setenv("OMNIGENT_TEST_ROUTING_KEY", "sekret")
    cfg = {
        "provider": "external",
        "base_url": "https://host/v1",
        "router_name": "task_v0",
        "api_key": "${OMNIGENT_TEST_ROUTING_KEY}",
    }
    client = _build_external_routing_client(cfg)
    assert isinstance(client, ExternalRoutingClient)
    # The bearer auth carries the expanded token.
    request = httpx.Request("POST", "https://host/v1/routes:select")
    flow = client._auth.auth_flow(request)
    assert next(flow).headers["Authorization"] == "Bearer sekret"


def test_external_api_key_wins_over_profile(monkeypatch: Any) -> None:
    """When both are set, api_key takes precedence; profile is not resolved."""
    monkeypatch.setenv("OMNIGENT_TEST_ROUTING_KEY", "sekret")
    cfg = {
        "provider": "external",
        "base_url": "https://host/v1",
        "router_name": "task_v0",
        "api_key": "${OMNIGENT_TEST_ROUTING_KEY}",
        "profile": "staging",
    }
    with patch(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
    ) as resolve:
        client = _build_external_routing_client(cfg)
    resolve.assert_not_called()
    assert isinstance(client, ExternalRoutingClient)
    assert client._auth is not None


def test_external_missing_required_fields_disables() -> None:
    """base_url and router_name are both required; missing either disables."""
    assert _build_external_routing_client({"provider": "external", "router_name": "x"}) is None
    assert (
        _build_external_routing_client({"provider": "external", "base_url": "https://h/v1"})
        is None
    )


def test_llm_without_server_llm_disables() -> None:
    assert _build_local_llm_routing_client(None) is None


def test_llm_builds_client() -> None:
    server_llm = object()
    with (
        patch(
            "omnigent.runtime.policies.builder._resolve_server_llm_connection",
            return_value={"base_url": "b", "api_key": "k"},
        ),
        patch(
            "omnigent.runtime.policies.builder._build_policy_llm_client",
            return_value=MagicMock(),
        ),
    ):
        client = _build_local_llm_routing_client(server_llm)
    assert isinstance(client, LLMRoutingClient)
