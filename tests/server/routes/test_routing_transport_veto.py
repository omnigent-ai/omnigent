"""Unit coverage for the Smart Routing direct-vendor transport veto.

These tests pin :func:`_session_uses_direct_vendor_transport` and the gateway
prefix set directly, without the database-backed event route. The route's
persistence and decision-card output stay covered by the forwarder integration
suite (``tests/server/integration/test_routing_integration.py``); here the
transport decision itself is exercised against the shared launch-path
resolution so the subscription-precedence, provider-backed-key, and
default-key-provider branches are protected without repeating database setup.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.onboarding.configure_models import default_base_url_for_family
from omnigent.runtime.workflow import _load_global_auth
from omnigent.server.routes._sessions import orchestration
from omnigent.spec.types import AgentSpec, ApiKeyAuth, ExecutorSpec, ProviderAuth


def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, yaml_text: str) -> None:
    """Point the provider-config layer at an isolated ``config.yaml``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp dir to hold the config file.
    :param yaml_text: The config contents, e.g. a ``providers:`` block.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    # Ambient CLI/key detection would otherwise shadow the configured defaults.
    monkeypatch.setattr("omnigent.onboarding.detected.detect_providers", list)
    (tmp_path / "config.yaml").write_text(yaml_text)


def _claude_sdk_spec(**executor_kwargs: object) -> AgentSpec:
    """Build a ``claude-sdk`` worker spec with the given executor fields.

    :param executor_kwargs: Extra :class:`ExecutorSpec` fields, e.g.
        ``auth=ApiKeyAuth(api_key="k")``.
    :returns: The agent spec.
    """
    return AgentSpec(
        spec_version=1,
        name="offgateway-sdk",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "claude-sdk"},
            **executor_kwargs,  # type: ignore[arg-type]
        ),
    )


def test_direct_api_key_pane_is_direct_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An ``api_key`` with no ``base_url`` talks direct to the vendor."""
    _isolate_config(monkeypatch, tmp_path, "{}")
    spec = _claude_sdk_spec(auth=ApiKeyAuth(api_key="mock-key"))
    assert orchestration._session_uses_direct_vendor_transport(spec, "claude-sdk") is True


def test_api_key_with_base_url_is_gateway_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A spec ``api_key`` carrying a ``base_url`` reaches gateway-hosted models."""
    _isolate_config(monkeypatch, tmp_path, "{}")
    spec = _claude_sdk_spec(
        auth=ApiKeyAuth(api_key="mock-key", base_url="https://gw.example.invalid/v1")
    )
    assert orchestration._session_uses_direct_vendor_transport(spec, "claude-sdk") is False


