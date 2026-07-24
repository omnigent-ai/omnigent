from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import omnigent.harness_plugins as hp
from omnigent.harness_install_spec import HarnessInstallSpec


class _EntryPoint:
    def __init__(self, name: str, loader: Callable[[], hp.HarnessContribution]) -> None:
        self.name = name
        self._loader = loader

    def load(self) -> Callable[[], hp.HarnessContribution]:
        return self._loader


@pytest.fixture(autouse=True)
def _reset_plugin_state() -> Iterator[None]:
    hp.reset_plugin_state_for_tests()
    yield
    hp.reset_plugin_state_for_tests()


def _install_entry_points(
    monkeypatch: pytest.MonkeyPatch,
    *entry_points: _EntryPoint,
) -> None:
    monkeypatch.setattr(
        hp.importlib.metadata,
        "entry_points",
        lambda: {hp.COMMUNITY_ENTRY_POINT_GROUP: entry_points},
    )


def test_community_harness_contribution_is_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    def _contribution() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-foo",
            valid_harnesses=frozenset({"foo"}),
            harness_modules={"foo": "omnigent.community.harness.foo.inner.foo_harness"},
            aliases={"foo-code": "foo"},
            model_env_keys={"foo": "HARNESS_FOO_MODEL"},
            spawn_env_builders={"foo": "omnigent.community.harness.foo.plugin:build_spawn_env"},
            harness_labels={"foo": "Foo"},
        )

    _install_entry_points(monkeypatch, _EntryPoint("foo", _contribution))

    assert "foo" in hp.valid_harnesses()
    assert hp.harness_aliases()["foo-code"] == "foo"
    assert hp.harness_modules()["foo-code"] == "omnigent.community.harness.foo.inner.foo_harness"
    assert hp.model_env_keys()["foo"] == "HARNESS_FOO_MODEL"
    assert (
        hp.spawn_env_builders()["foo"] == "omnigent.community.harness.foo.plugin:build_spawn_env"
    )
    foo_row = next((row for row in hp.harness_catalog() if row["id"] == "foo"), None)
    assert foo_row is not None
    assert foo_row["label"] == "Foo"
    # Every catalog row now also carries a setup_steps checklist.
    assert "setup_steps" in foo_row


def test_community_harness_rejects_non_community_import_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _contribution() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-foo",
            valid_harnesses=frozenset({"foo"}),
            harness_modules={"foo": "omnigent_foo.inner.foo_harness"},
        )

    _install_entry_points(monkeypatch, _EntryPoint("foo", _contribution))

    state = hp.plugin_state()
    assert "foo" in state.load_errors
    assert "foo" not in hp.valid_harnesses()


def test_community_harness_rejects_builtin_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    def _contribution() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-evil",
            valid_harnesses=frozenset({"claude-sdk"}),
            harness_modules={"claude-sdk": "omnigent.community.harness.evil.inner.evil_harness"},
        )

    _install_entry_points(monkeypatch, _EntryPoint("evil", _contribution))

    state = hp.plugin_state()
    assert "evil" in state.load_errors
    assert hp.harness_modules()["claude-sdk"] == "omnigent.inner.claude_sdk_harness"


def test_community_harness_rejects_alias_collision_with_builtin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _contribution() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-evil",
            valid_harnesses=frozenset({"foo"}),
            harness_modules={"foo": "omnigent.community.harness.evil.inner.foo_harness"},
            aliases={"claude-sdk": "foo"},
        )

    _install_entry_points(monkeypatch, _EntryPoint("evil", _contribution))

    state = hp.plugin_state()
    assert "evil" in state.load_errors
    assert "foo" not in hp.valid_harnesses()


def test_community_harness_rejects_community_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _first() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-foo",
            valid_harnesses=frozenset({"foo"}),
            harness_modules={"foo": "omnigent.community.harness.foo.inner.foo_harness"},
        )

    def _second() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-bar",
            valid_harnesses=frozenset({"foo"}),
            harness_modules={"foo": "omnigent.community.harness.bar.inner.foo_harness"},
        )

    _install_entry_points(
        monkeypatch,
        _EntryPoint("foo", _first),
        _EntryPoint("bar", _second),
    )

    state = hp.plugin_state()
    assert "bar" in state.load_errors
    assert hp.harness_modules()["foo"] == "omnigent.community.harness.foo.inner.foo_harness"


def test_community_harness_rejects_native_terminal_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _contribution() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-foo",
            valid_harnesses=frozenset({"foo-native"}),
            harness_modules={"foo-native": "omnigent.community.harness.foo.inner.foo_harness"},
            native_harnesses=frozenset({"foo-native"}),
        )

    _install_entry_points(monkeypatch, _EntryPoint("foo", _contribution))

    state = hp.plugin_state()
    assert "foo" in state.load_errors
    assert "foo-native" not in hp.valid_harnesses()


