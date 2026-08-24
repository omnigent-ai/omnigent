"""Tests for the framework-owned cognee memory boundary.

The optional ``cognee`` package is NOT in the dev set — it is simulated by
injecting a fake module into ``sys.modules`` and forcing the installed-probe,
so these tests run identically with and without the extra. Covers the gate
precedence (env kill-switch > installed probe), settings/data-root
resolution, dataset sanitization, the framework-instruction gate, the
circuit breaker, and the bounded search/add/cognify paths.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnigent.runtime import memory as memory_mod
from omnigent.runtime.memory import (
    COGNEE_MEMORY_INSTRUCTION,
    cognee_available,
    cognee_data_root,
    cognee_framework_instructions,
    memory_add,
    memory_search,
    resolve_grants,
    sanitize_dataset_name,
    spec_declares_cognee_builtin,
)


@pytest.fixture(autouse=True)
def _reset_memory_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Isolate the module's process-level state between tests."""
    monkeypatch.delenv(memory_mod.COGNEE_DISABLE_ENV, raising=False)
    memory_mod.breaker.reset()
    memory_mod._store_configured = False
    memory_mod._registered_agent_connections.clear()
    yield
    memory_mod.breaker.reset()
    memory_mod._store_configured = False
    memory_mod._registered_agent_connections.clear()
    if memory_mod._background_executor is not None:
        memory_mod._background_executor.shutdown(wait=True)
        memory_mod._background_executor = None


def _fake_cognee(
    *,
    search_results: list[Any] | None = None,
    search_error: Exception | None = None,
) -> MagicMock:
    """Build a fake ``cognee`` module with async search/add/cognify."""
    fake = MagicMock()
    fake.SearchType = SimpleNamespace(GRAPH_COMPLETION="graph_completion", CHUNKS="chunks")
    if search_error is not None:
        fake.search = AsyncMock(side_effect=search_error)
    else:
        fake.search = AsyncMock(return_value=search_results or [])
    fake.add = AsyncMock()
    fake.cognify = AsyncMock()
    fake.config = MagicMock()
    return fake


def _install(
    monkeypatch: pytest.MonkeyPatch,
    fake: MagicMock,
) -> None:
    """Make the fake cognee importable and the installed-probe pass."""
    monkeypatch.setitem(sys.modules, "cognee", fake)
    monkeypatch.setattr(memory_mod, "cognee_installed", lambda: True)


def _settings(tmp_path: Path, **extra: Any) -> dict[str, Any]:
    return {"data_root": str(tmp_path / "cognee"), **extra}


# ---------------------------------------------------------------------------
# Gate precedence
# ---------------------------------------------------------------------------


def test_available_requires_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory_mod, "cognee_installed", lambda: False)
    assert cognee_available() is False


def test_env_kill_switch_beats_installed_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory_mod, "cognee_installed", lambda: True)
    monkeypatch.setenv(memory_mod.COGNEE_DISABLE_ENV, "1")
    assert cognee_available() is False


@pytest.mark.parametrize("value", ["true", "TRUE", "yes", "1"])
def test_kill_switch_truthy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setattr(memory_mod, "cognee_installed", lambda: True)
    monkeypatch.setenv(memory_mod.COGNEE_DISABLE_ENV, value)
    assert cognee_available() is False


def test_kill_switch_falsy_value_leaves_gate_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory_mod, "cognee_installed", lambda: True)
    monkeypatch.setenv(memory_mod.COGNEE_DISABLE_ENV, "0")
    assert cognee_available() is True


# ---------------------------------------------------------------------------
# Settings + data root + sanitization
# ---------------------------------------------------------------------------


def test_data_root_defaults_under_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.process_logging as process_logging

    monkeypatch.setattr(process_logging, "data_dir", lambda: Path("/tmp/omnigent-data"))
    assert cognee_data_root({}) == Path("/tmp/omnigent-data") / "cognee"


def test_data_root_honors_config_override(tmp_path: Path) -> None:
    assert cognee_data_root({"data_root": str(tmp_path)}) == tmp_path


def test_settings_ignores_non_mapping_block(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.config as config_mod

    monkeypatch.setattr(config_mod, "load_effective_config", lambda: {"cognee": True})
    assert memory_mod.cognee_settings() == {}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ag_ABC-123", "ag_abc_123"),
        ("  Team Knowledge  ", "team_knowledge"),
        ("___", "default"),
    ],
)
def test_sanitize_dataset_name(raw: str, expected: str) -> None:
    assert sanitize_dataset_name(raw) == expected


