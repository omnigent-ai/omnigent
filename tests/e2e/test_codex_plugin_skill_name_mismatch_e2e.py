"""E2E regression test: plugin skills are exposed with inconsistent names
(``plugin:skill`` vs ``skill``).

A plugin-derived skill is installed in the shape the bug report observed at
runtime::

    <home>/.codex/plugins/cache/<marketplace>/<plugin>/<version>/skills/<skill>/SKILL.md
    <home>/.codex/skills/<skill>            (runtime-linked, BARE dir name)
    <home>/.claude/plugins/...              (manifests that namespace it <plugin>:<skill>)

On the current build the same installed skill is surfaced under two
different names depending on the session's harness family:

* a claude-family session's ``GET /v1/sessions/{id}/skills`` menu shows
  ``myplugin:brand-review`` (``_claude_plugin_skills`` namespaces it),
* a codex-family session's menu and ``$CODEX_HOME/skills/`` carry only the
  bare ``brand-review`` (``select_codex_skill_dirs`` keys by directory
  name; ``populate_codex_skills_from_bundle`` links the bare dir).

Skill lookup is exact-match everywhere (``find_skill_by_name``,
``LoadSkillTool``), so a name carried from one context into the other
fails: resolving ``myplugin:brand-review`` on a codex session 404s even
though ``CODEX_HOME/skills/brand-review`` exists, and the ``load_skill``
tool rejects the namespaced form outright.

These tests assert the FIXED contract the bug report asks for — "the name
shown to the model/user should be the same name that the Codex runtime can
load, or the resolver should accept an alias from ``plugin:skill`` to the
underlying Codex skill" — so they FAIL on the broken build and PASS once a
fix lands (the fix step's fail→pass target).

Usage::

    pytest tests/e2e/test_codex_plugin_skill_name_mismatch_e2e.py -v
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
from omnigent.runner.app import ResolvedSpec
from omnigent.spec.types import SkillSpec

_MARKETPLACE = "testmarket"
_PLUGIN = "myplugin"
_SKILL = "brand-review"
_NAMESPACED = f"{_PLUGIN}:{_SKILL}"
_SKILL_BODY_MARKER = "plugin skill body marker c7e1f4"


def _skill_md() -> str:
    """Minimal SKILL.md with valid frontmatter and a distinctive body."""
    return (
        f"---\nname: {_SKILL}\n"
        "description: Plugin-derived skill (name-mismatch repro)\n---\n\n"
        f"{_SKILL_BODY_MARKER}\n"
    )


def _seed_plugin_home(home: Path) -> None:
    """
    Install one plugin-derived skill in the observed runtime shape.

    Creates the codex plugin cache entry, the runtime-linked bare skill dir
    under ``<home>/.codex/skills/``, and the Claude plugin manifests that
    cause the skill to be exposed under the namespaced ``<plugin>:<skill>``
    name on claude-family sessions.

    :param home: The fake user home directory to populate.
    """
    # Codex plugin cache source (content-addressed plugin store).
    cache_skill = (
        home
        / ".codex"
        / "plugins"
        / "cache"
        / _MARKETPLACE
        / _PLUGIN
        / "1.0.0"
        / "skills"
        / _SKILL
    )
    cache_skill.mkdir(parents=True)
    (cache_skill / "SKILL.md").write_text(_skill_md())

    # Runtime-linked skill: BARE directory name only — the reported
    # "No corresponding .../codex-home/skills/<plugin>:<skill>".
    codex_skills = home / ".codex" / "skills"
    codex_skills.mkdir(parents=True)
    (codex_skills / _SKILL).symlink_to(cache_skill)

    # Claude plugin manifests: the source of the namespaced exposure.
    plugins_root = home / ".claude" / "plugins"
    install_rel = Path("cache") / _MARKETPLACE / _PLUGIN / "1.0.0"
    claude_skill = plugins_root / install_rel / "skills" / _SKILL
    claude_skill.mkdir(parents=True)
    (claude_skill / "SKILL.md").write_text(_skill_md())
    (plugins_root / "installed_plugins.json").write_text(
        json.dumps({"plugins": {f"{_PLUGIN}@{_MARKETPLACE}": [{"installPath": str(install_rel)}]}})
    )
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {f"{_PLUGIN}@{_MARKETPLACE}": True}})
    )


class _ExecutorStub:
    """Minimal ``ExecutorSpec`` stand-in exposing ``harness_kind``."""

    def __init__(self, harness: str) -> None:
        """:param harness: The session's harness, e.g. ``"codex-native"``."""
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
            return {"agent_id": "ag_pluginskill", "workspace": self._workspace}

    async def get(self, url: str, **kwargs: Any) -> _Response:
        """:returns: The stub snapshot response (url/kwargs ignored)."""
        del url, kwargs
        return self._Response(self._workspace)


