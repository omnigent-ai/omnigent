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
    from omnigent.inner import os_env

    _bundle, skill = _bundle_with_skill(tmp_path)
    granted: list[list[Path]] = []

    def capture(spec: object, *, additional_read_roots: list[Path] = (), **_kwargs: object):  # type: ignore[no-untyped-def]
        granted.append(list(additional_read_roots))
        return object()

    monkeypatch.setattr(os_env, "create_os_environment", capture)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    agent = AgentSpec(
        spec_version=1,
        name="skills",
        os_env=OSEnvSpec(type="caller_process", cwd=str(workspace)),
        skills=[skill],
    )
    SessionResourceRegistry(runner_workspace=workspace).resolve_environment(
        "s1", DEFAULT_ENVIRONMENT_ID, agent
    )
    assert skill.skill_dir in granted[0]


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
    from omnigent.runner.native.orchestration import _agent_skill_roots
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

    # The runner's native terminals forward the agent's skill directories too.
    assert _agent_skill_roots(agent) == [skill.skill_dir]
    resources = SessionResourceRegistry(terminal_registry=registry)
    with pytest.raises(RuntimeError, match="stop after"):
        await resources.launch_auxiliary_terminal(
            session_id="s1",
            terminal_name="bash",
            session_key="main",
            spec=TerminalEnvSpec(command="bash"),
            additional_read_roots=_agent_skill_roots(agent),
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
