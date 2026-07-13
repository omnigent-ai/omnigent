"""
Tests for ``_build_antigravity_spawn_env`` in
``omnigent/runtime/workflow.py``.

The spawn-env builder maps ``spec.executor`` fields to ``HARNESS_ANTIGRAVITY_*``
env vars the antigravity harness wrap reads at first-turn time. Auth is
Gemini-native (a direct API key, or Vertex AI) — the SDK has no
OpenAI-compatible base_url, so there is deliberately no gateway / Databricks
path here.

Unit test — no subprocess spawn, no real httpx.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime import workflow as wf
from omnigent.runtime.workflow import (
    _build_antigravity_spawn_env,
    configure_agent_harness_with_provider,
)
from omnigent.spec.types import (
    AgentSpec,
    ApiKeyAuth,
    DatabricksAuth,
    ExecutorSpec,
    LLMConfig,
)


@pytest.fixture(autouse=True)
def _isolate_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate config + secrets to a tmp dir and clear ambient Gemini env.

    Empty OMNIGENT_CONFIG_HOME + file secret backend + cleared
    ``GEMINI_API_KEY`` / ``ANTIGRAVITY_API_KEY`` so the no-auth fallback tests
    start clean (a test that wants one sets it).

    :returns: The tmp config-home dir, so a test can write a ``config.yaml``.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_API_KEY", raising=False)
    return tmp_path


def _write_antigravity_config(tmp_path: Path, ref: str) -> None:
    """Write an ``antigravity:`` block referencing *ref* into the isolated config.

    :param tmp_path: The isolated ``OMNIGENT_CONFIG_HOME`` (see the autouse
        fixture).
    :param ref: The secret reference to record, e.g. ``"env:GEMINI_KEY_SRC"``.
    """
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"antigravity": {"api_key_ref": ref}}))


def _make_spec(
    *,
    model: str | None = "gemini-3-pro",
    profile: str | None = None,
    auth: ApiKeyAuth | DatabricksAuth | None = None,
    config_extra: dict[str, object] | None = None,
) -> AgentSpec:
    """Build a minimal antigravity :class:`AgentSpec` for spawn-env tests."""
    config: dict[str, object] = {"harness": "antigravity"}
    if model is not None:
        config["model"] = model
    if profile is not None:
        config["profile"] = profile
    if config_extra is not None:
        config.update(config_extra)
    return AgentSpec(
        spec_version=1,
        name="test-antigravity",
        instructions="You are a test agent.",
        executor=ExecutorSpec(type="omnigent", config=config, model=model, auth=auth),
        llm=LLMConfig(model=model) if model is not None else None,
    )


def test_model_threads_into_env_var() -> None:
    """``executor.model`` is encoded into ``HARNESS_ANTIGRAVITY_MODEL``."""
    env = _build_antigravity_spawn_env(_make_spec(model="gemini-3-pro"))
    assert env["HARNESS_ANTIGRAVITY_MODEL"] == "gemini-3-pro"


def test_no_model_omits_env_var() -> None:
    """A spec with no model omits ``HARNESS_ANTIGRAVITY_MODEL`` entirely."""
    env = _build_antigravity_spawn_env(_make_spec(model=None))
    assert "HARNESS_ANTIGRAVITY_MODEL" not in env


def test_api_key_auth_threads_key_only() -> None:
    """``ApiKeyAuth`` sets the API key; any base_url is dropped (no gateway)."""
    env = _build_antigravity_spawn_env(
        _make_spec(
            model="gemini-3-pro",
            auth=ApiKeyAuth(api_key="ag-secret", base_url="https://openrouter.ai/api/v1"),
        )
    )
    assert env["HARNESS_ANTIGRAVITY_API_KEY"] == "ag-secret"
    # The SDK's LocalAgentConfig has no base_url field, so a gateway URL would
    # be silently inert — the builder must not emit it.
    assert "HARNESS_ANTIGRAVITY_GATEWAY_BASE_URL" not in env


def test_global_auth_is_not_adopted_when_spec_has_no_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy global ``auth:`` key is NEVER adopted by antigravity.

    The global block carries the OpenAI/gateway key the other SDK harnesses
    inherit (an ``sk-…`` key); the Gemini-native SDK can't use it, so shipping
    it as ``HARNESS_ANTIGRAVITY_API_KEY`` would guarantee an auth failure /
    mis-billing. With no spec auth, no stored block, and no ambient Gemini key,
    the builder must emit no key at all (the wrap then uses ambient/Vertex
    creds). Against the old global-``auth:`` fallback this would have set
    ``HARNESS_ANTIGRAVITY_API_KEY`` to the OpenAI key.
    """
    monkeypatch.setattr(wf, "_load_global_auth", lambda: ApiKeyAuth(api_key="sk-openai-global"))
    env = _build_antigravity_spawn_env(_make_spec(model="gemini-3-pro", auth=None))
    assert "HARNESS_ANTIGRAVITY_API_KEY" not in env


