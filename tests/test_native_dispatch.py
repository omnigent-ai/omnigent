from __future__ import annotations

from collections.abc import Iterator

import pytest

import omnigent.harness_plugins as hp
from omnigent import native_dispatch


@pytest.fixture(autouse=True)
def _reset_caches() -> Iterator[None]:
    hp.reset_plugin_state_for_tests()
    native_dispatch.reset_resolve_cache_for_tests()
    yield
    hp.reset_plugin_state_for_tests()
    native_dispatch.reset_resolve_cache_for_tests()


def test_resolve_colon_and_dot_paths() -> None:
    # module:attr form
    assert native_dispatch.resolve("omnigent.harness_plugins:load_object") is hp.load_object
    # module.attr form
    assert native_dispatch.resolve("omnigent.harness_plugins.load_object") is hp.load_object


def test_resolve_is_cached() -> None:
    first = native_dispatch.resolve("omnigent.harness_plugins:native_agents")
    second = native_dispatch.resolve("omnigent.harness_plugins:native_agents")
    assert first is second
    assert "omnigent.harness_plugins:native_agents" in native_dispatch._RESOLVE_CACHE


def test_resolve_hook_returns_none_for_unset_optional_hook() -> None:
    provider = hp.native_provider_for_key("claude")
    assert provider is not None
    # interrupt_handler is not yet a module-level function, so it stays None.
    assert provider.interrupt_handler is None
    assert native_dispatch.resolve_hook(provider, "interrupt_handler") is None


def test_resolve_hook_resolves_populated_hook() -> None:
    provider = hp.native_provider_for_key("pi")
    assert provider is not None
    run_native = native_dispatch.resolve_hook(provider, "run_native")
    assert callable(run_native)
    assert run_native.__name__ == "run_pi_native"


def test_resolve_hook_for_key() -> None:
    run_native = native_dispatch.resolve_hook_for_key("codex", "run_native")
    assert callable(run_native)
    assert run_native.__name__ == "run_codex_native"


def test_resolve_hook_for_unknown_key_returns_none() -> None:
    assert native_dispatch.resolve_hook_for_key("does-not-exist", "run_native") is None
