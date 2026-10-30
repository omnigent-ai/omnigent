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
