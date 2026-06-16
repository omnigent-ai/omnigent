"""
Tests for ``_build_cursor_spawn_env`` in ``omnigent/runtime/workflow.py``.

The spawn-env builder maps ``spec`` fields to the ``HARNESS_CURSOR_*`` env
vars the cursor harness wrap reads at first-turn time. Unlike the
gateway-backed builders, cursor has NO Databricks-gateway path: only an
explicit ``api_key`` auth maps to ``HARNESS_CURSOR_API_KEY``, and a
``DatabricksAuth`` profile is deliberately ignored (cursor-agent talks only
to Cursor's own backend). Mirrors ``test_openai_agents_sdk_spawn_env.py``.

This is a unit test — no subprocess spawn. End-to-end verification of the
spawn-env → wrap → executor path lives in the harness e2e tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.runtime.workflow import _build_cursor_spawn_env
from omnigent.spec.types import (
    AgentSpec,
    ApiKeyAuth,
    DatabricksAuth,
    ExecutorSpec,
    LLMConfig,
)


@pytest.fixture(autouse=True)
def _isolate_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point OMNIGENT_CONFIG_HOME at an empty temp dir so the developer's real
    ``~/.omnigent/config.yaml`` can't leak in, and clear any ambient
    ``CURSOR_API_KEY`` so the no-auth / DatabricksAuth cases are deterministic
    (the builder falls back to an ambient key — see the ambient-fallback test)."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)


def _make_spec(
    *,
    model: str | None = "gpt-5",
    name: str = "test-cursor",
    auth: ApiKeyAuth | DatabricksAuth | None = None,
) -> AgentSpec:
    """Build a minimal cursor :class:`AgentSpec` for the spawn-env tests."""
    config: dict[str, object] = {"harness": "cursor"}
    if model is not None:
        config["model"] = model
    return AgentSpec(
        spec_version=1,
        name=name,
        instructions="You are a test agent.",
        executor=ExecutorSpec(type="omnigent", config=config, model=model, auth=auth),
        llm=LLMConfig(model=model) if model is not None else None,
    )


def test_model_threads_into_env_var() -> None:
    """``executor.model`` is encoded into ``HARNESS_CURSOR_MODEL``."""
    env = _build_cursor_spawn_env(_make_spec(model="gpt-5"))
    assert env["HARNESS_CURSOR_MODEL"] == "gpt-5"


def test_no_model_produces_no_model_env_var() -> None:
    """A spec with no model omits ``HARNESS_CURSOR_MODEL`` (cursor's default applies)."""
    env = _build_cursor_spawn_env(_make_spec(model=None))
    assert "HARNESS_CURSOR_MODEL" not in env


def test_api_key_auth_sets_api_key_env_var() -> None:
    """``executor.auth: {type: api_key, ...}`` sets ``HARNESS_CURSOR_API_KEY``."""
    env = _build_cursor_spawn_env(_make_spec(auth=ApiKeyAuth(api_key="cur_test_123")))
    assert env["HARNESS_CURSOR_API_KEY"] == "cur_test_123"


def test_databricks_auth_does_not_set_api_key() -> None:
    """A ``DatabricksAuth`` profile has no cursor equivalent and is ignored.

    Failure means a Databricks profile is mis-forwarded as a Cursor API key —
    cursor-agent has no gateway path, so the only correct behaviour is to leave
    auth to an inherited ``CURSOR_API_KEY`` / ``cursor-agent login``.
    """
    env = _build_cursor_spawn_env(_make_spec(auth=DatabricksAuth(profile="oss")))
    assert "HARNESS_CURSOR_API_KEY" not in env


def test_no_auth_omits_api_key_env_var() -> None:
    """With no spec auth and no ambient key, no ``HARNESS_CURSOR_API_KEY`` is written."""
    env = _build_cursor_spawn_env(_make_spec(auth=None))
    assert "HARNESS_CURSOR_API_KEY" not in env


def test_ambient_cursor_api_key_used_when_no_spec_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no spec api-key auth, an ambient ``CURSOR_API_KEY`` is threaded as
    ``HARNESS_CURSOR_API_KEY`` so a user who exported the key can run cursor
    without declaring auth in the spec (the SDK needs it in the harness env)."""
    monkeypatch.setenv("CURSOR_API_KEY", "crsr_ambient")
    env = _build_cursor_spawn_env(_make_spec(auth=None))
    assert env["HARNESS_CURSOR_API_KEY"] == "crsr_ambient"


def test_spec_api_key_wins_over_ambient(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit spec api-key auth takes precedence over an ambient key."""
    monkeypatch.setenv("CURSOR_API_KEY", "crsr_ambient")
    env = _build_cursor_spawn_env(_make_spec(auth=ApiKeyAuth(api_key="crsr_spec")))
    assert env["HARNESS_CURSOR_API_KEY"] == "crsr_spec"


def test_skills_filter_always_set() -> None:
    """``HARNESS_CURSOR_SKILLS_FILTER`` is always written so the wrap never
    falls back to ``"all"`` and overrides an explicit ``skills: none``."""
    env = _build_cursor_spawn_env(_make_spec())
    assert "HARNESS_CURSOR_SKILLS_FILTER" in env


def test_name_threads_into_agent_name_env_var() -> None:
    """``spec.name`` is forwarded as ``HARNESS_CURSOR_AGENT_NAME``."""
    env = _build_cursor_spawn_env(_make_spec(name="polly"))
    assert env["HARNESS_CURSOR_AGENT_NAME"] == "polly"


def test_workdir_threads_into_bundle_dir_env_var(tmp_path: Path) -> None:
    """A bundle ``workdir`` is forwarded as ``HARNESS_CURSOR_BUNDLE_DIR``."""
    env = _build_cursor_spawn_env(_make_spec(), workdir=tmp_path)
    assert env["HARNESS_CURSOR_BUNDLE_DIR"] == str(tmp_path)


def test_no_workdir_omits_bundle_dir_env_var() -> None:
    """No ``workdir`` omits ``HARNESS_CURSOR_BUNDLE_DIR``."""
    env = _build_cursor_spawn_env(_make_spec())
    assert "HARNESS_CURSOR_BUNDLE_DIR" not in env


def _write_cursor_config(tmp_path: Path, ref: str) -> None:
    """Write a ``cursor:`` block referencing *ref* into the isolated config.

    :param tmp_path: The isolated ``OMNIGENT_CONFIG_HOME`` (see the autouse
        fixture).
    :param ref: The secret reference to record, e.g. ``"env:CURSOR_KEY_SRC"``.
    """
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"cursor": {"api_key_ref": ref}}))


