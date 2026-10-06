"""Claude's web menu includes native and bridged portable skills.

Omnigent exposes ``.agents/skills`` to Claude through an additional directory,
alongside the CLI's native ``.claude/skills`` and configured user skill tiers.
Host discovery must report the same commands.

Usage::

    pytest tests/e2e/test_claude_terminal_web_skills_parity_e2e.py -v
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.host.frames import HostSkillsFrame
from omnigent.host.skills import HostSkillDiscovery
from omnigent.runner import create_runner_app
from omnigent.runner.app import ResolvedSpec
from omnigent.spec.types import SkillSpec

_CLAUDE_DIR_SKILL = "claude-dir-skill"
_AGENTS_ONLY_SKILL = "agents-only-skill"
_USER_CFG_SKILL = "user-cfg-skill"


def _skill_md(name: str, description: str) -> str:
    """Minimal SKILL.md with valid frontmatter.

    :param name: Frontmatter skill name (matches its directory name).
    :param description: One-line human description.
    :returns: The SKILL.md contents.
    """
    return f"---\nname: {name}\ndescription: {description}\n---\n\nBody of {name}.\n"


def _seed_workspace(workspace: Path) -> None:
    """
    Seed native and portable workspace skills.

    :param workspace: The session workspace directory to populate.
    """
    claude_skill = workspace / ".claude" / "skills" / _CLAUDE_DIR_SKILL
    claude_skill.mkdir(parents=True)
    (claude_skill / "SKILL.md").write_text(
        _skill_md(_CLAUDE_DIR_SKILL, "workspace .claude skill (both surfaces)")
    )
    agents_skill = workspace / ".agents" / "skills" / _AGENTS_ONLY_SKILL
    agents_skill.mkdir(parents=True)
    (agents_skill / "SKILL.md").write_text(
        _skill_md(_AGENTS_ONLY_SKILL, "workspace .agents skill")
    )


class _ExecutorStub:
    """Minimal ``ExecutorSpec`` stand-in exposing ``harness_kind``."""

    def __init__(self, harness: str) -> None:
        """:param harness: The session's harness, e.g. ``"claude-native"``."""
        self.harness_kind = harness


class _SpecStub:
    """Minimal ``AgentSpec`` stand-in for runner skill discovery."""

    def __init__(self, harness: str) -> None:
        """:param harness: Harness id driving per-harness skill discovery."""
        self.skills: list[SkillSpec] = []
        self.skills_filter: str = "all"
        self.executor = _ExecutorStub(harness)


class _ServerClient:
    """Fake Omnigent server client returning a fixed session snapshot."""

    def __init__(self, workspace: str) -> None:
        """:param workspace: Session workspace path to report."""
        self._workspace = workspace

    class _Response:
        """Stub 200 snapshot response with an agent_id + workspace."""

        def __init__(self, workspace: str) -> None:
            """:param workspace: Workspace path to include in the body."""
            self.status_code = 200
            self._workspace = workspace

        def json(self) -> dict[str, Any]:
            """:returns: A minimal session snapshot."""
            return {"agent_id": "ag_skillparity", "workspace": self._workspace}

    async def get(self, url: str, **kwargs: Any) -> _Response:
        """:returns: The stub snapshot response (url/kwargs ignored)."""
        del url, kwargs
        return self._Response(self._workspace)


def _make_app(harness: str, workspace: Path) -> Any:
    """
    Build a runner app whose spec resolver returns a stub spec.

    :param harness: The session's harness id, e.g. ``"claude-native"``.
    :param workspace: Session workspace (host-skill discovery root).
    :returns: The configured runner FastAPI app.
    """
    entry = ResolvedSpec(spec=_SpecStub(harness), workdir=workspace)

    async def _spec_resolver(agent_id: str, session_id: str | None) -> Any:
        """Return the stub resolved spec."""
        del agent_id, session_id
        return entry

    return create_runner_app(
        spec_resolver=_spec_resolver,
        server_client=_ServerClient(str(workspace)),  # type: ignore[arg-type]
    )


def _menu_names(harness: str, workspace: Path) -> list[str]:
    """Read the menu catalog on the host, independently of invocation."""

    def unexpected_bundle(_: HostSkillsFrame) -> httpx.Response:
        raise AssertionError("Directory discovery must not fetch a session bundle")

    discovery = HostSkillDiscovery(unexpected_bundle)
    return [
        s["name"]
        for s in discovery.discover(HostSkillsFrame("menu", harness, str(workspace)), workspace)
    ]


async def _client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    """
    Yield an httpx client bound to the runner app over ASGI.

    :param app: The runner FastAPI app.
    :returns: Async iterator yielding the client.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
        yield c


@pytest.mark.asyncio
async def test_claude_web_menu_lists_only_terminal_loadable_workspace_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The menu includes portable skills exposed by the native launch bridge."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _seed_workspace(workspace)

    names = _menu_names("claude-native", workspace)

    assert set(names) == {_CLAUDE_DIR_SKILL, _AGENTS_ONLY_SKILL}


@pytest.mark.asyncio
async def test_claude_web_menu_sources_user_skills_from_claude_config_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The menu honors ``CLAUDE_CONFIG_DIR`` alongside portable skill bridging."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    cfg = tmp_path / "claude-config"
    user_skill = cfg / "skills" / _USER_CFG_SKILL
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text(_skill_md(_USER_CFG_SKILL, "user config-dir skill"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _seed_workspace(workspace)

    names = _menu_names("claude-native", workspace)

    assert set(names) == {_CLAUDE_DIR_SKILL, _AGENTS_ONLY_SKILL, _USER_CFG_SKILL}