def _make_app(harness: str, workspace: Path) -> Any:
    """
    Build a runner app whose spec resolver returns a stub spec.

    :param harness: The session's harness id, e.g. ``"codex-native"``.
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
async def test_codex_session_resolves_plugin_namespaced_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A codex session must resolve the namespaced ``plugin:skill`` name.

    The user journey: a plugin-derived skill is installed (plugin cache +
    ``CODEX_HOME/skills/<skill>`` link), the skill is referred to by its
    namespaced form ``myplugin:brand-review`` (the name claude-family
    surfaces expose for the very same installed skill), and the codex
    session's skill resolver — the endpoint behind the composer's slash
    command — must accept it.

    On the broken build this 404s (exact-match lookup, only the bare
    ``brand-review`` is exposed), which is exactly the reported failure:
    "a context can refer to ``plugin:skill``, while the Codex runtime has
    only ``CODEX_HOME/skills/skill``".
    """
    home = tmp_path / "home"
    _seed_plugin_home(home)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Sanity: a claude-family session exposes this installed skill under the
    # namespaced name — the name a user/model then carries into codex context.
    claude_app = _make_app("claude-sdk", workspace)
    async for c in _client(claude_app):
        claude_menu = await c.get("/v1/sessions/conv_claude_ps/skills")
    claude_names = [s["name"] for s in claude_menu.json()["skills"]]
    assert _NAMESPACED in claude_names, (
        f"precondition: claude-family menu should namespace the plugin skill; got {claude_names}"
    )

    codex_app = _make_app("codex-native", workspace)
    async for c in _client(codex_app):
        menu = await c.get("/v1/sessions/conv_codex_ps/skills")
        resolved = await c.post(
            "/v1/sessions/conv_codex_ps/skills/resolve",
            json={"name": _NAMESPACED, "arguments": ""},
        )

    exposed = [s["name"] for s in menu.json()["skills"]]
    # Sanity: the skill IS installed and surfaced for the codex session.
    assert any(_SKILL in name for name in exposed), (
        f"precondition: installed plugin skill missing from codex menu; got {exposed}"
    )

    # THE BUG: the namespaced form other surfaces expose for the
    # same installed skill must resolve — either because codex exposes the
    # matching name or because the resolver accepts the alias. On the broken
    # build this is a 404 with the bare name in `available`.
    assert resolved.status_code == 200, (
        f"codex session rejected the plugin-namespaced skill name "
        f"{_NAMESPACED!r} even though the skill is installed and linked as "
        f"CODEX_HOME/skills/{_SKILL}. Response: {resolved.status_code} "
        f"{resolved.json()}"
    )
    assert _SKILL_BODY_MARKER in resolved.json()["meta_text"]


@pytest.mark.asyncio
async def test_codex_home_and_exposed_name_agree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The name shown to the model/user must be loadable by the codex runtime.

    ``populate_codex_skills_from_bundle`` decides what the actual codex
    runtime can load (``CODEX_HOME/skills/<dir>``). Whatever name any
    Omnigent surface exposes for the installed plugin skill (claude-family
    menus namespace it ``<plugin>:<skill>``), the codex runtime must be able
    to load that same name — the reported contract's "the name shown to the model/user
    should be the same name that the Codex runtime can load, or the resolver
    should accept an alias".

    On the broken build the claude-family surface exposes
    ``myplugin:brand-review`` while ``CODEX_HOME/skills/`` contains only
    ``brand-review``, and the codex resolver rejects the namespaced form —
    so the exposed name is dead on codex.
    """
    from omnigent.inner.codex_executor import populate_codex_skills_from_bundle
    from omnigent.spec.skill_sources import SkillSourceContext, resolve_harness_skills

    home = tmp_path / "home"
    _seed_plugin_home(home)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # What codex can actually load, per the shared populate helper.
    codex_home = tmp_path / "codex-home"
    populate_codex_skills_from_bundle(codex_home, None, "all")
    loadable = {p.name for p in (codex_home / "skills").iterdir()}
    assert _SKILL in loadable  # sanity: the plugin skill is linked

    # Every name a harness surface exposes for this installed skill…
    ctx = SkillSourceContext(roots=(workspace,), home=home, skills_filter="all", bundle_dir=None)
    exposed_names = {
        s.name
        for harness in ("claude-sdk", "codex-native")
        for s in resolve_harness_skills(ctx, harness)
        if _SKILL in s.name
    }
    assert exposed_names, "precondition: the plugin skill is exposed by some surface"

    # …must be loadable by the codex runtime (same name, or an alias the
    # resolver accepts — checked via the resolver endpoint).
    app = _make_app("codex-native", workspace)
    dead_names: dict[str, Any] = {}
    for name in sorted(exposed_names):
        if name in loadable:
            continue
        async for c in _client(app):
            resp = await c.post(
                "/v1/sessions/conv_codex_ps2/skills/resolve",
                json={"name": name, "arguments": ""},
            )
        if resp.status_code != 200:
            dead_names[name] = resp.json()

    assert not dead_names, (
        f"these exposed names for the installed plugin skill are "
        f"neither present in CODEX_HOME/skills ({sorted(loadable)}) nor "
        f"accepted as an alias by the codex skill resolver: {dead_names}"
    )
