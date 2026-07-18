"""Tests for the server's routing-client builders.

Covers :func:`_build_external_routing_client` (the external
``routes:select`` provider) and :func:`_build_local_llm_routing_client`
(the built-in judge), including config validation that degrades to
``None`` (routing disabled) rather than raising.
"""

from __future__ import annotations

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


def test_external_resolves_profile_auth() -> None:
    cfg = {
        "provider": "external",
        "base_url": "https://host/v1",
        "router_name": "task_v0",
        "profile": "staging",
    }
    creds = MagicMock(token="dapi-XYZ", host="https://host")
    with patch(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        return_value=creds,
    ) as resolve:
        client = _build_external_routing_client(cfg)
    resolve.assert_called_once_with("staging")
    assert isinstance(client, ExternalRoutingClient)
    assert client._auth is not None  # bearer auth built from the profile token


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
