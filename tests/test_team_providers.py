"""The selected team plugin is explicit, unique, and fails closed."""

from __future__ import annotations

import importlib.metadata
from types import SimpleNamespace

import pytest

from omnigent.team_providers import (
    TEAM_PROVIDER_ENTRY_POINT_GROUP,
    TEAM_PROVIDER_ENV_VAR,
    Team,
    TeamProvider,
    load_team_provider,
)


class _Provider(TeamProvider):
    async def list_teams(self, user_id: str) -> list[Team]:
        return [Team(id="engineering", name=user_id)]


def _entry(name, factory):
    return SimpleNamespace(name=name, load=lambda: factory)


def _register(monkeypatch, entries):
    def discover(*, group):
        assert group == TEAM_PROVIDER_ENTRY_POINT_GROUP
        return entries

    monkeypatch.setattr(importlib.metadata, "entry_points", discover)
    monkeypatch.setenv(TEAM_PROVIDER_ENV_VAR, "example")


def test_unset_provider_skips_discovery(monkeypatch):
    def unexpected_discovery(**kwargs):
        pytest.fail("Disabled providers must not import third-party code")

    monkeypatch.setattr(importlib.metadata, "entry_points", unexpected_discovery)
    for value in (None, "", "  "):
        if value is None:
            monkeypatch.delenv(TEAM_PROVIDER_ENV_VAR, raising=False)
        else:
            monkeypatch.setenv(TEAM_PROVIDER_ENV_VAR, value)
        assert load_team_provider() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [_Provider, lambda: _Provider()])
async def test_loads_only_selected_factory(monkeypatch, factory):
    _register(monkeypatch, [_entry("other", object), _entry("example", factory)])
    monkeypatch.setenv(TEAM_PROVIDER_ENV_VAR, " example ")
    provider = load_team_provider()
    assert isinstance(provider, _Provider)
    assert await provider.list_teams("alice@example.com") == [
        Team(id="engineering", name="alice@example.com")
    ]


@pytest.mark.parametrize("count", [0, 2])
def test_requires_one_matching_entry_point(monkeypatch, count):
    _register(monkeypatch, [_entry("example", _Provider)] * count)
    with pytest.raises(ValueError, match=f"found {count}"):
        load_team_provider()


def _fail_with_secret(*args, **kwargs):
    raise RuntimeError("secret-provider-token")


@pytest.mark.parametrize("failure", ["discover", "load", "construct"])
def test_plugin_errors_do_not_expose_secret_text(monkeypatch, failure):
    entry = _entry("example", _fail_with_secret if failure == "construct" else _Provider)
    if failure == "load":
        entry.load = _fail_with_secret
    _register(monkeypatch, [entry])
    if failure == "discover":
        monkeypatch.setattr(importlib.metadata, "entry_points", _fail_with_secret)
    with pytest.raises(ValueError) as error:
        load_team_provider()
    assert "secret-provider-token" not in str(error.value)
    assert error.value.__suppress_context__


class _SyncProvider(TeamProvider):
    def list_teams(self, user_id: str):  # type: ignore[bad-override] — invalid plugin
        return []


@pytest.mark.parametrize("factory", [object, TeamProvider, _SyncProvider, lambda: _Provider])
def test_requires_async_provider_instance(monkeypatch, factory):
    _register(monkeypatch, [_entry("example", factory)])
    with pytest.raises(ValueError, match="async list_teams"):
        load_team_provider()
