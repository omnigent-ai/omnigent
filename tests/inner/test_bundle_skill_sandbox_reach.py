"""A bundle skill's own files must stay readable from a sandboxed tool.

Runners extract an agent bundle under the temp directory, and ``linux_bwrap``
mounts a private tmpfs over ``/tmp``, so a skill's ``scripts/`` and
references were invisible to sandboxed shell and file tools unless the skill
directory is granted read-only.
"""

from __future__ import annotations

import shlex
import tempfile
from pathlib import Path

import pytest

from omnigent.entities import DEFAULT_ENVIRONMENT_ID
from omnigent.inner.bundle_skills import bundle_resource_roots, skill_resource_roots
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.spec.types import AgentSpec, SkillSpec
from tests.e2e._harness_probes import bwrap_namespace_unavailable_reason


def _bundle_with_skill(root: Path) -> tuple[Path, SkillSpec]:
    """A bundle as a runner extracts it, with one skill that ships a script."""
    skill_dir = root / "bundle" / "skills" / "pdf"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: pdf\ndescription: PDF\n---\nRun it.\n")
    (skill_dir / "scripts" / "run.sh").write_text("echo skill-script-ran\n")
    return root / "bundle", SkillSpec(
        name="pdf", description="PDF", content="Run it.", skill_dir=skill_dir
    )


def test_resource_roots_name_the_skill_and_plugin_directories(tmp_path: Path) -> None:
    bundle, skill = _bundle_with_skill(tmp_path)
    assert skill_resource_roots([skill, skill, SkillSpec("mem", "d", "c")]) == [skill.skill_dir]
    assert bundle_resource_roots(bundle) == [bundle / "skills"]
    (bundle / ".claude-plugin").mkdir()
    assert bundle_resource_roots(bundle) == [bundle / "skills", bundle / ".claude-plugin"]
    assert bundle_resource_roots(None) == []


def test_the_primary_environment_grants_the_specs_skill_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from omnigent.inner import os_env
    from omnigent.inner.sandbox import SandboxPolicy

    _bundle, skill = _bundle_with_skill(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    created: list[_RecordingEnvironment] = []

    def create(spec: object, *, additional_read_roots: list[Path] = (), **_kwargs: object):  # type: ignore[no-untyped-def]
        policy = SandboxPolicy(
            backend_type="linux_bwrap",
            active=True,
            read_roots=list(additional_read_roots),
            write_roots=[workspace],
            write_files=[],
            allow_network=False,
            copy_on_write_roots=[workspace],
        )
        created.append(_RecordingEnvironment(policy))
        return created[-1]

    monkeypatch.setattr(os_env, "create_os_environment", create)
    copy_on_write = OSEnvSpec(
        type="caller_process",
        cwd=str(workspace),
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap", write_paths=[{"path": ".", "copy_on_write": True}]
        ),
    )
    agent = AgentSpec(spec_version=1, name="skills", os_env=copy_on_write, skills=[skill])
    registry = SessionResourceRegistry(runner_workspace=workspace)
    env = registry.resolve_environment("s1", DEFAULT_ENVIRONMENT_ID, agent)
    base_roots = list(created[0].base.read_roots or [])
    assert skill.skill_dir in env.sandbox.read_roots

    # An updated skill extracts elsewhere: the same copy-on-write environment
    # now reads the new directory, and no longer the old one.
    moved = tmp_path / "bundle-v2" / "skills" / "pdf"
    moved.mkdir(parents=True)
    updated = replace(agent, skills=[replace(skill, skill_dir=moved)])
    again = registry.resolve_environment("s1", DEFAULT_ENVIRONMENT_ID, updated)
    assert again is env and len(created) == 1
    assert moved in env.sandbox.read_roots and skill.skill_dir not in env.sandbox.read_roots
    assert set(base_roots) <= set(env.sandbox.read_roots)
    assert env.sandbox.copy_on_write_roots == [workspace]
    replaced = len(env.policies)
    registry.resolve_environment("s1", DEFAULT_ENVIRONMENT_ID, updated)
    assert len(env.policies) == replaced  # unchanged skills: the helper is left running


class _RecordingEnvironment:
    """An environment stand-in that records each sandbox policy it is given."""

    def __init__(self, policy: object) -> None:
        self.base = policy
        self.sandbox = policy
        self.policies: list[object] = []

    def replace_sandbox_policy(self, policy: object) -> None:
        self.sandbox = policy
        self.policies.append(policy)


@pytest.mark.skipif(
    bwrap_namespace_unavailable_reason() is not None,
    reason=bwrap_namespace_unavailable_reason() or "",
)
@pytest.mark.asyncio
async def test_a_sandboxed_shell_runs_a_bundle_skills_script() -> None:
    # The bundle must sit under the temp directory, as the runner extracts it.
    with tempfile.TemporaryDirectory(prefix="runner-specs-test-") as tmp:
        root = Path(tmp)
        _bundle, skill = _bundle_with_skill(root)
        workspace = root / "ws"
        workspace.mkdir()
        agent = AgentSpec(
            spec_version=1,
            name="skills",
            os_env=OSEnvSpec(
                type="caller_process",
                cwd=str(workspace),
                sandbox=OSEnvSandboxSpec(
                    type="linux_bwrap",
                    write_paths=["."],
                    # An editable install keeps omnigent outside the venv; the helper imports it.
                    read_paths=[str(Path(__file__).resolve().parents[2])],
                ),
            ),
            skills=[skill],
        )
        registry = SessionResourceRegistry(runner_workspace=workspace)
        env = registry.resolve_environment("s1", DEFAULT_ENVIRONMENT_ID, agent)
        script = skill.skill_dir / "scripts" / "run.sh"  # type: ignore[operator]
        ran = await env.shell(shlex.join(["sh", str(script)]), timeout=30)
        assert ran.get("exit_code") == 0, ran
        assert "skill-script-ran" in str(ran.get("stdout", ""))
        # Granted read-only: the sandbox cannot rewrite the skill.
        wrote = await env.shell(f"echo x > {shlex.quote(str(script))}", timeout=30)
        assert wrote.get("exit_code") not in (None, 0), wrote


