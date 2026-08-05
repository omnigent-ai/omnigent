"""Tests for :mod:`omnigent.onboarding.sandboxes.registry`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import click
import pytest

from omnigent.onboarding.sandboxes import get_launcher
from omnigent.onboarding.sandboxes.registry import (
    COMMUNITY_MODULE_PREFIX,
    SandboxProviderContribution,
    SandboxProviderMetadata,
    SandboxProviderPluginState,
    SandboxRegistryError,
    available_providers,
    get_provider_metadata,
    instantiate,
    plugin_state,
    reset_plugin_state_for_tests,
)


@dataclass
class _FakeEntryPoint:
    """Minimal stub for importlib.metadata.EntryPoint."""

    name: str
    value: Any

    def load(self) -> Any:
        return self.value


class _AcmeLauncher:
    """Dummy launcher used by the entrypoint test."""

    provider = "acme"


class _NoOpLauncher:
    """Dummy launcher for built-in registration tests."""

    provider = "noop"


NoOpSandboxLauncher = _NoOpLauncher
AcmeSandboxLauncher = _AcmeLauncher


def _set_entry_points(
    monkeypatch: pytest.MonkeyPatch,
    *eps: _FakeEntryPoint,
) -> None:
    """Patch the registry's internal entrypoint discovery."""
    monkeypatch.setattr(
        "omnigent.onboarding.sandboxes.registry._entry_points",
        lambda: eps,
    )


def test_plugin_state_loads_builtins() -> None:
    """The cached plugin state includes core Omnigent built-in providers."""
    reset_plugin_state_for_tests()
    state = plugin_state()
    assert isinstance(state, SandboxProviderPluginState)
    assert "modal" in state
    assert "kubernetes" in state


def test_plugin_state_is_cached() -> None:
    """Successive calls return the same state object."""
    reset_plugin_state_for_tests()
    first = plugin_state()
    second = plugin_state()
    assert first is second


def test_reset_clears_state() -> None:
    """reset_plugin_state_for_tests lets the next call rebuild the state."""
    reset_plugin_state_for_tests()
    first = plugin_state()
    reset_plugin_state_for_tests()
    second = plugin_state()
    assert first is not second


def test_available_providers_returns_builtins() -> None:
    """available_providers exposes built-in provider names."""
    reset_plugin_state_for_tests()
    names = available_providers()
    assert "modal" in names
    assert "kubernetes" in names


def test_get_provider_metadata_known_provider() -> None:
    """Metadata for a registered provider is available."""
    reset_plugin_state_for_tests()
    meta = get_provider_metadata("modal")
    assert meta is not None
    assert meta.name == "modal"
    assert "omnigent.onboarding.sandboxes.modal:ModalSandboxLauncher" in meta.launcher_class


def test_get_provider_metadata_unknown_returns_none() -> None:
    """Metadata for an unknown provider is ``None``."""
    reset_plugin_state_for_tests()
    assert get_provider_metadata("not-a-provider") is None


def test_instantiate_loads_built_in_provider() -> None:
    """instantiate imports and constructs a built-in launcher."""
    reset_plugin_state_for_tests()
    launcher = instantiate("modal")
    assert launcher.provider == "modal"


def test_instantiate_unknown_raises() -> None:
    """Instantiating an unknown provider raises SandboxRegistryError."""
    reset_plugin_state_for_tests()
    with pytest.raises(SandboxRegistryError, match="unknown sandbox provider"):
        instantiate("not-a-provider")


def _acme_contribution() -> SandboxProviderContribution:
    return SandboxProviderContribution(
        name="omnigent-acme",
        providers={
            "acme": SandboxProviderMetadata(
                name="acme",
                launcher_class=f"{COMMUNITY_MODULE_PREFIX}acme:AcmeSandboxLauncher",
            )
        },
    )


def test_community_entrypoints_are_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Community entrypoints in the right namespace are registered."""
    reset_plugin_state_for_tests()
    _set_entry_points(
        monkeypatch,
        _FakeEntryPoint(name="acme", value=_acme_contribution),
    )
    state = plugin_state()

    assert "acme" in state
    meta = state.get("acme")
    assert meta is not None
    assert meta.launcher_class == f"{COMMUNITY_MODULE_PREFIX}acme:AcmeSandboxLauncher"


def test_broken_entrypoint_is_recorded_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken community plugin is captured in load_errors."""
    reset_plugin_state_for_tests()

    def _broken() -> SandboxProviderContribution:
        raise TypeError("bad")

    _set_entry_points(
        monkeypatch,
        _FakeEntryPoint(name="bad", value=_broken),
    )
    state = plugin_state()

    assert "bad" not in state.providers
    assert "bad" in state.load_errors


