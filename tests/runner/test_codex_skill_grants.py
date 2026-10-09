"""Session-owned Codex skill grants across runner launch and tool dispatch."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from omnigent.entities import DEFAULT_ENVIRONMENT_ID
from omnigent.inference_config import inference_config_scope
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import CallerProcessOSEnvironment
from omnigent.runner import create_runner_app
from omnigent.runner.app import _build_spawn_env_from_spec
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.runner.tool_dispatch import _execute_os_env_tool
from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.runtime.harnesses.process_manager import (
    HarnessProcessManager,
    _build_harness_spawn_env,
)
from omnigent.spec.types import AgentSpec, ApiKeyAuth, ExecutorSpec
from tests.runner.helpers import NullServerClient


def _agent(workspace: Path, skills_filter: str | list[str] = "all") -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="scoped-codex",
        skills_filter=skills_filter,
        os_env=OSEnvSpec(
            cwd=str(workspace), sandbox=OSEnvSandboxSpec(type="none", write_paths=["."])
        ),
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "codex"},
            model="test-model",
            auth=ApiKeyAuth(api_key="synthetic-test-key", base_url="http://127.0.0.1:1/v1"),
        ),
    )


@pytest.mark.asyncio
async def test_registry_skills_grants_are_private_and_session_scoped(tmp_path: Path) -> None:
    registry = SessionResourceRegistry(runner_workspace=tmp_path)
    agent = _agent(tmp_path)
    try:
        first = registry.resolve_environment("first", DEFAULT_ENVIRONMENT_ID, agent)
        second = registry.resolve_environment("second", DEFAULT_ENVIRONMENT_ID, agent)
        assert isinstance(first, CallerProcessOSEnvironment)
        assert isinstance(second, CallerProcessOSEnvironment)
        first_root = registry.codex_skills_dir("first")
        second_root = registry.codex_skills_dir("second")
        first_identity = first_root.stat().st_ino

        assert first_root != second_root
        assert first_root == first_root.resolve()
        assert first_root.is_dir() and not first_root.is_symlink()
        assert not first_root.is_relative_to(tmp_path)
        if os.name != "nt":
            assert stat.S_IMODE(first_root.stat().st_mode) == 0o700
        assert first.sandbox.read_roots == [first_root]
        assert second.sandbox.read_roots == [second_root]
        assert registry.resolve_environment("first", DEFAULT_ENVIRONMENT_ID, agent) is first
        assert registry.codex_skills_dir("first").stat().st_ino == first_identity
        assert agent.os_env is not None and agent.os_env.sandbox is not None
        assert agent.os_env.sandbox.read_paths is None
    finally:
        await registry.cleanup_session("first")
        await registry.cleanup_session("second")


@pytest.mark.asyncio
async def test_cached_environment_grants_skills_populated_after_first_use(tmp_path: Path) -> None:
    registry = SessionResourceRegistry(runner_workspace=tmp_path)
    agent = _agent(tmp_path)
    (tmp_path / "README.md").write_text("workspace")
    try:
        environment = registry.resolve_environment("session", DEFAULT_ENVIRONMENT_ID, agent)
        assert (await environment.read("README.md"))["content"] == "workspace"
        skills_root = registry.codex_skills_dir("session")
        assert list(skills_root.iterdir()) == []
        (skills_root / "SKILL.md").write_text("staged after helper startup")

        cached = registry.resolve_environment("session", DEFAULT_ENVIRONMENT_ID, agent)
        assert cached is environment
        assert (await cached.read(str(skills_root / "SKILL.md")))["content"] == (
            "staged after helper startup"
        )
        denied = await cached.write(str(skills_root / "SKILL.md"), "must not overwrite")
        assert "error" in denied
        assert (skills_root / "SKILL.md").read_text() == "staged after helper startup"
    finally:
        await registry.cleanup_session("session")


@pytest.mark.asyncio
@pytest.mark.parametrize("skills_filter", ["all", "none", ["implementation-planning"]])
async def test_codex_spawn_transports_the_cached_environments_root(
    tmp_path: Path, skills_filter: str | list[str]
) -> None:
    registry = SessionResourceRegistry(runner_workspace=tmp_path)
    agent = _agent(tmp_path, skills_filter)
    original = deepcopy(agent)
    try:
        environment = registry.resolve_environment("session", DEFAULT_ENVIRONMENT_ID, agent)
        assert isinstance(environment, CallerProcessOSEnvironment)
        skills_root = registry.codex_skills_dir("session")
        with inference_config_scope({}):
            first_env = _build_spawn_env_from_spec(
                agent, "codex", session_id="session", resource_registry=registry, cwd=tmp_path
            )
            restarted_env = _build_spawn_env_from_spec(
                agent, "codex", session_id="session", resource_registry=registry, cwd=tmp_path
            )
        assert first_env is not None and restarted_env is not None
        assert first_env["HARNESS_CODEX_SKILLS_DIR"] == str(skills_root)
        assert restarted_env["HARNESS_CODEX_SKILLS_DIR"] == str(skills_root)
        assert json.loads(first_env["HARNESS_CODEX_SKILLS_FILTER"]) == skills_filter
        assert environment.sandbox.read_roots == [skills_root]
        assert agent == original
    finally:
        await registry.cleanup_session("session")


def test_codex_spawn_without_registry_cannot_inherit_a_parent_skill_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_CODEX_SKILLS_DIR", "/parent-session-skills")
    with inference_config_scope({}):
        env = _build_spawn_env_from_spec(_agent(tmp_path), "codex", session_id="unowned")
    assert env is not None
    assert env["HARNESS_CODEX_SKILLS_DIR"] == ""
    assert _build_harness_spawn_env(env)["HARNESS_CODEX_SKILLS_DIR"] == ""


@pytest.mark.asyncio
async def test_direct_file_tools_read_only_their_sessions_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = SessionResourceRegistry(runner_workspace=tmp_path)
    first_root = registry.codex_skills_dir("first")
    second_root = registry.codex_skills_dir("second")
    first_skill = first_root / "SKILL.md"
    second_skill = second_root / "SKILL.md"
    first_skill.write_text("first-session skill")
    second_skill.write_text("second-session skill")
    monkeypatch.setenv("HARNESS_CODEX_SKILLS_DIR", str(first_root))
    try:
        own = json.loads(
            await _execute_os_env_tool(
                "sys_os_read",
                {"path": str(first_skill)},
                agent_spec=_agent(tmp_path),
                conversation_id="first",
                resource_registry=registry,
            )
        )
        other = json.loads(
            await _execute_os_env_tool(
                "sys_os_read",
                {"path": str(first_skill)},
                agent_spec=_agent(tmp_path, "none"),
                conversation_id="second",
                resource_registry=registry,
            )
        )
        fallback = json.loads(
            await _execute_os_env_tool(
                "sys_os_read",
                {"path": str(first_skill)},
                agent_spec=_agent(tmp_path),
                conversation_id="first",
            )
        )
        denied_write = json.loads(
            await _execute_os_env_tool(
                "sys_os_write",
                {"path": str(first_skill), "content": "overwrite"},
                agent_spec=_agent(tmp_path),
                conversation_id="first",
                resource_registry=registry,
            )
        )
        assert own["content"] == "first-session skill"
        assert "error" in other
        assert "error" in fallback
        assert "error" in denied_write
        assert first_skill.read_text() == "first-session skill"
    finally:
        await registry.cleanup_session("first")
        await registry.cleanup_session("second")


@pytest.mark.asyncio
async def test_cleanup_only_removes_the_owning_sessions_skills(tmp_path: Path) -> None:
    registry = SessionResourceRegistry(runner_workspace=tmp_path)
    first_root = registry.codex_skills_dir("first")
    second_root = registry.codex_skills_dir("second")
    (first_root / "SKILL.md").write_text("first")
    (second_root / "SKILL.md").write_text("second")
    workspace_file = tmp_path / "README.md"
    workspace_file.write_text("preserved workspace")
    try:
        await registry.cleanup_session("first")
        assert not first_root.exists()
        assert (second_root / "SKILL.md").read_text() == "second"
        assert workspace_file.read_text() == "preserved workspace"
        await registry.cleanup_session("first")
    finally:
        await registry.cleanup_session("second")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "suffix"), [("POST", "/reset-state"), ("DELETE", "/resources"), ("DELETE", "")]
)
async def test_cleanup_releases_codex_before_removing_its_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, suffix: str
) -> None:
    """Reset/delete retires the cached process before its skills become invalid."""
    monkeypatch.setitem(_HARNESS_MODULES, "codex", "tests.runtime.harnesses._test_harness")
    registry = SessionResourceRegistry(runner_workspace=tmp_path)
    old_root = registry.codex_skills_dir("owner")
    other_root = registry.codex_skills_dir("other")
    agent = _agent(tmp_path)
    with tempfile.TemporaryDirectory(
        prefix="og-skills-", dir="/tmp" if sys.platform == "darwin" else None
    ) as process_dir:
        manager = HarnessProcessManager(tmp_parent=Path(process_dir))
        await manager.start(sweep_orphans=False)
        original_cleanup = registry.cleanup_session
        try:
            with inference_config_scope({}):
                old_env = _build_spawn_env_from_spec(
                    agent, "codex", cwd=tmp_path, session_id="owner", resource_registry=registry
                )
            old_client = await manager.get_client("owner", "codex", old_env)
            old_process = manager._entries["owner"].process
            old_pid = (await old_client.get("/pid")).json()["pid"]
            other_client = await manager.get_client("other", "codex")
            other_pid = (await other_client.get("/pid")).json()["pid"]

            async def cleanup_after_release(session_id: str) -> None:
                assert session_id == "owner"
                assert old_root.is_dir(), "skills must remain until harness teardown completes"
                assert old_process.returncode is not None, "cached harness was not released"
                assert not manager.has_session(session_id)
                await original_cleanup(session_id)

            monkeypatch.setattr(registry, "cleanup_session", cleanup_after_release)
            app = create_runner_app(
                process_manager=manager,
                server_client=NullServerClient(),  # type: ignore[arg-type]
                resource_registry=registry,
                runner_workspace=tmp_path,
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://runner"
            ) as client:
                result = await client.request(method, f"/v1/sessions/owner{suffix}")
            assert result.status_code == 200, result.text
            assert not old_root.exists()
            assert other_root.is_dir()
            assert (await other_client.get("/pid")).json()["pid"] == other_pid

            with inference_config_scope({}):
                new_env = _build_spawn_env_from_spec(
                    agent, "codex", cwd=tmp_path, session_id="owner", resource_registry=registry
                )
            new_root = registry.codex_skills_dir("owner")
            assert new_root != old_root
            new_client = await manager.get_client("owner", "codex", new_env)
            assert (await new_client.get("/pid")).json()["pid"] != old_pid
            assert (await new_client.get("/env/HARNESS_CODEX_SKILLS_DIR")).json() == {
                "value": str(new_root)
            }
        finally:
            await manager.shutdown()
            await original_cleanup("owner")
            await original_cleanup("other")
