"""
Runner-owned skill discovery + resolution endpoints.

Skills are resolved on the runner, not the Omnigent server, because the
runner is where the harness executes and may read a skill's local
resource files. These tests exercise the two runner endpoints the AP
server delegates to:

* ``GET /v1/sessions/{id}/skills`` — the merged (bundled + host) skill
  list for the web composer's slash-command menu.
* ``POST /v1/sessions/{id}/skills/resolve`` — a skill invocation's
  hidden ``<skill>`` meta text, with the ``<path>`` resolved against the
  runner's filesystem.
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


def _skill_md(name: str, description: str) -> str:
    """
    Build minimal SKILL.md text with valid frontmatter.

    :param name: Skill name, e.g. ``"host-skill"``.
    :param description: One-line description.
    :returns: SKILL.md file contents.
    """
    return f"---\nname: {name}\ndescription: {description}\n---\n\nbody for {name}\n"


class _SpecStub:
    """
    Minimal stand-in for an ``AgentSpec`` exposing only what skill
    discovery reads: the bundled ``skills`` list and ``skills_filter``.

    :param skills: Bundled skills the agent ships.
    :param skills_filter: Host-skill filter (``"all"`` / ``"none"`` /
        list of names).
    """

    def __init__(self, skills: list[SkillSpec], skills_filter: str | list[str]) -> None:
        """
        :param skills: Bundled skills the agent ships.
        :param skills_filter: Host-skill filter from the agent spec.
        """
        self.skills = skills
        self.skills_filter = skills_filter


class _ServerClient:
    """
    Fake Omnigent server client whose session snapshot carries an agent_id and
    (optionally) the session's workspace.

    The runner reads ``agent_id`` from ``GET /v1/sessions/{id}`` to drive
    ``spec_resolver`` when the session's spec isn't cached, and reads
    ``workspace`` to root host-skill discovery at the agent's working
    directory on this runner.

    :param workspace: Session workspace path returned in the snapshot, or
        ``None`` to omit it (the runner then falls back to its global
        workspace / cwd).
    """

    def __init__(self, workspace: str | None = None) -> None:
        """
        :param workspace: Session workspace path to report, or ``None``.
        """
        self._workspace = workspace

    class _Response:
        """Stub 200 snapshot response with an agent_id + workspace."""

        def __init__(self, workspace: str | None) -> None:
            """
            :param workspace: Workspace path to include in the body.
            """
            self.status_code = 200
            self._workspace = workspace

        def json(self) -> dict[str, Any]:
            """:returns: A minimal session snapshot."""
            return {"agent_id": "ag_x", "workspace": self._workspace}

    async def get(self, url: str, **kwargs: Any) -> _Response:
        """
        :param url: Request URL (ignored).
        :param kwargs: Extra kwargs (ignored).
        :returns: The stub snapshot response.
        """
        del url, kwargs
        return self._Response(self._workspace)


def _make_app(
    bundle_dir: Path,
    bundled: list[SkillSpec],
    skills_filter: str | list[str],
    *,
    workspace: Path | None = None,
    resolver_calls: list[str] | None = None,
):  # type: ignore[no-untyped-def]
    """
    Build a runner app whose spec resolver returns a stub spec.

    :param bundle_dir: Materialized bundle workdir (carried on the
        resolved spec entry; no longer the host-skill discovery root).
    :param bundled: Bundled skills the stub spec exposes.
    :param skills_filter: Host-skill filter for the stub spec.
    :param workspace: Session workspace the fake server reports — the
        host-skill discovery root. ``None`` omits it.
    :param resolver_calls: Optional list appended to on each
        ``spec_resolver`` invocation, for asserting cache behavior.
    :returns: The configured FastAPI app.
    """
    spec = _SpecStub(bundled, skills_filter)
    entry = ResolvedSpec(spec=spec, workdir=bundle_dir)

    async def _spec_resolver(agent_id: str, session_id: str | None) -> Any:
        """Return the stub resolved spec, recording the call."""
        if resolver_calls is not None:
            resolver_calls.append(agent_id)
        return entry

    return create_runner_app(
        spec_resolver=_spec_resolver,
        server_client=_ServerClient(str(workspace) if workspace is not None else None),  # type: ignore[arg-type]
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
async def test_get_session_skills_returns_bundled_skills(tmp_path: Path) -> None:
    """
    ``GET /skills`` returns the bundled skills (name + description).
    ``skills_filter="none"`` suppresses host discovery so the result is
    exactly the bundled set — hermetic, independent of the dev's real
    ``~/.claude/skills/``.
    """
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    bundled = [
        SkillSpec(name="grill-me", description="Stress-test a plan.", content="Ask questions."),
        SkillSpec(name="code-review", description="Review changes.", content="Look hard."),
    ]
    app = _make_app(bundle, bundled, "none")

    async for c in _client(app):
        resp = await c.get("/v1/sessions/conv_s/skills")

    assert resp.status_code == 200, resp.text
    skills = resp.json()["skills"]
    assert skills == [
        {"name": "grill-me", "description": "Stress-test a plan."},
        {"name": "code-review", "description": "Review changes."},
    ]


@pytest.mark.asyncio
async def test_get_session_skills_unions_workspace_and_bundle_host_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Host discovery is the union of every root the agent can load from:
    the spec's bundled skills, host skills under the session workspace
    (the agent's cwd, where project ``.claude/skills/`` live), AND host
    skills under the agent bundle workdir.

    Placing a distinct host skill under each of the workspace and the
    bundle dir proves both roots are scanned and merged. Ordering:
    bundled first, then the workspace (primary root), then the bundle
    workdir. The home dir is pinned to an empty temp dir so the dev's
    real skills don't leak in.
    """
    home = tmp_path / "home"
    (home / ".claude" / "skills").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    bundle = tmp_path / "bundle"
    bundle_host = bundle / ".claude" / "skills" / "bundle-host"
    bundle_host.mkdir(parents=True)
    (bundle_host / "SKILL.md").write_text(_skill_md("bundle-host", "From the bundle dir."))

    workspace = tmp_path / "workspace"
    workspace_host = workspace / ".claude" / "skills" / "workspace-host"
    workspace_host.mkdir(parents=True)
    (workspace_host / "SKILL.md").write_text(_skill_md("workspace-host", "From the workspace."))

    bundled = [
        SkillSpec(name="grill-me", description="Stress-test a plan.", content="Ask questions."),
    ]
    app = _make_app(bundle, bundled, "all", workspace=workspace)

    async for c in _client(app):
        resp = await c.get("/v1/sessions/conv_host/skills")

    assert resp.status_code == 200, resp.text
    names = [s["name"] for s in resp.json()["skills"]]
    # Bundled first, then workspace root, then bundle workdir.
    assert names == ["grill-me", "workspace-host", "bundle-host"]