def test_community_harness_readiness_uses_install_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _contribution() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-foo",
            valid_harnesses=frozenset({"foo"}),
            harness_modules={"foo": "omnigent.community.harness.foo.inner.foo_harness"},
            aliases={"foo-code": "foo"},
            install_specs={
                "foo": HarnessInstallSpec(
                    "Foo",
                    "foo-cli",
                    package=None,
                    install_hint="install foo-cli",
                )
            },
            harness_install_keys={"foo": "foo", "foo-code": "foo"},
        )

    _install_entry_points(monkeypatch, _EntryPoint("foo", _contribution))

    from omnigent.onboarding import harness_readiness as readiness

    monkeypatch.setattr(readiness, "resolve_cli_binary", lambda _binary: None)
    assert readiness.harness_is_configured("foo") is False
    configured = readiness.configured_harness_map()
    assert configured["foo"] is False
    assert configured["foo-code"] is False

    monkeypatch.setattr(readiness, "resolve_cli_binary", lambda binary: f"/usr/bin/{binary}")
    assert readiness.harness_is_configured("foo") is True


def test_community_namespace_imports_external_harness_package(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "plugin"
    package_dir = package_root / "omnigent" / "community" / "harness" / "foo"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("VALUE = 'ok'\n", encoding="utf-8")

    monkeypatch.syspath_prepend(str(package_root))

    import omnigent.community as community
    import omnigent.community.harness as harnesses

    importlib.reload(community)
    importlib.reload(harnesses)
    sys.modules.pop("omnigent.community.harness.foo", None)

    module = importlib.import_module("omnigent.community.harness.foo")
    assert module.VALUE == "ok"


def test_builtin_background_title_generators_are_registered() -> None:
    generators = hp.background_title_generators()

    assert set(generators) >= {
        "claude-sdk",
        "claude-native",
        "codex",
        "codex-native",
    }
    assert generators["claude-sdk"].generator.endswith("sdk:generate_background_title")
    assert generators["codex"].generator == generators["claude-sdk"].generator
    assert generators["claude-native"].resolver_harness == "claude-sdk"
    assert generators["codex-native"].resolver_harness is None


def test_community_harness_can_register_background_title_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _contribution() -> hp.HarnessContribution:
        return hp.HarnessContribution(
            name="omnigent-foo",
            valid_harnesses=frozenset({"foo"}),
            harness_modules={"foo": "omnigent.community.harness.foo.inner.foo_harness"},
            background_title_generators={
                "foo": hp.BackgroundTitleGeneratorSpec(
                    "omnigent.community.harness.foo.background_titles:generate"
                )
            },
        )

    _install_entry_points(monkeypatch, _EntryPoint("foo", _contribution))

    generator = hp.background_title_generators()["foo"]
    assert generator.generator == ("omnigent.community.harness.foo.background_titles:generate")


def test_builtin_native_providers_cover_every_native_agent() -> None:
    """Every native agent has exactly one provider row keyed by the same key."""
    agent_keys = sorted(agent.key for agent in hp.native_agents())
    provider_keys = sorted(provider.key for provider in hp.native_providers())
    assert provider_keys == agent_keys
    # No duplicate provider keys.
    assert len(provider_keys) == len(set(provider_keys))


def test_native_provider_for_key_lookup() -> None:
    assert hp.native_provider_for_key("claude") is not None
    assert hp.native_provider_for_key("claude").key == "claude"
    assert hp.native_provider_for_key("does-not-exist") is None


def test_builtin_native_providers_have_required_hooks() -> None:
    """run_native and auto_create_terminal are mandatory on every built-in row."""
    for provider in hp.native_providers():
        assert provider.run_native, provider.key
        assert provider.auto_create_terminal, provider.key


def test_builtin_native_provider_paths_resolve() -> None:
    """Every populated built-in provider hook resolves to a real callable.

    This is the guard that keeps the provider rows honest: a typo'd import path
    or a renamed run_<x>_native symbol fails here rather than at dispatch time.
    """
    from omnigent import native_dispatch

    native_dispatch.reset_resolve_cache_for_tests()
    for provider in hp.native_providers():
        for hook in (
            "run_native",
            "auto_create_terminal",
            "materialize_agent_spec",
        ):
            resolved = native_dispatch.resolve_hook(provider, hook)
            assert callable(resolved), f"{provider.key}.{hook} did not resolve to a callable"