# ---------------------------------------------------------------------------
# Memory grants (cross-agent access layer)
# ---------------------------------------------------------------------------


def test_grants_private_resolution_precedence() -> None:
    grants = resolve_grants({"dataset": "Custom DS"}, agent_id="ag_1", conversation_id="c_1")
    assert grants.private == "custom_ds"
    grants = resolve_grants({}, agent_id="ag_1", conversation_id="c_1")
    assert grants.private == "ag_1"
    grants = resolve_grants({}, agent_id=None, conversation_id="c_1")
    assert grants.private == "c_1"


def test_grants_require_a_resolvable_private_dataset() -> None:
    with pytest.raises(ValueError):
        resolve_grants({}, agent_id=None, conversation_id=None)


def test_grants_parse_csv_peer_lists_sanitized_and_deduped() -> None:
    grants = resolve_grants(
        {
            "read_datasets": "ag_Researcher, ag_writer,ag_researcher, ",
            "write_datasets": "ag_writer",
        },
        agent_id="ag_me",
        conversation_id=None,
    )
    assert grants.readable_peers == ("ag_researcher", "ag_writer")
    assert grants.writable_peers == ("ag_writer",)


def test_grants_readable_and_writable_surfaces() -> None:
    grants = resolve_grants(
        {
            "shared_dataset": "team",
            "read_datasets": "ag_peer_r",
            "write_datasets": "ag_peer_w",
        },
        agent_id="ag_me",
        conversation_id=None,
    )
    assert grants.readable() == ("ag_me", "team", "ag_peer_r")
    assert grants.writable() == ("ag_me", "team", "ag_peer_w")
    assert grants.can_read("ag_peer_r") is True
    assert grants.can_read("ag_peer_w") is False
    assert grants.can_write("ag_peer_w") is True
    assert grants.can_write("ag_peer_r") is False
    # Private and shared are always read/write for the owning agent.
    assert grants.can_write("ag_me") is True
    assert grants.can_write("team") is True


def test_grants_dedup_peer_matching_private() -> None:
    grants = resolve_grants(
        {"read_datasets": "ag_me, ag_other"}, agent_id="ag_me", conversation_id=None
    )
    assert grants.readable() == ("ag_me", "ag_other")


def test_grants_tier_pools_from_config() -> None:
    grants = resolve_grants(
        {"user_dataset": "u_vasilije", "team_dataset": "Platform Team", "org_dataset": "acme"},
        agent_id="ag_me",
        conversation_id=None,
    )
    assert grants.tier("user") == "u_vasilije"
    assert grants.tier("team") == "platform_team"
    assert grants.tier("org") == "acme"
    # Tier pools are read/write, ordered narrowest first after private.
    assert grants.readable() == ("ag_me", "u_vasilije", "platform_team", "acme")
    assert grants.writable() == ("ag_me", "u_vasilije", "platform_team", "acme")


def test_grants_tiers_inherit_from_global_settings() -> None:
    grants = resolve_grants(
        {"team_dataset": "spec_team"},
        agent_id="ag_me",
        conversation_id=None,
        settings={"team_dataset": "global_team", "org_dataset": "global_org", "user_dataset": 3},
    )
    # Per-agent config wins over the global block; non-string globals ignored.
    assert grants.tier("team") == "spec_team"
    assert grants.tier("org") == "global_org"
    assert grants.tier("user") is None


def test_grants_tiers_absent_by_default() -> None:
    grants = resolve_grants({}, agent_id="ag_me", conversation_id=None)
    assert grants.tier("user") is None
    assert grants.tier("team") is None
    assert grants.tier("org") is None
    assert grants.readable() == ("ag_me",)


# ---------------------------------------------------------------------------
# Framework instruction gate
# ---------------------------------------------------------------------------


def _spec_with(*names: str) -> SimpleNamespace:
    return SimpleNamespace(
        tools=SimpleNamespace(builtins=[SimpleNamespace(name=n, config={}) for n in names])
    )


def test_spec_declaration_probe() -> None:
    assert spec_declares_cognee_builtin(_spec_with("cognee_search")) is True
    assert spec_declares_cognee_builtin(_spec_with("web_search")) is False
    assert spec_declares_cognee_builtin(None) is False