def test_stored_cursor_key_used_when_spec_has_no_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A CURSOR_API_KEY registered via ``omnigent setup`` flows when the spec
    declares no auth — so a user need not export it in every shell."""
    monkeypatch.setenv("CURSOR_KEY_SRC", "crsr_stored_123")
    _write_cursor_config(tmp_path, "env:CURSOR_KEY_SRC")
    env = _build_cursor_spawn_env(_make_spec(auth=None))
    assert env["HARNESS_CURSOR_API_KEY"] == "crsr_stored_123"


def test_spec_api_key_auth_wins_over_stored_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit api-key auth on the spec takes precedence over the stored key.

    Failure means a per-agent ``executor.auth`` is silently overridden by the
    machine-wide default — the spec must always win.
    """
    monkeypatch.setenv("CURSOR_KEY_SRC", "crsr_stored_123")
    _write_cursor_config(tmp_path, "env:CURSOR_KEY_SRC")
    env = _build_cursor_spawn_env(_make_spec(auth=ApiKeyAuth(api_key="crsr_explicit_999")))
    assert env["HARNESS_CURSOR_API_KEY"] == "crsr_explicit_999"


def test_databricks_auth_does_not_adopt_stored_cursor_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit ``DatabricksAuth`` never adopts the stored cursor key.

    The stored-key fallback applies ONLY to a spec with no auth at all; a
    databricks-routed spec has explicitly chosen a non-cursor credential, so
    pulling the cursor key would mis-authenticate the run.
    """
    monkeypatch.setenv("CURSOR_KEY_SRC", "crsr_stored_123")
    _write_cursor_config(tmp_path, "env:CURSOR_KEY_SRC")
    env = _build_cursor_spawn_env(_make_spec(auth=DatabricksAuth(profile="oss")))
    assert "HARNESS_CURSOR_API_KEY" not in env


def test_unresolvable_stored_key_is_omitted(tmp_path: Path) -> None:
    """A dangling stored reference resolves softly to no env var.

    The ``cursor:`` block names ``env:CURSOR_KEY_SRC`` but the var is unset, so
    the builder must omit ``HARNESS_CURSOR_API_KEY`` (leaving cursor's own
    login / inherited key to satisfy auth) rather than crash the spawn.
    """
    _write_cursor_config(tmp_path, "env:CURSOR_KEY_SRC")
    env = _build_cursor_spawn_env(_make_spec(auth=None))
    assert "HARNESS_CURSOR_API_KEY" not in env