def test_entrypoint_must_return_contribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entrypoint returning the wrong type is recorded as an error."""
    reset_plugin_state_for_tests()
    _set_entry_points(
        monkeypatch,
        _FakeEntryPoint(name="wrong", value=lambda: "string"),
    )
    state = plugin_state()

    assert "wrong" not in state.providers
    assert "wrong" in state.load_errors
    assert "expected SandboxProviderContribution" in state.load_errors["wrong"]


def test_validation_rejects_community_module_outside_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Community providers must live under the community namespace."""
    reset_plugin_state_for_tests()
    contribution = SandboxProviderContribution(
        name="omnigent-external",
        providers={
            "external": SandboxProviderMetadata(
                name="external",
                launcher_class="external_provider.sandbox:ExternalLauncher",
            )
        },
    )
    _set_entry_points(
        monkeypatch,
        _FakeEntryPoint(name="external", value=lambda: contribution),
    )
    state = plugin_state()

    assert "external" not in state.providers
    assert "external" in state.load_errors
    assert COMMUNITY_MODULE_PREFIX in state.load_errors["external"]


def test_validation_rejects_name_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A community plugin cannot override an existing provider name."""
    reset_plugin_state_for_tests()
    contribution = SandboxProviderContribution(
        name="modal-duplicate",
        providers={
            "modal": SandboxProviderMetadata(
                name="modal",
                launcher_class=f"{COMMUNITY_MODULE_PREFIX}modal:ModalSandboxLauncher",
            )
        },
    )
    _set_entry_points(
        monkeypatch,
        _FakeEntryPoint(name="modal-duplicate", value=lambda: contribution),
    )
    state = plugin_state()

    assert "modal" in state.providers  # original built-in still present
    assert "modal-duplicate" in state.load_errors
    assert "override existing provider" in state.load_errors["modal-duplicate"]


def test_validation_rejects_name_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider's metadata name must match its provider-key."""
    reset_plugin_state_for_tests()
    contribution = SandboxProviderContribution(
        name="mismatch",
        providers={
            "foo": SandboxProviderMetadata(
                name="bar",
                launcher_class=f"{COMMUNITY_MODULE_PREFIX}foo:FooLauncher",
            )
        },
    )
    _set_entry_points(
        monkeypatch,
        _FakeEntryPoint(name="mismatch", value=lambda: contribution),
    )
    state = plugin_state()

    assert "foo" not in state.providers
    assert "mismatch" in state.load_errors
    assert "does not match metadata name" in state.load_errors["mismatch"]


def test_get_launcher_uses_registry() -> None:
    """The public get_launcher resolves providers through the merged state."""
    reset_plugin_state_for_tests()
    launcher = get_launcher("modal")
    assert launcher.provider == "modal"


def test_get_launcher_unknown_raises_click_exception() -> None:
    """An unknown provider still surfaces as a click.ClickException."""
    reset_plugin_state_for_tests()
    with pytest.raises(
        click.ClickException,
        match="Unknown or unavailable sandbox provider",
    ):
        get_launcher("definitely-not-real")


def test_instantiate_rejects_non_launcher_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A contribution without the host-launch contract is rejected."""
    reset_plugin_state_for_tests()
    contribution = SandboxProviderContribution(
        name="not-a-launcher",
        providers={
            "not-a-launcher": SandboxProviderMetadata(
                name="not-a-launcher",
                launcher_class=f"{COMMUNITY_MODULE_PREFIX}fake:FakeLauncher",
            )
        },
    )
    _set_entry_points(
        monkeypatch,
        _FakeEntryPoint(name="not-a-launcher", value=lambda: contribution),
    )

    class _NotALauncher:
        pass

    monkeypatch.setattr(
        "omnigent.onboarding.sandboxes.registry._load_object",
        lambda _: _NotALauncher,
    )

    with pytest.raises(SandboxRegistryError, match="not a SandboxHostLauncher subclass"):
        instantiate("not-a-launcher")