def test_instruction_present_only_when_declared_and_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(memory_mod, "cognee_installed", lambda: True)
    assert cognee_framework_instructions(_spec_with("cognee_remember")) == (
        COGNEE_MEMORY_INSTRUCTION,
    )
    assert cognee_framework_instructions(_spec_with("web_search")) == ()


def test_instruction_absent_when_kill_switched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory_mod, "cognee_installed", lambda: True)
    monkeypatch.setenv(memory_mod.COGNEE_DISABLE_ENV, "1")
    assert cognee_framework_instructions(_spec_with("cognee_search")) == ()


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


def test_breaker_opens_after_consecutive_failures() -> None:
    for _ in range(3):
        memory_mod.breaker.record_failure()
    assert memory_mod.breaker.allow() is False


def test_breaker_success_resets_failure_streak() -> None:
    memory_mod.breaker.record_failure()
    memory_mod.breaker.record_failure()
    memory_mod.breaker.record_success()
    memory_mod.breaker.record_failure()
    assert memory_mod.breaker.allow() is True


# ---------------------------------------------------------------------------
# Search / add / cognify
# ---------------------------------------------------------------------------


def test_search_returns_stringified_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _fake_cognee(search_results=["likes tea", {"fact": "NYC"}])
    _install(monkeypatch, fake)
    results = memory_search("about the user", ["ds_a"], settings=_settings(tmp_path))
    assert results == ["likes tea", "{'fact': 'NYC'}"]
    kwargs = fake.search.call_args.kwargs
    assert kwargs["query_text"] == "about the user"
    assert kwargs["datasets"] == ["ds_a"]
    assert kwargs["query_type"] == "graph_completion"


def test_search_honors_configured_search_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _fake_cognee(search_results=["m"])
    _install(monkeypatch, fake)
    memory_search("q", ["ds"], settings=_settings(tmp_path, search_type="CHUNKS"))
    assert fake.search.call_args.kwargs["query_type"] == "chunks"


def test_search_failure_returns_empty_and_counts_toward_breaker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _fake_cognee(search_error=RuntimeError("store down"))
    _install(monkeypatch, fake)
    settings = _settings(tmp_path)
    for _ in range(3):
        assert memory_search("q", ["ds"], settings=settings) == []
    # Breaker is now open: the next call short-circuits without touching cognee.
    fake.search.reset_mock()
    assert memory_search("q", ["ds"], settings=settings) == []
    fake.search.assert_not_called()


def test_search_short_circuits_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory_mod, "cognee_installed", lambda: False)
    assert memory_search("q", ["ds"]) == []


def test_add_writes_and_schedules_background_cognify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _fake_cognee()
    _install(monkeypatch, fake)
    cognified: list[str] = []
    monkeypatch.setattr(memory_mod, "_cognify_blocking", cognified.append)
    assert memory_add("fact", "ds_a", settings=_settings(tmp_path), node_set=["conv_1"]) is True
    kwargs = fake.add.call_args.kwargs
    assert kwargs["dataset_name"] == "ds_a"
    assert kwargs["node_set"] == ["conv_1"]
    assert memory_mod._background_executor is not None
    memory_mod._background_executor.shutdown(wait=True)
    assert cognified == ["ds_a"]


def test_add_failure_returns_false_and_skips_cognify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _fake_cognee()
    fake.add = AsyncMock(side_effect=RuntimeError("disk full"))
    _install(monkeypatch, fake)
    cognified: list[str] = []
    monkeypatch.setattr(memory_mod, "_cognify_blocking", cognified.append)
    assert memory_add("fact", "ds_a", settings=_settings(tmp_path)) is False
    assert cognified == []


class _InlineExecutor:
    """Executor stub that runs submissions synchronously for assertions."""

    def submit(self, fn: Any, *args: Any) -> None:
        fn(*args)


def test_agent_registration_submits_once_per_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory_mod, "cognee_installed", lambda: True)
    monkeypatch.setattr(memory_mod, "_get_background_executor", _InlineExecutor)
    posted: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        memory_mod,
        "_register_agent_blocking",
        lambda grants, session_id, settings: posted.append((grants.private, session_id)),
    )
    grants = resolve_grants({}, agent_id="ag_me", conversation_id=None)
    memory_mod.ensure_agent_registered(grants, session_id="conv_1", settings={})
    memory_mod.ensure_agent_registered(grants, session_id="conv_1", settings={})
    memory_mod.ensure_agent_registered(grants, session_id="conv_2", settings={})
    assert posted == [("ag_me", "conv_1"), ("ag_me", "conv_2")]