@pytest.mark.asyncio
async def test_get_session_skills_native_shape_finds_workspace_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The claude-native shape: the agent ships no bundled skills and its
    bundle root is a throwaway temp dir, but the session workspace has a
    project-local skill. Discovery must still surface it — the bug this
    fixes was rooting at the empty bundle temp dir, which found nothing.
    """
    home = tmp_path / "home"
    (home / ".claude" / "skills").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    # Empty throwaway bundle root, as created for single-YAML native agents.
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    workspace = tmp_path / "workspace"
    skill_dir = workspace / ".claude" / "skills" / "project-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_skill_md("project-skill", "Local to the project."))

    # No bundled skills (spec.skills == []), mirroring claude-native-ui.
    app = _make_app(bundle, [], "all", workspace=workspace)

    async for c in _client(app):
        resp = await c.get("/v1/sessions/conv_native/skills")

    assert resp.status_code == 200, resp.text
    assert [s["name"] for s in resp.json()["skills"]] == ["project-skill"]


@pytest.mark.asyncio
async def test_get_session_skills_empty_without_spec_resolver(tmp_path: Path) -> None:
    """
    With no spec resolver wired, ``GET /skills`` returns an empty list
    (nothing to discover) rather than erroring.
    """
    del tmp_path
    app = create_runner_app(server_client=_ServerClient())  # type: ignore[arg-type]

    async for c in _client(app):
        resp = await c.get("/v1/sessions/conv_none/skills")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"skills": []}


@pytest.mark.asyncio
async def test_resolve_session_skill_returns_runner_side_meta_text(tmp_path: Path) -> None:
    """
    ``POST /skills/resolve`` builds the ``<skill>`` meta text on the
    runner: it embeds the runner-side ``<path>`` (resolved from the
    skill's ``skill_dir`` on this filesystem) and the typed arguments.
    """
    bundle = tmp_path / "bundle"
    skill_dir = bundle / "skills" / "grill-me"
    skill_dir.mkdir(parents=True)
    bundled = [
        SkillSpec(
            name="grill-me",
            description="Stress-test a plan.",
            content="Ask sharp questions one at a time.",
            skill_dir=skill_dir,
        ),
    ]
    app = _make_app(bundle, bundled, "none")

    async for c in _client(app):
        resp = await c.post(
            "/v1/sessions/conv_r/skills/resolve",
            json={"name": "grill-me", "arguments": "review this rollout"},
        )

    assert resp.status_code == 200, resp.text
    meta = resp.json()["meta_text"]
    assert "<skill>" in meta
    assert "<name>grill-me</name>" in meta
    assert f"<path>{skill_dir / 'SKILL.md'}</path>" in meta
    assert "Ask sharp questions one at a time." in meta
    assert "<user_request>\nreview this rollout\n</user_request>" in meta


@pytest.mark.asyncio
async def test_resolve_session_skill_unknown_returns_404_with_available(tmp_path: Path) -> None:
    """
    Resolving a skill the session does not expose returns 404 plus the
    sorted list of available skill names (the error the Omnigent server
    surfaces to the user).
    """
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    bundled = [
        SkillSpec(name="grill-me", description="d", content="c"),
        SkillSpec(name="code-review", description="d", content="c"),
    ]
    app = _make_app(bundle, bundled, "none")

    async for c in _client(app):
        resp = await c.post(
            "/v1/sessions/conv_404/skills/resolve",
            json={"name": "does-not-exist", "arguments": ""},
        )

    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["error"] == "skill_not_found"
    assert body["available"] == ["code-review", "grill-me"]


@pytest.mark.asyncio
async def test_resolve_session_skill_missing_name_returns_400(tmp_path: Path) -> None:
    """A resolve request without a ``name`` is a 400, not a 404/500."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    app = _make_app(bundle, [], "none")

    async for c in _client(app):
        resp = await c.post("/v1/sessions/conv_400/skills/resolve", json={"arguments": "x"})

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_resolve_session_skill_invalid_json_body_returns_400(tmp_path: Path) -> None:
    """A non-JSON request body is a structured 400, not an uncaught 500."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    app = _make_app(bundle, [], "none")

    async for c in _client(app):
        resp = await c.post(
            "/v1/sessions/conv_badjson/skills/resolve",
            content=b"not json",
            headers={"content-type": "application/json"},
        )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_resolve_session_skill_non_string_arguments_returns_400(tmp_path: Path) -> None:
    """
    ``arguments`` that isn't a string is a structured 400 — otherwise it
    would blow up later in ``format_skill_meta_text``'s string join.
    """
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    bundled = [SkillSpec(name="grill-me", description="Stress-test a plan.", content="c")]
    app = _make_app(bundle, bundled, "none")

    async for c in _client(app):
        resp = await c.post(
            "/v1/sessions/conv_badargs/skills/resolve",
            json={"name": "grill-me", "arguments": 123},
        )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_session_skills_cached_per_session(tmp_path: Path) -> None:
    """
    The merged skills are cached per session: a second ``GET /skills``
    reuses the cache and does not re-run spec resolution / the host-skill
    filesystem walk.
    """
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    bundled = [SkillSpec(name="grill-me", description="d", content="c")]
    resolver_calls: list[str] = []
    app = _make_app(bundle, bundled, "none", resolver_calls=resolver_calls)

    async for c in _client(app):
        first = await c.get("/v1/sessions/conv_cache/skills")
        second = await c.get("/v1/sessions/conv_cache/skills")

    assert first.status_code == 200
    assert second.status_code == 200
    # Exactly one spec resolution (and thus one host-skill filesystem
    # walk) across both requests: the first populates the per-session
    # cache, the second hits it. Two entries here would mean the cache
    # was not consulted (a walk per request); zero would mean discovery
    # never ran at all.
    assert resolver_calls == ["ag_x"]
