"""Tests for ``_build_cursor_cloud_spawn_env`` in ``omnigent/runtime/workflow.py``.

The cursor-cloud spawn-env builder maps ``spec`` fields to the
``HARNESS_CURSOR_CLOUD_*`` env vars the cloud harness wrap reads. It mirrors the
local cursor builder's auth precedence (spec api-key > stored > ambient
``CURSOR_API_KEY``) and additionally resolves the GitHub repo URL + starting ref
the cloud agent clones (from the cwd ``origin`` remote, or an
``OMNIGENT_CURSOR_CLOUD_REPO`` / ``_REF`` override). Like the local builder, a
``DatabricksAuth`` profile has no cursor equivalent and is ignored.

Unit test — no subprocess spawn. The repo-resolution path is exercised via the
``OMNIGENT_CURSOR_CLOUD_REPO`` override to avoid depending on a real git cwd.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omnigent.runtime.workflow import _build_cursor_cloud_spawn_env
from omnigent.spec.types import (
    AgentSpec,
    ApiKeyAuth,
    DatabricksAuth,
    ExecutorSpec,
    LLMConfig,
)


@pytest.fixture(autouse=True)
def _isolate_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate config + clear ambient/override env so each case is deterministic."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.delenv("OMNIGENT_CURSOR_CLOUD_REPO", raising=False)
    monkeypatch.delenv("OMNIGENT_CURSOR_CLOUD_REF", raising=False)


def _make_spec(
    *,
    model: str | None = "composer-2.5",
    name: str = "test-cursor-cloud",
    auth: ApiKeyAuth | DatabricksAuth | None = None,
) -> AgentSpec:
    """Build a minimal cursor-cloud :class:`AgentSpec` for the spawn-env tests."""
    config: dict[str, object] = {"harness": "cursor-cloud"}
    if model is not None:
        config["model"] = model
    return AgentSpec(
        spec_version=1,
        name=name,
        instructions="You are a test agent.",
        executor=ExecutorSpec(type="omnigent", config=config, model=model, auth=auth),
        llm=LLMConfig(model=model) if model is not None else None,
    )


# ---------------------------------------------------------------------------
# Repo / ref resolution
# ---------------------------------------------------------------------------


def _init_repo(path: Path, *, remote: str | None, branch: str = "feature/x") -> None:
    """Initialize a git repo at *path* on *branch*, optionally with an origin remote."""
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    if remote is not None:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)
    (path / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


def test_cwd_origin_remote_resolves_repo_and_ref(tmp_path: Path) -> None:
    """The default runtime path: no OMNIGENT_CURSOR_CLOUD_REPO/REF override, so the
    repo + ref are derived from the cwd's ``origin`` remote and current branch.

    (The autouse fixture already clears the override env vars.)
    """
    _init_repo(tmp_path, remote="git@github.com:org/repo.git", branch="feature/x")
    env = _build_cursor_cloud_spawn_env(_make_spec(), cwd=tmp_path)
    # Origin remote normalized to https form, current branch as the ref.
    assert env["HARNESS_CURSOR_CLOUD_REPO"] == "https://github.com/org/repo"
    assert env["HARNESS_CURSOR_CLOUD_REF"] == "feature/x"


def test_repo_override_flows_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_CURSOR_CLOUD_REPO", "git@github.com:org/repo.git")
    monkeypatch.setenv("OMNIGENT_CURSOR_CLOUD_REF", "release-1.0")
    env = _build_cursor_cloud_spawn_env(_make_spec())
    # Normalized to https form by the resolver.
    assert env["HARNESS_CURSOR_CLOUD_REPO"] == "https://github.com/org/repo"
    assert env["HARNESS_CURSOR_CLOUD_REF"] == "release-1.0"


def test_repo_override_without_ref_omits_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_CURSOR_CLOUD_REPO", "https://github.com/org/repo")
    env = _build_cursor_cloud_spawn_env(_make_spec())
    assert env["HARNESS_CURSOR_CLOUD_REPO"] == "https://github.com/org/repo"
    assert "HARNESS_CURSOR_CLOUD_REF" not in env


def test_unresolvable_repo_is_omitted() -> None:
    """No cwd and no override -> repo resolution fails softly, leaving the repo
    env unset so the executor surfaces a clear turn-time error (not a spawn crash)."""
    env = _build_cursor_cloud_spawn_env(_make_spec(), cwd=None)
    assert "HARNESS_CURSOR_CLOUD_REPO" not in env
    assert "HARNESS_CURSOR_CLOUD_REF" not in env


def test_cwd_threads_into_cwd_env_var(tmp_path: Path) -> None:
    env = _build_cursor_cloud_spawn_env(_make_spec(), cwd=tmp_path)
    assert env["HARNESS_CURSOR_CLOUD_CWD"] == str(tmp_path)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def test_model_threads_into_env_var() -> None:
    env = _build_cursor_cloud_spawn_env(_make_spec(model="claude-4.6-sonnet-thinking"))
    assert env["HARNESS_CURSOR_CLOUD_MODEL"] == "claude-4.6-sonnet-thinking"


def test_no_model_omits_model_env_var() -> None:
    env = _build_cursor_cloud_spawn_env(_make_spec(model=None))
    assert "HARNESS_CURSOR_CLOUD_MODEL" not in env


def test_databricks_model_forwarded_for_executor_to_drop() -> None:
    """A ``databricks-*`` spec model is forwarded as-is; the executor's
    ``_resolve_cloud_model`` drops it to the cloud default at turn time (the
    spawn-env builder does not second-guess the model id)."""
    env = _build_cursor_cloud_spawn_env(_make_spec(model="databricks-claude-sonnet-4-6"))
    assert env["HARNESS_CURSOR_CLOUD_MODEL"] == "databricks-claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Auth precedence
# ---------------------------------------------------------------------------


def test_api_key_auth_sets_api_key_env_var() -> None:
    env = _build_cursor_cloud_spawn_env(_make_spec(auth=ApiKeyAuth(api_key="crsr_test_123")))
    assert env["HARNESS_CURSOR_CLOUD_API_KEY"] == "crsr_test_123"


def test_databricks_auth_does_not_set_api_key() -> None:
    """A ``DatabricksAuth`` profile has no cursor-cloud equivalent and is ignored."""
    env = _build_cursor_cloud_spawn_env(_make_spec(auth=DatabricksAuth(profile="oss")))
    assert "HARNESS_CURSOR_CLOUD_API_KEY" not in env


def test_no_auth_no_ambient_omits_api_key() -> None:
    env = _build_cursor_cloud_spawn_env(_make_spec(auth=None))
    assert "HARNESS_CURSOR_CLOUD_API_KEY" not in env


def test_ambient_cursor_api_key_used_when_no_spec_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "crsr_ambient")
    env = _build_cursor_cloud_spawn_env(_make_spec(auth=None))
    assert env["HARNESS_CURSOR_CLOUD_API_KEY"] == "crsr_ambient"


def test_spec_api_key_wins_over_ambient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "crsr_ambient")
    env = _build_cursor_cloud_spawn_env(_make_spec(auth=ApiKeyAuth(api_key="crsr_spec")))
    assert env["HARNESS_CURSOR_CLOUD_API_KEY"] == "crsr_spec"


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_name_threads_into_agent_name_env_var() -> None:
    env = _build_cursor_cloud_spawn_env(_make_spec(name="polly"))
    assert env["HARNESS_CURSOR_CLOUD_AGENT_NAME"] == "polly"
