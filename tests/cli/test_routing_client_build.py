"""Tests for how the server bootstrap assembles both routing backends.

Companion to ``test_build_routing_client.py``, which covers the individual
builders. Here the question is the PAIR: a Databricks deployment with an ``llm:``
block gets the AI Gateway *and* the built-in judge, so an off-gateway harness
still routes, and ``routing_client`` stays the pair's primary.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from omnigent.cli import _build_routing_backends
from omnigent.server.smart_routing import (
    ExternalRoutingClient,
    LLMRoutingClient,
    RoutingSettings,
)

_LLM_BLOCK: dict[str, Any] = {"model": "databricks-claude-haiku-4-5"}


@pytest.fixture
def workspace() -> Any:
    """Resolve every Databricks profile to one workspace host."""
    with patch(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        return_value=MagicMock(host="https://ws.example.invalid"),
    ) as resolve:
        yield resolve


def _server_llm() -> Any:
    from omnigent.spec import parse_server_llm

    return parse_server_llm(_LLM_BLOCK)


@contextmanager
def _policy_client() -> Any:
    """Stub the policy-LLM plumbing so no real credential is resolved."""
    with (
        patch(
            "omnigent.runtime.policies.builder._resolve_server_llm_connection",
            return_value=None,
        ),
        patch(
            "omnigent.runtime.policies.builder._build_policy_llm_client",
            return_value=MagicMock(),
        ),
    ):
        yield


def test_a_databricks_deployment_gets_both_backends(workspace: Any) -> None:
    del workspace
    cfg = {"providers": {"ws": {"kind": "databricks", "profile": "ws", "default": True}}}
    with _policy_client():
        backends = _build_routing_backends(cfg, _server_llm(), RoutingSettings())
    assert isinstance(backends.external, ExternalRoutingClient)
    assert isinstance(backends.local, LLMRoutingClient)
    # The primary every legacy consumer reads is the external client.
    assert backends.any() is backends.external


def test_an_explicit_external_provider_plus_an_llm_block_gets_both() -> None:
    cfg = {
        "routing": {
            "provider": "external",
            "base_url": "https://host/ai-gateway/routing/v1",
            "router_name": "task_v1",
        }
    }
    with _policy_client():
        backends = _build_routing_backends(cfg, _server_llm(), RoutingSettings())
    assert isinstance(backends.external, ExternalRoutingClient)
    assert isinstance(backends.local, LLMRoutingClient)


def test_provider_none_configures_neither_backend(workspace: Any) -> None:
    del workspace
    cfg = {
        "routing": {"provider": "none"},
        "providers": {"ws": {"kind": "databricks", "profile": "ws", "default": True}},
    }
    with _policy_client():
        backends = _build_routing_backends(cfg, _server_llm(), RoutingSettings())
    assert (backends.external, backends.local) == (None, None)
    assert backends.any() is None


def test_an_llm_block_alone_configures_only_the_built_in_judge() -> None:
    """No Databricks provider and no ``routing:`` block means judge only."""
    with (
        patch("omnigent.cli._databricks_provider_profile", return_value=None),
        _policy_client(),
    ):
        backends = _build_routing_backends({}, _server_llm(), RoutingSettings())
    assert backends.external is None
    assert isinstance(backends.local, LLMRoutingClient)
    assert backends.any() is backends.local


def test_a_databricks_deployment_without_an_llm_block_gets_only_the_gateway(
    workspace: Any,
) -> None:
    del workspace
    cfg = {"providers": {"ws": {"kind": "databricks", "profile": "ws", "default": True}}}
    backends = _build_routing_backends(cfg, None, RoutingSettings())
    assert isinstance(backends.external, ExternalRoutingClient)
    assert backends.local is None


def test_an_unusable_config_leaves_routing_off() -> None:
    """A default (non-external, non-none) provider with no ``llm:`` block."""
    with patch("omnigent.cli._databricks_provider_profile", return_value=None):
        backends = _build_routing_backends(
            {"routing": {"provider": "judge"}}, None, RoutingSettings()
        )
    assert (backends.external, backends.local) == (None, None)
