"""Non-secret provider inventory for host and UI status surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from omnigent.json_types import JsonObject
from omnigent.onboarding.ambient import DetectedProvider, detect_providers
from omnigent.onboarding.detected import effective_config_with_detected
from omnigent.onboarding.provider_config import (
    CLI_CONFIG_KIND,
    DATABRICKS_KIND,
    GATEWAY_KIND,
    KEY_KIND,
    LOCAL_KIND,
    SUBSCRIPTION_KIND,
    ProviderEntry,
    load_config,
    load_providers,
    provider_families,
)


class CapabilitySupport(str, Enum):
    """Whether Omnigent currently exposes a provider capability."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderCapabilities:
    """Provider-level features, kept separate from harness capabilities."""

    model_discovery: CapabilitySupport
    usage_status: CapabilitySupport
    multiple_profiles: CapabilitySupport
    interactive_cli: CapabilitySupport

    def as_dict(self) -> JsonObject:
        """Return a JSON-safe capability view."""
        return {
            "model_discovery": self.model_discovery.value,
            "usage_status": self.usage_status.value,
            "multiple_profiles": self.multiple_profiles.value,
            "interactive_cli": self.interactive_cli.value,
        }


@dataclass(frozen=True)
class ProviderInventoryEntry:
    """A configured or ambient provider without credential material."""

    provider_id: str
    display_name: str
    kind: str
    origin: str
    source: str
    configuration_state: str
    error: str | None
    families: tuple[str, ...]
    surfaces: tuple[str, ...]
    default_for: tuple[str, ...]
    default_models: dict[str, str]
    cli: str | None
    profile: str | None
    model_provider: str | None
    capabilities: ProviderCapabilities

    def as_dict(self) -> JsonObject:
        """Return the public API representation."""
        return {
            "id": self.provider_id,
            "display_name": self.display_name,
            "kind": self.kind,
            "origin": self.origin,
            "source": self.source,
            "configuration_state": self.configuration_state,
            "error": self.error,
            "families": list(self.families),
            "surfaces": list(self.surfaces),
            "default_for": list(self.default_for),
            "default_models": dict(self.default_models),
            "cli": self.cli,
            "profile": self.profile,
            "model_provider": self.model_provider,
            "capabilities": self.capabilities.as_dict(),
        }


_DISCOVERABLE_KINDS = frozenset({KEY_KIND, GATEWAY_KIND, LOCAL_KIND, DATABRICKS_KIND})


def _unknown_capabilities() -> ProviderCapabilities:
    return ProviderCapabilities(
        model_discovery=CapabilitySupport.UNKNOWN,
        usage_status=CapabilitySupport.UNKNOWN,
        multiple_profiles=CapabilitySupport.UNKNOWN,
        interactive_cli=CapabilitySupport.UNKNOWN,
    )


def provider_capabilities(provider: ProviderEntry) -> ProviderCapabilities:
    """Derive conservative capabilities from an existing provider entry."""
    known_cli_discovery = (
        provider.kind == SUBSCRIPTION_KIND and provider.cli in {"claude", "codex", "pi"}
    ) or (provider.kind == CLI_CONFIG_KIND and provider.cli == "codex")
    model_discovery = (
        CapabilitySupport.SUPPORTED
        if (provider.kind in _DISCOVERABLE_KINDS or known_cli_discovery)
        else CapabilitySupport.UNKNOWN
    )
    interactive_cli = (
        CapabilitySupport.SUPPORTED
        if provider.kind in {SUBSCRIPTION_KIND, CLI_CONFIG_KIND}
        else CapabilitySupport.UNSUPPORTED
    )
    if provider.kind == DATABRICKS_KIND:
        multiple_profiles = CapabilitySupport.SUPPORTED
    elif provider.kind == SUBSCRIPTION_KIND:
        multiple_profiles = CapabilitySupport.UNSUPPORTED
    else:
        multiple_profiles = CapabilitySupport.UNKNOWN
    return ProviderCapabilities(
        model_discovery=model_discovery,
        # Session token/cost accounting exists, but proactive provider quota
        # status is not implemented by any provider integration today.
        usage_status=CapabilitySupport.UNSUPPORTED,
        multiple_profiles=multiple_profiles,
        interactive_cli=interactive_cli,
    )


def build_provider_inventory(
    config: dict[str, object] | None = None,
    *,
    detected: list[DetectedProvider] | None = None,
) -> list[ProviderInventoryEntry]:
    """Return configured plus ambient providers without resolving secrets."""
    if config is None:
        config = dict(load_config())
    if detected is None:
        detected = detect_providers()

    explicit_raw = config.get("providers")
    explicit_names = set(explicit_raw) if isinstance(explicit_raw, dict) else set()
    detected_by_name = {item.name: item for item in detected}
    try:
        effective = effective_config_with_detected(config, detected)
    except Exception:  # one malformed explicit entry must not erase every row
        effective = config
    raw_providers = effective.get("providers")
    if not isinstance(raw_providers, dict):
        return []

    inventory: list[ProviderInventoryEntry] = []
    for raw_name, raw_provider in raw_providers.items():
        name = str(raw_name)
        ambient = detected_by_name.get(name)
        origin = "configured" if name in explicit_names else "detected"
        source = ambient.source if origin == "detected" and ambient is not None else "config"
        try:
            parsed = load_providers({"providers": {name: raw_provider}})
            provider = parsed.get(name)
        except Exception:
            provider = None
        if provider is None:
            inventory.append(
                ProviderInventoryEntry(
                    provider_id=name,
                    display_name=name,
                    kind=(
                        str(raw_provider.get("kind", "unknown"))
                        if isinstance(raw_provider, dict)
                        else "unknown"
                    ),
                    origin=origin,
                    source=source,
                    configuration_state="invalid",
                    error="Provider configuration is invalid. Reconfigure this provider.",
                    families=(),
                    surfaces=(),
                    default_for=(),
                    default_models={},
                    cli=None,
                    profile=None,
                    model_provider=None,
                    capabilities=_unknown_capabilities(),
                )
            )
            continue
        surfaces = tuple(sorted(provider_families(provider)))
        families = tuple(surface for surface in surfaces if surface != "pi")
        default_models = {
            family: model
            for family in families
            if (model := provider.family_default_model(family)) is not None
        }
        inventory.append(
            ProviderInventoryEntry(
                provider_id=provider.name,
                display_name=provider.display_name or provider.name,
                kind=provider.kind,
                origin=origin,
                source=source,
                configuration_state="valid",
                error=None,
                families=families,
                surfaces=surfaces,
                default_for=tuple(sorted(provider.default_families)),
                default_models=default_models,
                cli=provider.cli,
                profile=provider.profile,
                model_provider=provider.model_provider,
                capabilities=provider_capabilities(provider),
            )
        )
    return inventory


__all__ = [
    "CapabilitySupport",
    "ProviderCapabilities",
    "ProviderInventoryEntry",
    "build_provider_inventory",
    "provider_capabilities",
]
