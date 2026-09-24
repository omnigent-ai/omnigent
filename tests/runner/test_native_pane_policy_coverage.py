"""Guards: every built-in native harness declares its pane-reaper policy.

The reaper lists panes from the harness registry, so a built-in harness with no
``pane_reap`` declaration would silently never be reaped (the drift that left
devin panes alive forever). ``_require_full_pane_reap_coverage`` fails the runner
build instead, and the constants the runner still keeps by hand are pinned to
the declarations here.
"""

from __future__ import annotations

import dataclasses

import pytest

from omnigent.harness_plugins import _BUILTIN_NATIVE_PROVIDERS, NativeHarnessProvider
from omnigent.runner.app import _FORWARDER_OWNED_IDLE_HARNESSES, _require_full_pane_reap_coverage
from omnigent.runner.resource_registry import _STATUS_EMITTING_TERMINAL_ROLES

_BY_KEY = {provider.key: provider for provider in _BUILTIN_NATIVE_PROVIDERS}


def _harness_role(key: str) -> str:
    from omnigent.harness_plugins import native_agents

    return next(agent.harness for agent in native_agents() if agent.key == key)


def _with(key: str, **changes: object) -> list[NativeHarnessProvider]:
    return [
        dataclasses.replace(p, **changes) if p.key == key else p  # type: ignore[arg-type]
        for p in _BUILTIN_NATIVE_PROVIDERS
    ]


def test_the_builtin_declarations_pass() -> None:
    _require_full_pane_reap_coverage()
    _require_full_pane_reap_coverage(_BUILTIN_NATIVE_PROVIDERS)


@pytest.mark.parametrize("key", sorted(_BY_KEY))
def test_a_builtin_without_a_reap_policy_fails_the_build(key: str) -> None:
    with pytest.raises(RuntimeError, match=f"{key}: pane_reap undeclared"):
        _require_full_pane_reap_coverage(_with(key, pane_reap=None))


@pytest.mark.parametrize("key", sorted(_BY_KEY))
def test_a_builtin_without_a_status_owner_fails_the_build(key: str) -> None:
    with pytest.raises(RuntimeError, match=f"{key}: status_owner undeclared"):
        _require_full_pane_reap_coverage(_with(key, status_owner=None))


def test_an_exemption_needs_a_reason() -> None:
    with pytest.raises(RuntimeError, match="kimi: exempt without a reason"):
        _require_full_pane_reap_coverage(_with("kimi", pane_reap_exempt_reason=None))
    with pytest.raises(RuntimeError, match="goose: exempt without a reason"):
        _require_full_pane_reap_coverage(_with("goose", pane_reap="exempt"))


@pytest.mark.parametrize("key", ["codex", "antigravity", "opencode", "devin"])
def test_forwarder_owned_status_needs_a_turn_probe(key: str) -> None:
    with pytest.raises(RuntimeError, match=f"{key}: forwarder-owned status needs"):
        _require_full_pane_reap_coverage(_with(key, pane_turn_probe=None))


def test_a_community_provider_is_not_forced_into_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent import harness_plugins

    community = NativeHarnessProvider(
        key="community-harness",
        run_native="omnigent.community.harness.x:run",
        auto_create_terminal="omnigent.community.harness.x:launch",
    )
    merged = (*harness_plugins.native_providers(), community)
    monkeypatch.setattr(harness_plugins, "native_providers", lambda: merged)
    # Undeclared, so never reaped, but it does not fail the runner build.
    _require_full_pane_reap_coverage()
    from omnigent.terminals.pane_reaper import native_pane_reap_rows

    assert "community-harness" not in native_pane_reap_rows()


@pytest.mark.parametrize("key", sorted(_BY_KEY))
def test_status_owner_matches_the_runners_status_constants(key: str) -> None:
    provider = _BY_KEY[key]
    role = _harness_role(key)
    assert (provider.status_owner in ("pane", "status_file")) == (
        role in _STATUS_EMITTING_TERMINAL_ROLES
    )
    assert (provider.status_owner == "forwarder") == (role in _FORWARDER_OWNED_IDLE_HARNESSES)


def test_only_kimi_is_exempt() -> None:
    exempt = {key for key, p in _BY_KEY.items() if p.pane_reap != "reap"}
    assert exempt == {"kimi"}


@pytest.mark.parametrize("key", sorted(_BY_KEY))
def test_each_harness_launches_its_pane_with_the_role_the_reaper_expects(key: str) -> None:
    # The reaper lists a pane only when its resource role equals the harness
    # id; the launch paths set the role from these constants.
    from omnigent.runner import resource_registry

    constant = getattr(resource_registry, f"{key.upper()}_NATIVE_TERMINAL_ROLE")
    assert constant == _harness_role(key)


def test_the_runner_build_runs_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent import harness_plugins
    from omnigent.runner.app import create_runner_app
    from tests.runner.helpers import NullServerClient

    monkeypatch.setattr(
        harness_plugins, "_BUILTIN_NATIVE_PROVIDERS", tuple(_with("devin", pane_reap=None))
    )
    with pytest.raises(RuntimeError, match="devin: pane_reap undeclared"):
        create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