def test_agent_registration_skips_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory_mod, "cognee_installed", lambda: False)
    monkeypatch.setattr(
        memory_mod,
        "_register_agent_blocking",
        lambda *a: pytest.fail("must not register when unavailable"),
    )
    grants = resolve_grants({}, agent_id="ag_me", conversation_id=None)
    memory_mod.ensure_agent_registered(grants, session_id="conv_1", settings={})
    assert memory_mod._registered_agent_connections == set()


def _install_registration_fakes(
    monkeypatch: pytest.MonkeyPatch, fake: MagicMock
) -> tuple[MagicMock, AsyncMock]:
    """Fake the cognee agent-registry submodules for the blocking body."""
    _install(monkeypatch, fake)
    models = MagicMock()
    request_kwargs = MagicMock(side_effect=lambda **kw: kw)
    models.RegisterAgentRequest = request_kwargs
    operations = MagicMock()
    operations.register_agent_from_request = AsyncMock()
    users = MagicMock()
    users.get_default_user = AsyncMock(return_value="default-user")
    monkeypatch.setitem(sys.modules, "cognee.modules", MagicMock())
    monkeypatch.setitem(sys.modules, "cognee.modules.agents", MagicMock())
    monkeypatch.setitem(sys.modules, "cognee.modules.agents.models", models)
    monkeypatch.setitem(sys.modules, "cognee.modules.agents.operations", operations)
    monkeypatch.setitem(sys.modules, "cognee.modules.users", MagicMock())
    monkeypatch.setitem(sys.modules, "cognee.modules.users.methods", users)
    return request_kwargs, operations.register_agent_from_request


def test_registration_mirrors_grants_into_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    request_kwargs, register = _install_registration_fakes(monkeypatch, _fake_cognee())
    grants = resolve_grants(
        {"shared_dataset": "team", "read_datasets": "ag_peer_r", "write_datasets": "ag_peer_w"},
        agent_id="ag_me",
        conversation_id=None,
    )
    memory_mod._register_agent_blocking(grants, "conv_9", _settings(tmp_path))
    kwargs = request_kwargs.call_args.kwargs
    assert kwargs["agent_session_name"] == "ag_me"
    assert kwargs["session_id"] == "conv_9"
    assert kwargs["type"] == "omnigent"
    assert kwargs["memory_mode"] == "cognee"
    # Readable first, then write-only grants — flat names for the request...
    assert kwargs["dataset_names"] == ["ag_me", "team", "ag_peer_r", "ag_peer_w"]
    # ...with the precise read/write split preserved in metadata.
    assert kwargs["metadata"]["grants"] == {
        "readable": ["ag_me", "team", "ag_peer_r"],
        "writable": ["ag_me", "team", "ag_peer_w"],
    }
    assert register.await_count == 1


def test_registration_failure_is_swallowed_and_spares_breaker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, register = _install_registration_fakes(monkeypatch, _fake_cognee())
    register.side_effect = RuntimeError("registry down")
    grants = resolve_grants({}, agent_id="ag_me", conversation_id=None)
    memory_mod._register_agent_blocking(grants, "conv_9", _settings(tmp_path))
    # Observability-only: no exception escaped and the breaker is untouched.
    assert memory_mod.breaker.allow() is True


def test_local_store_configured_once_with_llm_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _fake_cognee(search_results=["m"])
    _install(monkeypatch, fake)
    settings = _settings(tmp_path, llm_api_key="sk-test", llm_provider="openai")
    memory_search("q", ["ds"], settings=settings)
    memory_search("q", ["ds"], settings=settings)
    root = tmp_path / "cognee"
    fake.config.system_root_directory.assert_called_once_with(str(root / "system"))
    fake.config.data_root_directory.assert_called_once_with(str(root / "data"))
    fake.config.set_llm_api_key.assert_called_once_with("sk-test")
    fake.config.set_llm_config.assert_called_once_with({"llm_provider": "openai"})
    assert (root / "system").is_dir()
    assert (root / "data").is_dir()
