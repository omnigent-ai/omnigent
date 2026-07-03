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
    assert {"id": "foo", "label": "Foo"} in hp.harness_catalog()


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

    monkeypatch.setattr(readiness.shutil, "which", lambda _binary: None)
    assert readiness.harness_is_configured("foo") is False
    configured = readiness.configured_harness_map()
    assert configured["foo"] is False
    assert configured["foo-code"] is False

    monkeypatch.setattr(readiness.shutil, "which", lambda binary: f"/usr/bin/{binary}")
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
