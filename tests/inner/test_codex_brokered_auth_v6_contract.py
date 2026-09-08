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


@pytest.mark.skip(reason="v6 slice 2: worker containment lifecycle")
def test_signer_exit_kills_codex_and_closes_relay() -> None:
    pass


@pytest.mark.skip(reason="v6 slice 2: worker containment lifecycle")
def test_runner_exit_kills_signer_codex_and_helpers() -> None:
    pass


@pytest.mark.skip(reason="v6 slice 3: signer HTTP client")
def test_signer_ignores_ambient_proxy_netrc_and_ca_environment() -> None:
    pass


@pytest.mark.skip(reason="v6 slice 3: signer request reconstruction")
def test_redirect_is_not_followed_with_bearer() -> None:
    pass


@pytest.mark.skip(reason="v6 slice 3: signer request reconstruction")
def test_connect_sni_and_inner_host_mismatch_is_rejected() -> None:
    pass


@pytest.mark.skip(reason="v6 slice 3: signer request reconstruction")
def test_protocol_upgrades_and_expect_continue_are_rejected() -> None:
    pass


@pytest.mark.skip(reason="v6 slice 3: signer authorization")
def test_only_exact_bearer_placeholder_is_accepted() -> None:
    pass


@pytest.mark.skip(reason="v6 slice 3: signer authorization")
def test_upstream_authorization_is_built_from_signer_cache() -> None:
    pass


@pytest.mark.skip(reason="v6 slice 3: signer authorization")
def test_placeholder_in_target_or_header_is_denied() -> None:
    pass


@pytest.mark.skip(reason="v6 slice 5: live jail probes")
def test_host_ipc_sockets_are_unreachable() -> None:
    pass


@pytest.mark.skip(reason="v6 slice 5: live jail probes")
def test_readable_socket_path_does_not_grant_connect() -> None:
    pass


@pytest.mark.skip(reason="v6 slice 3: signer request reconstruction")
def test_upstream_framing_headers_are_rebuilt() -> None:
    pass


@pytest.mark.skip(reason="v6 slice 3: signer authorization")
def test_keep_alive_reauthorizes_every_request() -> None:
    pass


@pytest.mark.skip(reason="v6 slice 2: worker containment lifecycle")
def test_provider_helper_is_in_teardown_group() -> None:
    pass


@pytest.mark.skip(reason="v6 slice 4: ucode authentication recovery")
def test_missing_login_returns_fixed_error_before_codex_launch() -> None:
    pass


@pytest.mark.skip(reason="v6 slice 4: ucode authentication recovery")
def test_recovery_message_excludes_helper_output_and_credentials() -> None:
    pass


@pytest.mark.skip(reason="v6 slice 4: ucode authentication recovery")
def test_retry_starts_fresh_signer_before_worker() -> None:
    pass