def test_vertex_config_threads_project_and_location() -> None:
    """``executor.config`` vertex/project/location thread to the Vertex env vars."""
    env = _build_antigravity_spawn_env(
        _make_spec(
            model="gemini-3-pro",
            config_extra={"vertex": True, "project": "my-proj", "location": "us-central1"},
        )
    )
    assert env["HARNESS_ANTIGRAVITY_VERTEX"] == "1"
    assert env["HARNESS_ANTIGRAVITY_PROJECT"] == "my-proj"
    assert env["HARNESS_ANTIGRAVITY_LOCATION"] == "us-central1"


def test_databricks_auth_ignored_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    """``DatabricksAuth`` is unsupported: no env var emitted, and a warning logged."""
    with caplog.at_level(logging.WARNING, logger=wf.__name__):
        env = _build_antigravity_spawn_env(
            _make_spec(model="gemini-3-pro", auth=DatabricksAuth(profile="dev"))
        )
    # No Databricks profile var — antigravity has no Databricks/gateway path.
    assert "HARNESS_ANTIGRAVITY_DATABRICKS_PROFILE" not in env
    assert "HARNESS_ANTIGRAVITY_API_KEY" not in env
    # The user is told their Databricks auth was ignored rather than silently
    # dropped (which would look like the key "didn't take").
    assert any("Databricks" in rec.message for rec in caplog.records)


def test_legacy_profile_is_ignored() -> None:
    """An ``executor.config['profile']`` does not produce any Databricks var."""
    env = _build_antigravity_spawn_env(_make_spec(model="gemini-3-pro", profile="my-profile"))
    assert "HARNESS_ANTIGRAVITY_DATABRICKS_PROFILE" not in env
    # Only the model var — a legacy profile is meaningless for this harness.
    assert env == {"HARNESS_ANTIGRAVITY_MODEL": "gemini-3-pro"}


def test_databricks_model_prefix_not_auto_routed() -> None:
    """A ``databricks-`` model no longer auto-selects a Databricks profile."""
    env = _build_antigravity_spawn_env(_make_spec(model="databricks-gpt-5-5", profile=None))
    # The old builder set DEFAULT here; antigravity has no Databricks path now.
    assert "HARNESS_ANTIGRAVITY_DATABRICKS_PROFILE" not in env
    assert env == {"HARNESS_ANTIGRAVITY_MODEL": "databricks-gpt-5-5"}


def test_no_auth_non_databricks_model_is_minimal() -> None:
    """A plain Gemini model with no auth yields only the model var.

    The wrap then falls back to the SDK's ambient
    ``GEMINI_API_KEY`` / ``ANTIGRAVITY_API_KEY``.
    """
    env = _build_antigravity_spawn_env(_make_spec(model="gemini-3-pro", profile=None))
    assert env == {"HARNESS_ANTIGRAVITY_MODEL": "gemini-3-pro"}


def test_provider_routing_for_antigravity_fails_loud() -> None:
    """Routing the antigravity harness through a generic provider raises loudly.

    Antigravity is Gemini-native (api_key / Vertex) with no OpenAI-compatible
    base_url, so it must never enter ``configure_agent_harness_with_provider``
    (which emits ``HARNESS_*_GATEWAY_*`` env vars the executor can't use). The
    guard fires before the entry is inspected, so a stand-in entry is fine.
    A regression that dropped the guard would silently emit inert gateway env
    vars instead of failing.
    """
    env: dict[str, str] = {}
    # A stand-in entry: the antigravity guard raises before the entry is ever
    # inspected, so its concrete type is irrelevant (hence the arg-type ignore).
    entry = SimpleNamespace(kind="key", name="some-openai-provider")
    with pytest.raises(OmnigentError) as exc_info:
        configure_agent_harness_with_provider(env, entry, harness_type="antigravity")  # type: ignore[arg-type]
    assert exc_info.value.code == ErrorCode.INVALID_INPUT
    # The guard fires first, so nothing is written to env before the raise.
    assert env == {}


def test_stored_gemini_key_used_when_spec_has_no_auth(
    monkeypatch: pytest.MonkeyPatch, _isolate_global_config: Path
) -> None:
    """A Gemini key registered via ``omnigent setup`` (the ``antigravity:``
    block) flows when the spec declares no auth — so a user need not export it
    in every shell."""
    monkeypatch.setenv("GEMINI_KEY_SRC", "AIza_stored_123")
    _write_antigravity_config(_isolate_global_config, "env:GEMINI_KEY_SRC")
    env = _build_antigravity_spawn_env(_make_spec(model="gemini-3-pro", auth=None))
    assert env["HARNESS_ANTIGRAVITY_API_KEY"] == "AIza_stored_123"


