"""Tests for ``omnigent.cli._build_routing_client`` provider selection.

Verifies the ``routing:`` config block chooses between the built-in
``LLMRoutingClient`` and the ``ExternalRoutingClient``, and that a
malformed ``external`` config degrades to ``None`` (routing disabled)
rather than raising.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from omnigent.cli import _build_routing_client
from omnigent.server.smart_routing import ExternalRoutingClient, LLMRoutingClient


def test_external_provider_builds_external_client() -> None:
    cfg = {
        "provider": "external",
        "base_url": "https://host/ai-gateway/routing/v1",
        "router_name": "task_v0",
    }
    client = _build_routing_client(cfg, None)
    assert isinstance(client, ExternalRoutingClient)
    assert client._url == "https://host/ai-gateway/routing/v1/routes:select"
    assert client._router_name == "task_v0"
    assert client._auth is None  # no profile -> unauthenticated


def test_external_provider_resolves_profile_auth() -> None:
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
        client = _build_routing_client(cfg, None)
    resolve.assert_called_once_with("staging")
    assert isinstance(client, ExternalRoutingClient)
    assert client._auth is not None  # bearer auth built from the profile token


def test_external_provider_missing_base_url_disables() -> None:
    assert _build_routing_client({"provider": "external", "router_name": "x"}, None) is None


def test_external_provider_missing_router_name_disables() -> None:
    assert (
        _build_routing_client({"provider": "external", "base_url": "https://h/v1"}, None) is None
    )


def test_llm_provider_without_server_llm_disables() -> None:
    assert _build_routing_client({"provider": "llm"}, None) is None


def test_default_provider_is_llm() -> None:
    """No routing block → llm provider; with no server_llm that's None."""
    assert _build_routing_client(None, None) is None


def test_llm_provider_builds_llm_client() -> None:
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
        client = _build_routing_client(None, server_llm)
    assert isinstance(client, LLMRoutingClient)
