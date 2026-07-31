"""End-to-end registry test for the echo-native EXAMPLE plugin (PR 2.4).

Proves the reference community native harness in
``examples/omnigent-echo-native`` loads through the real registry: its
``get_contribution()`` is accepted by the 2.1 validator, its harness/agent/
provider merge into the accessors, its hooks resolve to importable objects, and
it surfaces on the 2.2 ``/v1/harnesses`` native-agent catalog.

The example's implementation dir is added to ``sys.path`` here (the namespace
package ``omnigent.community.harness`` already extends onto it), so the test
runs without a separate ``pip install`` step — the same contract a real
installed plugin satisfies via its entry point.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import omnigent.harness_plugins as hp
from omnigent import native_dispatch

_EXAMPLE_ROOT = Path(__file__).resolve().parents[1] / "examples" / "omnigent-echo-native"


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


@pytest.fixture
def echo_plugin(monkeypatch: pytest.MonkeyPatch) -> Callable[[], hp.HarnessContribution]:
    """Put the example impl dir on sys.path and install its entry point."""
    monkeypatch.syspath_prepend(str(_EXAMPLE_ROOT))
    # The namespace package caches __path__ at import; refresh so it discovers
    # the example dir we just prepended.
    import omnigent.community.harness as harness_ns

    importlib.reload(harness_ns)
    from omnigent.community.harness.echo.plugin import get_contribution

    monkeypatch.setattr(
        hp.importlib.metadata,
        "entry_points",
        lambda: {hp.COMMUNITY_ENTRY_POINT_GROUP: (_EntryPoint("echo", get_contribution),)},
    )
    return get_contribution


def test_example_plugin_loads_without_error(
    echo_plugin: Callable[[], hp.HarnessContribution],
) -> None:
    """The example contribution passes the community validator (no load error)."""
    state = hp.plugin_state()
    assert "echo" not in state.load_errors, state.load_errors
    assert "echo-native" in hp.valid_harnesses()
    # The reversed alias folds to the canonical harness.
    assert hp.harness_aliases()["native-echo"] == "echo-native"


def test_example_plugin_registers_agent_and_provider(
    echo_plugin: Callable[[], hp.HarnessContribution],
) -> None:
    """The example's native agent + provider merge in, keyed by the same key."""
    assert any(a.key == "echo" for a in hp.native_agents())
    provider = hp.native_provider_for_key("echo")
    assert provider is not None
    assert provider.run_native.startswith("omnigent.community.harness.echo.")


def test_example_plugin_hooks_resolve(
    echo_plugin: Callable[[], hp.HarnessContribution],
) -> None:
    """Every declared provider hook resolves to an importable object.

    This is the payoff of the seam: core resolves the plugin's dotted hook
    paths with no per-harness ``import`` branch. A typo or renamed symbol in the
    example fails HERE, not at dispatch time.
    """
    provider = hp.native_provider_for_key("echo")
    assert provider is not None
    for hook in (
        "run_native",
        "auto_create_terminal",
        "spawn_env_builder",
        "materialize_agent_spec",
    ):
        resolved = native_dispatch.resolve_hook(provider, hook)
        assert callable(resolved), hook


def test_example_plugin_surfaces_on_native_agent_catalog(
    echo_plugin: Callable[[], hp.HarnessContribution],
) -> None:
    """The example appears on the 2.2 native-agent catalog with identity + caps."""
    rows = {r["key"]: r for r in hp.native_agent_catalog()}
    assert "echo" in rows
    echo = rows["echo"]
    assert echo["agent_name"] == "echo-native-ui"
    assert echo["harness"] == "echo-native"
    assert echo["wrapper_label"] == "echo-native-ui"
    assert echo["fork_history"] == "none"
    assert echo["capabilities"]["integration_mode"] == "native-tui"


def test_example_materialize_spec_produces_valid_bundle_spec(
    echo_plugin: Callable[[], hp.HarnessContribution],
    tmp_path: Path,
) -> None:
    """The materialize hook writes a loadable echo-native-ui spec.

    Mirrors what the server's seeding loop does with the hook, so the example
    agent would seed like any built-in native.
    """
    from omnigent.community.harness.echo.echo_native import materialize_echo_agent_spec

    spec_path = materialize_echo_agent_spec(tmp_path)
    assert spec_path.exists()
    import yaml

    raw = yaml.safe_load(spec_path.read_text())
    assert raw["name"] == "echo-native-ui"
    assert raw["executor"]["harness"] == "echo-native"


def test_example_spawn_env_builder_emits_marker(
    echo_plugin: Callable[[], hp.HarnessContribution],
) -> None:
    """The spawn-env hook is a real, side-effect-free mapping."""
    from omnigent.community.harness.echo.echo_native_bridge import build_echo_native_spawn_env

    env = build_echo_native_spawn_env(None)
    assert env["OMNIGENT_ECHO_NATIVE_EXAMPLE"] == "1"