def test_spec_api_key_auth_wins_over_stored_key(
    monkeypatch: pytest.MonkeyPatch, _isolate_global_config: Path
) -> None:
    """An explicit api-key auth on the spec takes precedence over the stored key.

    Failure means a per-agent ``executor.auth`` is silently overridden by the
    machine-wide default — the spec must always win.
    """
    monkeypatch.setenv("GEMINI_KEY_SRC", "AIza_stored_123")
    _write_antigravity_config(_isolate_global_config, "env:GEMINI_KEY_SRC")
    env = _build_antigravity_spawn_env(
        _make_spec(model="gemini-3-pro", auth=ApiKeyAuth(api_key="AIza_explicit_999"))
    )
    assert env["HARNESS_ANTIGRAVITY_API_KEY"] == "AIza_explicit_999"


def test_stored_key_used_and_global_auth_ignored(
    monkeypatch: pytest.MonkeyPatch, _isolate_global_config: Path
) -> None:
    """The dedicated ``antigravity:`` block is used; the global ``auth:`` is ignored.

    The Gemini-specific block authenticates a no-auth spec, and a present global
    ``auth:`` key (meant for another harness) has no influence at all. Against
    the old behavior the global key was a fallback tier; now it is never read.
    """
    monkeypatch.setattr(wf, "_load_global_auth", lambda: ApiKeyAuth(api_key="sk-openai-global"))
    monkeypatch.setenv("GEMINI_KEY_SRC", "AIza_stored_123")
    _write_antigravity_config(_isolate_global_config, "env:GEMINI_KEY_SRC")
    env = _build_antigravity_spawn_env(_make_spec(model="gemini-3-pro", auth=None))
    assert env["HARNESS_ANTIGRAVITY_API_KEY"] == "AIza_stored_123"


def test_databricks_auth_does_not_adopt_stored_key(
    monkeypatch: pytest.MonkeyPatch, _isolate_global_config: Path
) -> None:
    """An explicit ``DatabricksAuth`` never adopts the stored Gemini key.

    The stored-key fallback applies ONLY to a spec with no auth at all; a
    databricks-routed spec has explicitly chosen a non-Gemini credential, so
    pulling the Gemini key would mis-authenticate the run.
    """
    monkeypatch.setenv("GEMINI_KEY_SRC", "AIza_stored_123")
    _write_antigravity_config(_isolate_global_config, "env:GEMINI_KEY_SRC")
    env = _build_antigravity_spawn_env(
        _make_spec(model="gemini-3-pro", auth=DatabricksAuth(profile="oss"))
    )
    assert "HARNESS_ANTIGRAVITY_API_KEY" not in env


def test_ambient_gemini_key_adopted_when_no_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no spec/stored/global key, an ambient GEMINI_API_KEY is adopted.

    Mirrors the other key-only SDK harnesses: an exported key (or a host
    launched with one) authenticates a no-auth spec without per-spec config.
    """
    monkeypatch.setattr(wf, "_load_global_auth", lambda: None)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza_ambient_456")
    env = _build_antigravity_spawn_env(_make_spec(model="gemini-3-pro", auth=None))
    assert env["HARNESS_ANTIGRAVITY_API_KEY"] == "AIza_ambient_456"


def test_ambient_gemini_key_wins_over_global_openai_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ambient ``GEMINI_API_KEY`` is used while a global OpenAI ``auth:`` is ignored.

    This is the core credential-safety guarantee: with no spec auth and no stored
    ``antigravity:`` block, a present global ``auth:`` (the OpenAI/gateway
    ``sk-…`` key) must not short-circuit the user's ambient Gemini key. Against
    the old global-``auth:`` fallback the builder would have shipped
    ``sk-openai-global`` instead of the real Gemini key.
    """
    monkeypatch.setattr(wf, "_load_global_auth", lambda: ApiKeyAuth(api_key="sk-openai-global"))
    monkeypatch.setenv("GEMINI_API_KEY", "AIza_ambient_456")
    env = _build_antigravity_spawn_env(_make_spec(model="gemini-3-pro", auth=None))
    assert env["HARNESS_ANTIGRAVITY_API_KEY"] == "AIza_ambient_456"


def test_unresolvable_stored_key_is_omitted(_isolate_global_config: Path) -> None:
    """A dangling stored reference resolves softly to no env var.

    The ``antigravity:`` block names ``env:GEMINI_KEY_SRC`` but the var is
    unset, so the builder must omit ``HARNESS_ANTIGRAVITY_API_KEY`` (leaving the
    SDK's ambient / Vertex creds to satisfy auth) rather than crash the spawn.
    """
    _write_antigravity_config(_isolate_global_config, "env:GEMINI_KEY_SRC")
    env = _build_antigravity_spawn_env(_make_spec(model="gemini-3-pro", auth=None))
    assert "HARNESS_ANTIGRAVITY_API_KEY" not in env
