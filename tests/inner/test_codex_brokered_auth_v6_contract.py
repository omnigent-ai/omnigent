"""Executable acceptance inventory for brokered Codex authentication."""

from __future__ import annotations

import pytest

from omnigent.inner.model_egress import (
    FrozenModelRoute,
    ProviderModelBinding,
    resolve_model_routes,
)

_HOST = "workspace.cloud.databricks.com"
_PREFIX = "/serving-endpoints/openai"
_RESPONSES = f"{_PREFIX}/responses"


def _binding(*routes: FrozenModelRoute) -> ProviderModelBinding:
    return ProviderModelBinding(
        endpoint=f"https://{_HOST}{_PREFIX}",
        maximum_routes=routes,
    )


def test_effective_scope_is_three_way_intersection() -> None:
    signed = FrozenModelRoute(method="POST", host=_HOST, path=_RESPONSES)
    unrelated = FrozenModelRoute(method="GET", host=_HOST, path="/api/2.0/clusters/list")

    effective = resolve_model_routes(
        provider=_binding(signed, unrelated),
        trusted_session_endpoint=f"https://{_HOST}{_PREFIX}",
        operator_model_egress=[f"POST {_HOST}{_RESPONSES}"],
    )

    assert effective == (signed,)


def test_broad_operator_grant_cannot_expand_provider_scope() -> None:
    signed = FrozenModelRoute(method="POST", host=_HOST, path=_RESPONSES)

    effective = resolve_model_routes(
        provider=_binding(signed),
        trusted_session_endpoint=f"https://{_HOST}{_PREFIX}",
        operator_model_egress=[f"* {_HOST}/**"],
    )

    assert effective == (signed,)
    assert not effective[0].matches(
        method="GET",
        host=_HOST,
        path="/api/2.0/secrets/list",
        query="",
    )


def test_same_host_different_endpoint_prefix_fails_closed() -> None:
    signed = FrozenModelRoute(method="POST", host=_HOST, path=_RESPONSES)

    with pytest.raises(ValueError, match="no effective model routes"):
        resolve_model_routes(
            provider=_binding(signed),
            trusted_session_endpoint=f"https://{_HOST}/api/2.0",
            operator_model_egress=[f"* {_HOST}/**"],
        )


@pytest.mark.parametrize("endpoint", [f"https://{_HOST}", f"https://{_HOST}/"])
def test_trusted_endpoint_requires_non_root_path(endpoint: str) -> None:
    signed = FrozenModelRoute(method="POST", host=_HOST, path=_RESPONSES)

    with pytest.raises(ValueError, match="non-root path"):
        resolve_model_routes(
            provider=_binding(signed),
            trusted_session_endpoint=endpoint,
            operator_model_egress=[f"* {_HOST}/**"],
        )


def test_query_is_denied_by_default() -> None:
    route = FrozenModelRoute(method="POST", host=_HOST, path=_RESPONSES)

    assert route.matches(method="POST", host=_HOST, path=_RESPONSES, query="")
    assert not route.matches(method="POST", host=_HOST, path=_RESPONSES, query="debug=true")
