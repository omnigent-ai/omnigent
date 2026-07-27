"""Sandbox provider registry.

Core Omnigent contributes built-in sandbox providers directly. Optional
community packages contribute additional providers through the
``omnigent.sandbox_providers`` entry point group.

The registry mirrors the design of ``omnigent.harness_plugins``:
contributions are merged into a single plugin state, validation keeps
community provider code in the ``omnigent.community.sandbox`` namespace, and
broken plugins are recorded but never break core startup.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from omnigent.onboarding.sandboxes.base import SandboxLauncher

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

COMMUNITY_ENTRY_POINT_GROUP = "omnigent.sandbox_providers"
COMMUNITY_MODULE_PREFIX = "omnigent.community.sandbox."


class SandboxRegistryError(Exception):
    """A provider registry operation failed."""


@dataclass(frozen=True)
class SandboxProviderMetadata:
    """Static metadata for one sandbox provider.

    :param name: Provider short name, e.g. ``"modal"``.
    :param launcher_class: Fully-qualified launcher class as
        ``"module.path:ClassName"`` or ``"module.path.ClassName"``.
    :param config_model: Optional Pydantic model that validates the
        provider-specific ``sandbox.<name>`` config block.
    :param managed_token_ttl_s: Optional default managed launch-token
        lifetime in seconds.
    """

    name: str
    launcher_class: str
    config_model: type[object] | None = None
    managed_token_ttl_s: int | None = None


@dataclass(frozen=True)
class SandboxProviderContribution:
    """One package's contribution to the sandbox provider registry.

    :param name: Package / contributor name, e.g. ``"omnigent-acme"``. Used
        only for error messages.
    :param providers: Mapping of provider short name to metadata.
    """

    name: str
    providers: dict[str, SandboxProviderMetadata] = field(default_factory=dict)


@dataclass(frozen=True)
class SandboxProviderPluginState:
    """Merged provider registry plus non-fatal plugin load errors."""

    providers: dict[str, SandboxProviderMetadata]
    load_errors: dict[str, str]

    def __contains__(self, name: str) -> bool:
        return name in self.providers

    def get(self, name: str) -> SandboxProviderMetadata | None:
        return self.providers.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self.providers)


_state: SandboxProviderPluginState | None = None


def _entry_points() -> tuple[importlib.metadata.EntryPoint, ...]:
    """Return community sandbox-provider entrypoints, tolerating old stdlib."""
    discovered = importlib.metadata.entry_points()
    if hasattr(discovered, "select"):
        return tuple(discovered.select(group=COMMUNITY_ENTRY_POINT_GROUP))
    return tuple(discovered.get(COMMUNITY_ENTRY_POINT_GROUP, ()))


def _load_object(import_path: str) -> Any:
    """Load ``module:attribute`` or ``module.attribute``."""
    if ":" in import_path:
        module_name, attr = import_path.split(":", 1)
    else:
        module_name, attr = import_path.rsplit(".", 1)
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def _launcher_module(import_path: str) -> str:
    """Return the module part of a launcher class import path."""
    if ":" in import_path:
        return import_path.split(":", 1)[0]
    return import_path.rsplit(".", 1)[0]


def _config_model_module(config_model: type[object]) -> str | None:
    """Return the module of a config model class, or ``None``."""
    return getattr(config_model, "__module__", None)


def _builtin_contribution() -> SandboxProviderContribution:
    """The built-in sandbox-provider contribution from core Omnigent."""
    return SandboxProviderContribution(
        name="omnigent",
        providers={
            "lakebox": SandboxProviderMetadata(
                name="lakebox",
                launcher_class="omnigent.onboarding.sandboxes.lakebox:LakeboxLauncher",
            ),
            "modal": SandboxProviderMetadata(
                name="modal",
                launcher_class="omnigent.onboarding.sandboxes.modal:ModalSandboxLauncher",
                managed_token_ttl_s=25 * 3600,
            ),
            "daytona": SandboxProviderMetadata(
                name="daytona",
                launcher_class="omnigent.onboarding.sandboxes.daytona:DaytonaSandboxLauncher",
                managed_token_ttl_s=7 * 24 * 3600,
            ),
            "boxlite": SandboxProviderMetadata(
                name="boxlite",
                launcher_class="omnigent.onboarding.sandboxes.boxlite:BoxliteSandboxLauncher",
                managed_token_ttl_s=7 * 24 * 3600,
            ),
            "cwsandbox": SandboxProviderMetadata(
                name="cwsandbox",
                launcher_class="omnigent.onboarding.sandboxes.cwsandbox:CWSandboxLauncher",
            ),
            "islo": SandboxProviderMetadata(
                name="islo",
                launcher_class="omnigent.onboarding.sandboxes.islo:IsloSandboxLauncher",
                managed_token_ttl_s=7 * 24 * 3600,
            ),
            "e2b": SandboxProviderMetadata(
                name="e2b",
                launcher_class="omnigent.onboarding.sandboxes.e2b:E2BSandboxLauncher",
            ),
            "openshell": SandboxProviderMetadata(
                name="openshell",
                launcher_class="omnigent.onboarding.sandboxes.openshell:OpenShellSandboxLauncher",
                managed_token_ttl_s=7 * 24 * 3600,
            ),
            "kubernetes": SandboxProviderMetadata(
                name="kubernetes",
                launcher_class="omnigent.onboarding.sandboxes.kubernetes:KubernetesSandboxLauncher",
                managed_token_ttl_s=7 * 24 * 3600,
            ),
        },
    )


def _provider_module_available(import_path: str) -> bool:
    """Return whether the module holding a launcher class is importable."""
    module_name = _launcher_module(import_path)
    return importlib.util.find_spec(module_name) is not None


def _validate_community_contribution(
    contribution: SandboxProviderContribution,
    *,
    entry_point_name: str,
    existing: dict[str, SandboxProviderMetadata],
) -> str | None:
    """Validate a community contribution. Returns an error string or ``None``."""
    if not contribution.name:
        return "sandbox provider plugin must set name"

    for name, meta in contribution.providers.items():
        if not meta.name:
            return f"provider {name!r} must set name"
        if name != meta.name:
            return f"provider key {name!r} does not match metadata name {meta.name!r}"
        if not meta.launcher_class:
            return f"provider {name!r} must set launcher_class"
        if name in existing:
            return (
                f"sandbox provider plugin {entry_point_name!r} attempts to "
                f"override existing provider {name!r}"
            )

        module = _launcher_module(meta.launcher_class)
        if not module.startswith(COMMUNITY_MODULE_PREFIX):
            return (
                f"sandbox provider plugin {entry_point_name!r} provider "
                f"{name!r} uses module {module!r}; expected "
                f"{COMMUNITY_MODULE_PREFIX}*"
            )

        if meta.config_model is not None:
            cfg_module = _config_model_module(meta.config_model)
            if cfg_module is None or not cfg_module.startswith(COMMUNITY_MODULE_PREFIX):
                return (
                    f"sandbox provider plugin {entry_point_name!r} config model "
                    f"for provider {name!r} must live under "
                    f"{COMMUNITY_MODULE_PREFIX}*"
                )
    return None


def plugin_state() -> SandboxProviderPluginState:
    """Return the merged built-in + community sandbox provider registry."""
    global _state
    if _state is not None:
        return _state

    built_in = _builtin_contribution().providers
    providers: dict[str, SandboxProviderMetadata] = {}
    load_errors: dict[str, str] = {}

    for name, meta in built_in.items():
        if _provider_module_available(meta.launcher_class):
            providers[name] = meta

    for entry_point in _entry_points():
        try:
            loaded = entry_point.load()
            contribution = loaded() if callable(loaded) else loaded
            if not isinstance(contribution, SandboxProviderContribution):
                raise TypeError(
                    f"entry point returned {type(contribution).__name__}, "
                    "expected SandboxProviderContribution"
                )
            error = _validate_community_contribution(
                contribution,
                entry_point_name=entry_point.name,
                existing=providers,
            )
            if error is not None:
                raise ValueError(error)
            providers.update(contribution.providers)
        except Exception as exc:
            load_errors[entry_point.name] = str(exc)
            logger.warning(
                "could not load sandbox provider entry point %s (%s)",
                entry_point.name,
                exc,
                exc_info=True,
            )

    _state = SandboxProviderPluginState(providers=providers, load_errors=load_errors)
    return _state


def reset_plugin_state_for_tests() -> None:
    """Clear the cached plugin state."""
    global _state
    _state = None


def available_providers() -> tuple[str, ...]:
    """All sandbox providers whose launcher modules are available."""
    return plugin_state().names()


def get_provider_metadata(name: str) -> SandboxProviderMetadata | None:
    """Return metadata for a provider, or ``None`` if it is not registered."""
    return plugin_state().get(name)


def instantiate(
    name: str,
    *,
    workspace_host: str | None = None,
) -> object:
    """Import and instantiate a registered provider's launcher class.

    :param name: Registered provider name.
    :param workspace_host: Optional Databricks workspace host passed
        to the Lakebox launcher constructor.
    :returns: A fresh launcher instance.
    :raises SandboxRegistryError: If the provider is unknown or its
        class cannot be imported/instantiated.
    """
    meta = get_provider_metadata(name)
    if meta is None:
        raise SandboxRegistryError(f"unknown sandbox provider '{name}'")
    try:
        launcher_cls = _load_object(meta.launcher_class)
    except Exception as exc:
        raise SandboxRegistryError(
            f"could not load sandbox provider '{name}' from {meta.launcher_class!r}: {exc}"
        ) from exc
    if not isinstance(launcher_cls, type) or not issubclass(launcher_cls, SandboxLauncher):
        raise SandboxRegistryError(
            f"sandbox provider '{name}' resolved to {launcher_cls!r}, "
            f"which is not a SandboxLauncher subclass"
        )
    if name == "lakebox" and workspace_host is not None:
        return launcher_cls(workspace_host=workspace_host)
    return launcher_cls()