def test_resolved_subscription_is_direct_despite_legacy_gateway_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A subscription default is direct even beside a legacy gateway api-key.

    The agent is ``claude-sdk`` with no spec auth; an explicit default Claude
    subscription provider is configured, and a legacy global ``auth:`` block
    also carries an api-key with a ``base_url``. Launch selects the
    subscription (which has no gateway transport) before considering the
    legacy credential, so the veto must treat the pane as direct and must not
    read the unrelated gateway api-key as an exemption.
    """
    _isolate_config(
        monkeypatch,
        tmp_path,
        "providers:\n"
        "  claude:\n"
        "    kind: subscription\n"
        "    cli: claude\n"
        "    default: true\n"
        "auth:\n"
        "  type: api_key\n"
        "  api_key: mock-key\n"
        "  base_url: https://gw.example.invalid/v1\n",
    )
    spec = _claude_sdk_spec()

    from omnigent.models.model_catalog import SUBSCRIPTION_KIND, resolve_model_provider

    assert resolve_model_provider(spec, "claude-sdk").kind == SUBSCRIPTION_KIND
    # The legacy gateway api-key is configured but launch never selects it, so
    # it must not exempt the subscription pane from the veto.
    assert getattr(_load_global_auth(), "base_url", None) == "https://gw.example.invalid/v1"
    assert orchestration._session_uses_direct_vendor_transport(spec, "claude-sdk") is True


def test_provider_backed_gateway_key_is_not_vetoed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``ProviderAuth`` key provider with a gateway endpoint is not direct.

    Launch routes this pane through the provider's configured family endpoint,
    so a gateway-hosted pick is runnable. The veto must read the selected
    provider's transport rather than the (``ProviderAuth``, not ``ApiKeyAuth``)
    spec auth, which carries no ``base_url`` of its own.
    """
    monkeypatch.setenv("GW_KEY", "sk-ant-test")
    _isolate_config(
        monkeypatch,
        tmp_path,
        "providers:\n"
        "  gw:\n"
        "    kind: key\n"
        "    anthropic:\n"
        "      base_url: https://gw.example.invalid/v1\n"
        "      api_key: $GW_KEY\n",
    )
    spec = _claude_sdk_spec(auth=ProviderAuth(name="gw"))

    from omnigent.models.model_catalog import KEY_KIND, resolve_model_provider

    provider = resolve_model_provider(spec, "claude-sdk")
    assert provider.kind == KEY_KIND
    assert provider.base_url == "https://gw.example.invalid/v1"
    assert provider.launch_base_url is None
    assert orchestration._session_uses_direct_vendor_transport(spec, "claude-sdk") is False


def test_explicit_default_key_provider_is_direct_despite_legacy_gateway_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit default key provider on the vendor endpoint is direct.

    Launch selects the configured default provider before the legacy global
    ``auth:`` block, so that block's gateway ``base_url`` plays no part in
    this pane's transport. The veto must judge the selected provider's own
    endpoint and decline the gateway pick.
    """
    vendor_default = default_base_url_for_family("anthropic")
    monkeypatch.setenv("DIRECT_KEY", "sk-ant-test")
    _isolate_config(
        monkeypatch,
        tmp_path,
        "providers:\n"
        "  anthropic-direct:\n"
        "    kind: key\n"
        "    default: true\n"
        "    anthropic:\n"
        f"      base_url: {vendor_default}\n"
        "      api_key: $DIRECT_KEY\n"
        "auth:\n"
        "  type: api_key\n"
        "  api_key: mock-key\n"
        "  base_url: https://gw.example.invalid/v1\n",
    )
    spec = _claude_sdk_spec()

    from omnigent.models.model_catalog import KEY_KIND, resolve_model_provider

    provider = resolve_model_provider(spec, "claude-sdk")
    assert provider.kind == KEY_KIND
    assert provider.detail == "provider 'anthropic-direct'"
    assert provider.base_url == vendor_default
    assert getattr(_load_global_auth(), "base_url", None) == "https://gw.example.invalid/v1"
    assert orchestration._session_uses_direct_vendor_transport(spec, "claude-sdk") is True


def test_gateway_model_prefixes_cover_every_catalog_spelling() -> None:
    """The veto's gateway prefixes track every Unity Catalog spelling.

    Model discovery lists the same gateway endpoints under more than one
    prefix: serving-endpoints spell them ``databricks-`` while Unity Catalog
    model services spell them ``system.ai.``. The off-gateway veto keys on
    ``_GATEWAY_MODEL_PREFIXES``, so a spelling it omits would let a gateway-only
    pick slip onto a direct-vendor pane. Pin the prefixes to the discovery
    module's own source of truth.
    """
    from omnigent.models.databricks_model_discovery import _CATALOG_SPELLINGS

    prefixes = orchestration._GATEWAY_MODEL_PREFIXES
    missing = [spelling for spelling in _CATALOG_SPELLINGS if spelling not in prefixes]
    assert not missing, (
        f"_GATEWAY_MODEL_PREFIXES omits catalog spelling(s) {missing!r}; a "
        "gateway-only pick in that spelling would slip past the veto."
    )