def test_a_terminal_sandbox_gets_the_read_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Terminals build their own environment, so forward the skill read roots.
    from omnigent.inner import terminal as terminal_mod
    from omnigent.inner.datamodel import TerminalEnvSpec

    _bundle, skill = _bundle_with_skill(tmp_path)
    granted: list[list[Path]] = []
    real_create = terminal_mod.create_os_environment

    def capture(spec: object, **kwargs: object):  # type: ignore[no-untyped-def]
        granted.append(list(kwargs.get("additional_read_roots", ())))  # type: ignore[arg-type]
        return real_create(spec, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(terminal_mod, "_require_supported_tmux", lambda: None)
    monkeypatch.setattr(terminal_mod, "create_os_environment", capture)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = terminal_mod.create_terminal_instance(
        name="bash",
        session_key="s1",
        spec=TerminalEnvSpec(command="bash"),
        parent_os_env_spec=OSEnvSpec(type="caller_process", cwd=str(workspace)),
        additional_read_roots=[skill.skill_dir],  # type: ignore[list-item]
    )
    try:
        assert granted == [[skill.skill_dir]]
    finally:
        import shutil

        shutil.rmtree(result.instance.private_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_sys_terminal_and_native_terminals_pass_the_agents_skill_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import json

    from omnigent.inner.datamodel import TerminalEnvSpec
    from omnigent.runner.native import _agent_skill_root_kwargs
    from omnigent.terminals import TerminalRegistry
    from omnigent.tools.base import ToolContext
    from omnigent.tools.builtins.sys_terminal import SysTerminalLaunchTool

    _bundle, skill = _bundle_with_skill(tmp_path)
    seen: list[object] = []

    async def capture(*_args: object, **kwargs: object) -> object:
        seen.append(kwargs.get("additional_read_roots"))
        raise RuntimeError("stop after the launch arguments are known")

    registry = TerminalRegistry()
    monkeypatch.setattr(registry, "launch", capture)
    agent = AgentSpec(
        spec_version=1,
        name="skills",
        terminals={"bash": TerminalEnvSpec(command="bash")},
        skills=[skill],
    )
    tool = SysTerminalLaunchTool(spec=agent, registry=registry)
    ctx = ToolContext(task_id="t", agent_id="a", workspace=tmp_path, conversation_id="c" * 32)
    raw = await asyncio.to_thread(
        tool.invoke, json.dumps({"terminal": "bash", "session": "s1"}), ctx
    )
    assert "launch failed" in json.loads(raw)["error"]
    assert seen == [[skill.skill_dir]]

    # The runner's native and HTTP terminal launches forward the agent's skill directories too.
    assert _agent_skill_root_kwargs(agent) == {"additional_read_roots": [skill.skill_dir]}
    resources = SessionResourceRegistry(terminal_registry=registry)
    with pytest.raises(RuntimeError, match="stop after"):
        await resources.launch_auxiliary_terminal(
            session_id="s1",
            terminal_name="bash",
            session_key="main",
            spec=TerminalEnvSpec(command="bash"),
            **_agent_skill_root_kwargs(agent),
        )
    assert seen[-1] == [skill.skill_dir]


@pytest.mark.asyncio
async def test_a_dispatched_os_tool_grants_the_specs_skill_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The non-shared branch of runner tool dispatch builds its own environment.
    from omnigent.inner import os_env
    from omnigent.runner.tool_dispatch import _execute_os_env_tool

    _bundle, skill = _bundle_with_skill(tmp_path)
    granted: list[list[Path]] = []

    def capture(spec: object, *, additional_read_roots: list[Path] = (), **_kwargs: object):  # type: ignore[no-untyped-def]
        granted.append(list(additional_read_roots))
        return

    monkeypatch.setattr(os_env, "create_os_environment", capture)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    agent = AgentSpec(
        spec_version=1,
        name="skills",
        os_env=OSEnvSpec(type="caller_process", cwd=str(workspace)),
        skills=[skill],
    )
    result = await _execute_os_env_tool(
        "sys_os_read", {"path": str(skill.skill_dir)}, agent_spec=agent, conversation_id="c1"
    )
    assert "unable to create" in result
    assert granted == [[skill.skill_dir]]


@pytest.mark.asyncio
async def test_a_replaced_policy_restarts_the_helper_and_keeps_its_copy_on_write_paths(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from omnigent.inner.os_env import create_os_environment
    from omnigent.inner.sandbox import with_additional_read_roots

    env = create_os_environment(
        OSEnvSpec(type="caller_process", cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert env is not None
    try:
        assert (await env.shell("echo before", timeout=30)).get("exit_code") == 0
        granted = with_additional_read_roots(env.sandbox, [tmp_path / "skills"])
        env.replace_sandbox_policy(granted)
        assert env.sandbox is granted
        assert (await env.shell("echo after", timeout=30)).get("exit_code") == 0
        with pytest.raises(ValueError, match="copy-on-write"):
            env.replace_sandbox_policy(replace(granted, copy_on_write_roots=[tmp_path]))
    finally:
        env.close()
