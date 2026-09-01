"""Non-secret provider inventory for host and UI status surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from omnigent.env_credentials import (
    expand_envvars_with_omnigent_prefix,
    getenv_nonempty_with_omnigent_prefix,
)
from omnigent.errors import OmnigentError
from omnigent.harness_availability import (
    HARNESS_BINARY_MISSING,
    HARNESS_NEEDS_AUTH,
    HARNESS_VERSION_TOO_LOW,
    HarnessAvailability,
)
from omnigent.json_types import JsonObject
from omnigent.onboarding.ambient import DetectedProvider, detect_providers_noninteractive
from omnigent.onboarding.detected import effective_config_with_detected
from omnigent.onboarding.provider_config import (
    BEDROCK_KIND,
    CLI_CONFIG_KIND,
    DATABRICKS_KIND,
    GATEWAY_KIND,
    KEY_KIND,
    LOCAL_KIND,
    SUBSCRIPTION_KIND,
    FamilyConfig,
    ProviderEntry,
    default_provider_for_harness,
    load_config,
    load_providers,
    provider_cli_home,
    provider_families,
)
from omnigent.spec.parser import check_unresolved_env_vars


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


class ConnectionState(str, Enum):
    """How usable a provider is based on silent, local evidence."""

    CONNECTED = "connected"
    AUTHENTICATION_REQUIRED = "authentication_required"
    MISCONFIGURED = "misconfigured"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderConnection:
    """A settled local connection state and its non-secret explanation."""

    state: ConnectionState
    detail: str


def _connection(state: ConnectionState, detail: str) -> ProviderConnection:
    return ProviderConnection(state=state, detail=detail)


_CLI_READINESS_HARNESS: dict[str, str] = {
    "claude": "claude-native",
    "codex": "codex-native",
    "pi": "pi",
}


def _selected_codex_home_connection(provider: ProviderEntry) -> ProviderConnection | None:
    """Return conservative readiness for a provider-selected Codex home."""
    if provider.cli != "codex" or provider.cli_home is None:
        return None
    try:
        codex_home = provider_cli_home(provider)
    except OmnigentError:
        return _connection(
            ConnectionState.MISCONFIGURED,
            "The selected Codex home does not resolve to an absolute local path.",
        )
    if codex_home is None:
        return None
    if provider.kind == SUBSCRIPTION_KIND:
        from omnigent.onboarding.ambient import codex_auth_has_credential

        if codex_auth_has_credential(codex_home / "auth.json"):
            return _connection(
                ConnectionState.CONNECTED,
                "A usable credential exists in the selected Codex home; "
                "the vendor was not contacted.",
            )
    return _connection(
        ConnectionState.UNKNOWN,
        "The selected Codex home is configured, but status reads cannot prove "
        "this profile without opening a protected credential store.",
    )


def _cli_connection(
    provider: ProviderEntry,
    readiness: Mapping[str, HarnessAvailability] | None,
    *,
    provider_detected: bool,
) -> ProviderConnection:
    """Describe a CLI provider without running the CLI's auth command.

    Positive harness readiness is deliberately not treated as provider proof:
    the harness map is aggregate and may be green because a different key or
    gateway can launch the same harness. A safe ambient detection is
    provider-specific, while cached negative binary/auth states remain useful.
    """
    cli = provider.cli
    if cli is None:
        return _connection(
            ConnectionState.UNKNOWN,
            "This provider does not name the CLI that carries its credential.",
        )
    availability = (
        readiness.get(_CLI_READINESS_HARNESS.get(cli, cli)) if readiness is not None else None
    )
    if availability == HARNESS_VERSION_TOO_LOW:
        return _connection(
            ConnectionState.UNAVAILABLE,
            f"The installed {cli} CLI is older than the version this harness requires.",
        )
    if availability is False or availability == HARNESS_BINARY_MISSING:
        return _connection(
            ConnectionState.UNAVAILABLE,
            f"The {cli} CLI is not installed on this host.",
        )
    selected_home_connection = _selected_codex_home_connection(provider)
    if selected_home_connection is not None:
        return selected_home_connection
    if provider_detected:
        return _connection(
            ConnectionState.CONNECTED,
            f"A usable {cli} credential is configured locally; the vendor was not contacted.",
        )
    if provider.kind == SUBSCRIPTION_KIND and availability == HARNESS_NEEDS_AUTH:
        return _connection(
            ConnectionState.AUTHENTICATION_REQUIRED,
            f"The {cli} CLI is installed but has no locally detected credential.",
        )
    if availability is True:
        return _connection(
            ConnectionState.UNKNOWN,
            f"The {cli} harness is ready, but that readiness may come from another provider.",
        )
    return _connection(
        ConnectionState.UNKNOWN,
        f"This host has no provider-specific readiness evidence for the {cli} CLI.",
    )


def _reference_resolves(key: str, value: str) -> bool:
    """Return whether environment references expand, without secret-store I/O."""
    try:
        expanded = expand_envvars_with_omnigent_prefix(value)
        check_unresolved_env_vars(key, expanded)
    except OmnigentError:
        return False
    return True


def _family_connection(
    provider_name: str,
    family_name: str,
    family: FamilyConfig,
) -> ProviderConnection:
    """Describe one inline family using only config and environment evidence."""
    prefix = f"providers.{provider_name}.{family_name}"
    if not _reference_resolves(f"{prefix}.base_url", family.base_url):
        return _connection(
            ConnectionState.MISCONFIGURED,
            f"The {family_name} endpoint references an environment variable that is not set.",
        )
    if family.api_key is not None:
        if not _reference_resolves(f"{prefix}.api_key", family.api_key):
            return _connection(
                ConnectionState.AUTHENTICATION_REQUIRED,
                f"The {family_name} API key references an environment variable that is not set.",
            )
        return _connection(
            ConnectionState.CONNECTED,
            f"The {family_name} credential is configured locally; the vendor was not contacted.",
        )
    if family.api_key_ref is not None:
        if family.api_key_ref.startswith("env:"):
            env_var = family.api_key_ref[len("env:") :]
            if getenv_nonempty_with_omnigent_prefix(env_var) is None:
                return _connection(
                    ConnectionState.AUTHENTICATION_REQUIRED,
                    f"The {family_name} credential environment variable is not set.",
                )
            return _connection(
                ConnectionState.CONNECTED,
                f"The {family_name} credential is present in the environment; "
                "the vendor was not contacted.",
            )
        if family.api_key_ref.startswith("keychain:"):
            return _connection(
                ConnectionState.UNKNOWN,
                f"The {family_name} credential is stored in a protected secret store "
                "that status reads do not open.",
            )
        return _connection(
            ConnectionState.UNKNOWN,
            f"The {family_name} credential uses a reference status reads do not resolve.",
        )
    if family.auth_command is not None:
        return _connection(
            ConnectionState.UNKNOWN,
            f"The {family_name} credential comes from an auth command run only at launch.",
        )
    return _connection(
        ConnectionState.MISCONFIGURED,
        f"The {family_name} family declares no credential source.",
    )


_FAMILY_STATE_PRECEDENCE: tuple[ConnectionState, ...] = (
    ConnectionState.MISCONFIGURED,
    ConnectionState.AUTHENTICATION_REQUIRED,
    ConnectionState.UNKNOWN,
    ConnectionState.CONNECTED,
)


def _families_connection(provider: ProviderEntry) -> ProviderConnection:
    results = [
        _family_connection(provider.name, family, provider.families[family])
        for family in sorted(provider.families)
    ]
    if not results:
        return _connection(ConnectionState.MISCONFIGURED, "This provider serves no model family.")
    for state in _FAMILY_STATE_PRECEDENCE:
        for result in results:
            if result.state is state:
                return result
    return results[0]


def _databricks_connection(provider: ProviderEntry) -> ProviderConnection:
    """Check profile declarations and package presence without authenticating."""
    from omnigent.onboarding.databricks_config import (
        databricks_sdk_installed,
        list_databricks_profiles,
    )

    profile = provider.profile
    if profile is None:
        return _connection(ConnectionState.MISCONFIGURED, "This provider names no profile.")
    if profile not in list_databricks_profiles():
        return _connection(
            ConnectionState.AUTHENTICATION_REQUIRED,
            "The configured Databricks profile is not declared on this host.",
        )
    if not databricks_sdk_installed():
        return _connection(
            ConnectionState.UNAVAILABLE,
            "The databricks extra is not installed on this host.",
        )
    return _connection(
        ConnectionState.UNKNOWN,
        "The Databricks profile and SDK are present, but status reads do not authenticate it.",
    )


def provider_connection_state(
    provider: ProviderEntry,
    *,
    harness_readiness: Mapping[str, HarnessAvailability] | None = None,
    provider_detected: bool = False,
) -> ProviderConnection:
    """Return a provider-specific state without reading a secret or running a CLI."""
    try:
        if provider.kind in (SUBSCRIPTION_KIND, CLI_CONFIG_KIND):
            return _cli_connection(
                provider,
                harness_readiness,
                provider_detected=provider_detected,
            )
        if provider.kind == DATABRICKS_KIND:
            return _databricks_connection(provider)
        if provider.kind in (KEY_KIND, GATEWAY_KIND, LOCAL_KIND):
            return _families_connection(provider)
        if provider.kind == BEDROCK_KIND:
            return _connection(
                ConnectionState.UNKNOWN,
                "Bedrock credentials resolve from the AWS credential chain only at launch.",
            )
    except Exception:
        return _connection(
            ConnectionState.UNKNOWN,
            "This provider's local status could not be checked without authenticating.",
        )
    return _connection(
        ConnectionState.UNKNOWN,
        f"Omnigent cannot check a {provider.kind} provider without authenticating.",
    )


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
    connection_state: ConnectionState
    connection_detail: str
    default_for_harnesses: tuple[str, ...] = ()

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
            "connection_state": self.connection_state.value,
            "connection_detail": self.connection_detail,
            "default_for_harnesses": list(self.default_for_harnesses),
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


# Harnesses a pre-session picker asks about. Resolving each one host-side keeps
# the harness→provider rule in the one function that owns it, instead of
# re-deriving it from families in the web app.
_PICKER_HARNESSES: tuple[str, ...] = (
    "claude-native",
    "claude-sdk",
    "codex-native",
    "codex",
    "openai-agents",
    "pi-native",
    "pi",
)


def _default_harnesses_by_provider(config: dict[str, object]) -> dict[str, tuple[str, ...]]:
    """Map each provider name to the harnesses it would serve by default.

    Uses :func:`default_provider_for_harness`, the same resolver a launch uses,
    so a status surface can name the provider that will actually run a session
    rather than guessing from families.

    :param config: The effective config, ambient detections included.
    :returns: Provider name → harness ids, e.g. ``{"codex": ("codex-native",)}``.
    """
    raw_providers = config.get("providers")
    if not isinstance(raw_providers, dict):
        return {}

    # The canonical resolver parses the whole provider table. Give it only the
    # individually valid rows so one unrelated typo cannot hide every valid
    # harness mapping from this read-only status surface.
    valid_providers: dict[str, object] = {}
    for raw_name, raw_provider in raw_providers.items():
        name = str(raw_name)
        try:
            parsed = load_providers({"providers": {name: raw_provider}})
        except Exception:
            continue
        if name in parsed:
            valid_providers[name] = raw_provider

    resolver_config = dict(config)
    resolver_config["providers"] = valid_providers
    by_provider: dict[str, list[str]] = {}
    for harness in _PICKER_HARNESSES:
        try:
            provider = default_provider_for_harness(resolver_config, harness)
        except Exception:  # a malformed default must not erase the whole map
            continue
        if provider is not None:
            by_provider.setdefault(provider.name, []).append(harness)
    return {name: tuple(harnesses) for name, harnesses in by_provider.items()}


def build_provider_inventory(
    config: dict[str, object] | None = None,
    *,
    detected: list[DetectedProvider] | None = None,
    harness_readiness: Mapping[str, HarnessAvailability] | None = None,
) -> list[ProviderInventoryEntry]:
    """Return providers and silent local status without resolving secrets."""
    if config is None:
        config = dict(load_config())
    if detected is None:
        detected = detect_providers_noninteractive()

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

    default_harnesses = _default_harnesses_by_provider(effective)
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
                    connection_state=ConnectionState.MISCONFIGURED,
                    connection_detail=(
                        "This provider's configuration could not be parsed on this host."
                    ),
                )
            )
            continue
        provider_detected = any(
            item.kind == provider.kind
            and (
                item.name == provider.name
                or (
                    provider.kind == SUBSCRIPTION_KIND
                    and provider.cli is not None
                    and item.name == provider.cli
                )
                or (
                    provider.kind == CLI_CONFIG_KIND
                    and provider.model_provider is not None
                    and item.model_provider == provider.model_provider
                )
            )
            for item in detected
        )
        connection = provider_connection_state(
            provider,
            harness_readiness=harness_readiness,
            provider_detected=provider_detected,
        )
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
                connection_state=connection.state,
                connection_detail=connection.detail,
                default_for_harnesses=default_harnesses.get(provider.name, ()),
            )
        )
    return inventory


__all__ = [
    "CapabilitySupport",
    "ConnectionState",
    "ProviderCapabilities",
    "ProviderConnection",
    "ProviderInventoryEntry",
    "build_provider_inventory",
    "provider_capabilities",
    "provider_connection_state",
]
