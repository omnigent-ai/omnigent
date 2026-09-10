"""E2E regression test: the web UI's skill menu diverges from the Claude
terminal's loaded skills.

For a claude-family session the web composer's slash-command menu is fed by
``GET /v1/sessions/{id}/skills`` (``_resolve_session_skills`` →
``resolve_harness_skills``), while the embedded terminal's menu is whatever
the real Claude Code CLI discovers itself. Live-verified against Claude Code
v2.1.212, the two disagree in both directions:

* the web menu surfaces ``<workspace>/.agents/skills/<skill>`` entries (the
  generic host walk scans ``.agents``), but Claude Code does not read
  ``.agents/skills`` — so the menu lists commands the terminal cannot
  invoke (a native session sends ``/name`` to the CLI as plaintext; there
  is no server-side resolve+inject on that path), and
* Claude Code lists user-tier skills from ``$CLAUDE_CONFIG_DIR/skills``
  (defaulting to ``~/.claude/skills``), but the web resolution reads only
  ``Path.home()/.claude/skills`` — so with a non-default config dir the
  terminal shows skills the web menu omits.

These tests assert the FIXED parity contract — the claude-family web menu
lists exactly what the Claude terminal can load — so they FAIL on the broken
build and PASS once a fix lands (the fix step's fail→pass target).

Usage::

    pytest tests/e2e/test_claude_terminal_web_skills_parity_e2e.py -v
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

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
    Seed the two workspace skill tiers the bug diverges on.

    ``.claude/skills`` is read by both the Claude Code terminal and the web
    resolution; ``.agents/skills`` is read ONLY by the web resolution's
    generic host walk (live-verified: Claude Code v2.1.212's slash menu does
    not list it).

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
        _skill_md(_AGENTS_ONLY_SKILL, "workspace .agents skill (web-only today)")
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


async def _menu_names(app: Any, session_id: str) -> list[str]:
    """
    Fetch the composer skill menu for *session_id* from the runner app.

    :param app: The runner FastAPI app.
    :param session_id: Session id to query (unique per call — the runner
        caches per-session skill resolutions).
    :returns: The skill names ``GET /v1/sessions/{id}/skills`` returned.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
        resp = await c.get(f"/v1/sessions/{session_id}/skills")
    assert resp.status_code == 200, resp.text
    return [s["name"] for s in resp.json()["skills"]]


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
    """
    The claude-family web menu must not list ``.agents/skills`` entries.

    The user journey: a workspace carries skills under both
    ``.claude/skills/`` and ``.agents/skills/``; the user opens the web
    composer's slash menu and the embedded Claude terminal's slash menu for
    the same claude-native session and compares them. Live-verified on
    Claude Code v2.1.212: the terminal lists only the ``.claude/skills``
    skill; the web menu additionally lists the ``.agents/skills`` one, and
    selecting it sends ``/agents-only-skill`` to a CLI that has no such
    command.

    On the broken build the web menu includes ``agents-only-skill`` —
    exactly the reported "loaded skills are different between claude
    terminal and web ui".
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _seed_workspace(workspace)

    app = _make_app("claude-native", workspace)
    names = await _menu_names(app, "conv_claude_parity_ws")

    # Precondition (passes on the broken build too): the tier both surfaces
    # agree on is listed.
    assert _CLAUDE_DIR_SKILL in names, (
        f"precondition: workspace .claude/skills skill missing from menu; got {names}"
    )

    # THE BUG: Claude Code does not discover ``.agents/skills`` (verified
    # live against its slash menu), so surfacing it in the web menu lists a
    # command the terminal cannot invoke.
    assert _AGENTS_ONLY_SKILL not in names, (
        f"web menu for a claude session lists {_AGENTS_ONLY_SKILL!r} from "
        f".agents/skills, which the Claude Code terminal does not load — the "
        f"two surfaces show different skills. Menu: {names}"
    )


@pytest.mark.asyncio
async def test_claude_web_menu_sources_user_skills_from_claude_config_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The claude-family web menu must honor ``CLAUDE_CONFIG_DIR`` user skills.

    The Claude Code terminal loads user-tier skills from
    ``$CLAUDE_CONFIG_DIR/skills`` (live-verified: its slash menu labels them
    "(user)"), while the web resolution reads only
    ``Path.home()/.claude/skills``. With a non-default config dir the
    terminal therefore lists a skill the web menu omits — the other
    direction of the reported divergence.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    cfg = tmp_path / "claude-config"
    user_skill = cfg / "skills" / _USER_CFG_SKILL
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text(
        _skill_md(_USER_CFG_SKILL, "user config-dir skill (terminal-only today)")
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _seed_workspace(workspace)

    app = _make_app("claude-native", workspace)
    names = await _menu_names(app, "conv_claude_parity_cfg")

    # THE BUG (other direction): the terminal's slash menu lists this skill
    # as "(user)"; the web menu must list it too or the surfaces diverge.
    assert _USER_CFG_SKILL in names, (
        f"Claude terminal loads user skills from $CLAUDE_CONFIG_DIR/skills "
        f"({cfg / 'skills'}), but the web menu omits {_USER_CFG_SKILL!r} — "
        f"the two surfaces show different skills. Menu: {names}"
    )
