"""Runner invocation resolves skills and resource paths in the execution environment."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harnesses.claude_native import plugin_discovery
from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app
from omnigent.runner.app import ResolvedSpec
from omnigent.server.routes._sessions.helpers import _resolve_skill_meta_text_via_runner
from omnigent.spec.skill_sources import resolve_session_skills
from omnigent.spec.types import SkillSpec
from tests._helpers.claude_launch import configure_claude_launch
from tests._helpers.live_server import serve_app_in_thread
from tests._helpers.polling import poll_until_async
from tests._helpers.providers import isolate_provider_resolution


@pytest.fixture(autouse=True)
def _isolate_claude_config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the ambient Claude config dir the runner resolution reads.

    These tests pin ``Path.home()``; an ambient ``CLAUDE_CONFIG_DIR`` would
    redirect the claude user scope away from that pinned home and flip the
    expected menus on machines that set it.
    """
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def _skill_md(name: str, description: str) -> str:
    """
    Build minimal SKILL.md text with valid frontmatter.

    :param name: Skill name, e.g. ``"host-skill"``.
    :param description: One-line description.
    :returns: SKILL.md file contents.
    """
    return f"---\nname: {name}\ndescription: {description}\n---\n\nbody for {name}\n"


class _ExecutorStub:
    """Minimal ``ExecutorSpec`` stand-in exposing ``harness_kind``."""

    def __init__(self, harness: str) -> None:
        """:param harness: The session's harness, e.g. ``"claude-sdk"``."""
        self.harness_kind = harness


class _SpecStub:
    """
    Minimal stand-in for an ``AgentSpec`` exposing only what skill
    discovery reads: the bundled ``skills`` list, ``skills_filter``, and
    ``executor.harness_kind`` (so the runner can dispatch to the right
    per-harness skill source).

    :param skills: Bundled skills the agent ships.
    :param skills_filter: Host-skill filter (``"all"`` / ``"none"`` /
        list of names).
    :param harness: The session's harness; defaults to ``"claude-sdk"``.
    """

    def __init__(
        self,
        skills: list[SkillSpec],
        skills_filter: str | list[str],
        harness: str = "claude-sdk",
    ) -> None:
        """
        :param skills: Bundled skills the agent ships.
        :param skills_filter: Host-skill filter from the agent spec.
        :param harness: Harness id driving per-harness skill discovery.
        """
        self.skills = skills
        self.skills_filter = skills_filter
        self.executor = _ExecutorStub(harness)


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
    harness: str = "claude-sdk",
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
    :param harness: The stub spec's harness, driving per-harness skill
        discovery. Defaults to ``"claude-sdk"`` (today's behavior).
    :returns: The configured FastAPI app.
    """
    spec = _SpecStub(bundled, skills_filter, harness=harness)
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


@asynccontextmanager
async def _socket_client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    """
    Yield a client for the runner app over a real socket, with the server's runner timeouts.

    :param app: The runner FastAPI app.
    :returns: Async context manager yielding the client.
    """
    with serve_app_in_thread(app) as base_url:
        async with httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(5.0, read=None), trust_env=False
        ) as c:
            yield c


@pytest.mark.asyncio
async def test_invocation_available_skills_returns_bundled_skills(tmp_path: Path) -> None:
    """Unknown invocations list the visible bundled skills available to resolve."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    bundled = [
        SkillSpec(name="grill-me", description="Stress-test a plan.", content="Ask questions."),
        SkillSpec(name="code-review", description="Review changes.", content="Look hard."),
    ]
    app = _make_app(bundle, bundled, "none")

    async for c in _client(app):
        resp = await c.post("/v1/sessions/conv_s/skills/resolve", json={"name": "missing-skill"})

    assert resp.status_code == 404, resp.text
    assert resp.json()["available"] == ["code-review", "grill-me"]


@pytest.mark.asyncio
async def test_invocation_available_skills_unions_workspace_and_bundle_host_skills(
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
        resp = await c.post(
            "/v1/sessions/conv_host/skills/resolve", json={"name": "missing-skill"}
        )

    assert resp.status_code == 404, resp.text
    names = resp.json()["available"]
    assert set(names) == {"grill-me", "workspace-host", "bundle-host"}


@pytest.mark.asyncio
async def test_invocation_available_skills_native_shape_finds_workspace_skill(
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
        resp = await c.post(
            "/v1/sessions/conv_native/skills/resolve", json={"name": "missing-skill"}
        )

    assert resp.status_code == 404, resp.text
    assert resp.json()["available"] == ["project-skill"]


@pytest.mark.asyncio
async def test_invocation_available_skills_empty_without_spec_resolver(tmp_path: Path) -> None:
    """
    With no spec resolver, an unknown invocation reports no available skills.
    """
    del tmp_path
    app = create_runner_app(server_client=_ServerClient())  # type: ignore[arg-type]

    async for c in _client(app):
        resp = await c.post(
            "/v1/sessions/conv_none/skills/resolve", json={"name": "missing-skill"}
        )

    assert resp.status_code == 404, resp.text
    assert resp.json()["available"] == []


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
        assert (await c.get("/v1/sessions/conv_r/skills")).status_code == 404
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
async def test_resolve_accepts_frontmatter_name_from_older_server(tmp_path: Path) -> None:
    """An older server sends a bundled skill's frontmatter name, not its directory."""
    bundle = tmp_path / "bundle"
    skill_dir = bundle / "skills" / "review"
    skill_dir.mkdir(parents=True)
    bundled = [
        SkillSpec(
            name="review",
            description="Review changes.",
            content="Look hard.",
            skill_dir=skill_dir,
            display_name="code-review",
        ),
    ]
    app = _make_app(bundle, bundled, "none")

    async for c in _client(app):
        resp = await c.post("/v1/sessions/conv_r/skills/resolve", json={"name": "code-review"})

    assert resp.status_code == 200, resp.text
    assert "<name>review</name>" in resp.json()["meta_text"]


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
@pytest.mark.parametrize(
    "source_warning",
    [None, "Codex plugin skills unavailable: `codex plugin list` timed out after 5s."],
    ids=["complete", "failed-source"],
)
async def test_session_skills_cached_per_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_warning: str | None
) -> None:
    """
    The merged skills are cached per session: a second ``POST /skills/resolve``
    reuses the cache and does not re-run spec resolution / the host-skill
    filesystem walk. A missing name reuses it too unless a launch was still loading.
    A list built with a warning expires sooner, so a later request walks again.
    """
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    bundled = [SkillSpec(name="grill-me", description="d", content="c")]
    resolver_calls: list[str] = []
    app = _make_app(bundle, bundled, "none", resolver_calls=resolver_calls)
    walks: list[tuple[Path, ...]] = []

    def counted_walk(
        spec: Any, roots: tuple[Path, ...], *args: Any, **kwargs: Any
    ) -> list[SkillSpec]:
        walks.append(roots)
        if source_warning is not None:
            kwargs["warnings"].append(source_warning)
        return resolve_session_skills(spec, roots, *args, **kwargs)

    monkeypatch.setattr("omnigent.runner.app.resolve_session_skills", counted_walk)
    runner_now = [time.monotonic()]
    monkeypatch.setattr(
        "omnigent.runner.app.time",
        SimpleNamespace(monotonic=lambda: runner_now[0], time=time.time),
    )

    async for c in _client(app):
        first = await c.post("/v1/sessions/conv_cache/skills/resolve", json={"name": "grill-me"})
        second = await c.post("/v1/sessions/conv_cache/skills/resolve", json={"name": "grill-me"})
        missing = await c.post("/v1/sessions/conv_cache/skills/resolve", json={"name": "unknown"})
        cached_walks = len(walks)
        runner_now[0] += runner_app._INCOMPLETE_SESSION_SKILLS_CACHE_TTL_SECONDS
        expired = await c.post("/v1/sessions/conv_cache/skills/resolve", json={"name": "grill-me"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert missing.status_code == 404, missing.text
    assert expired.status_code == 200
    # The first request populates the per-session cache; the later ones, even the miss, hit it.
    assert cached_walks == 1, walks
    assert len(walks) == (1 if source_warning is None else 2), walks
    assert resolver_calls == ["ag_x"]


@pytest.mark.asyncio
async def test_session_skills_cache_ttl_expiry_rediscovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The per-session cache honors a TTL: once it elapses, the next request
    re-walks the filesystem and picks up a skill installed mid-session.

    With the TTL pinned to 0 every cached entry is immediately stale, so a
    host skill created AFTER the first request must appear in the second —
    proof the walk reran rather than serving the stale cache (the converse of
    ``test_session_skills_cached_per_session``, which proves the cache holds
    within the window).
    """
    home = tmp_path / "home"
    (home / ".claude" / "skills").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setattr("omnigent.runner.app._SESSION_SKILLS_CACHE_TTL_SECONDS", 0.0)

    workspace = tmp_path / "workspace"
    first_skill = workspace / ".claude" / "skills" / "first"
    first_skill.mkdir(parents=True)
    (first_skill / "SKILL.md").write_text(_skill_md("first", "Present from the start."))

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    app = _make_app(bundle, [], "all", workspace=workspace)

    async for c in _client(app):
        first = await c.post(
            "/v1/sessions/conv_ttl/skills/resolve", json={"name": "missing-skill"}
        )
        # Install a second host skill only AFTER the first response is served.
        second_skill = workspace / ".claude" / "skills" / "second"
        second_skill.mkdir(parents=True)
        (second_skill / "SKILL.md").write_text(_skill_md("second", "Installed mid-session."))
        second = await c.post(
            "/v1/sessions/conv_ttl/skills/resolve", json={"name": "missing-skill"}
        )

    assert first.status_code == 404
    assert second.status_code == 404
    assert set(first.json()["available"]) == {"first"}
    # TTL=0 ⇒ the second request re-walked and discovered the new skill.
    assert set(second.json()["available"]) == {"first", "second"}


def _seed_claude_plugin(home: Path) -> None:
    """Seed a fake ~/.claude with one enabled plugin exposing one skill."""
    install = home / ".claude" / "plugins" / "cache" / "mkt" / "superpowers" / "1.0.0"
    (install / "skills" / "using-superpowers").mkdir(parents=True)
    (install / "skills" / "using-superpowers" / "SKILL.md").write_text(
        _skill_md("using-superpowers", "Use superpowers.")
    )
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"superpowers@mkt": True}})
    )
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "superpowers@mkt": [{"installPath": str(install), "version": "1.0.0"}]
                },
            }
        )
    )


@pytest.mark.asyncio
async def test_invocation_available_skills_includes_enabled_claude_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claude session surfaces enabled-plugin skills, namespaced."""
    home = tmp_path / "home"
    _seed_claude_plugin(home)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = _make_app(bundle, [], "all", workspace=workspace, harness="claude-native")

    async for c in _client(app):
        resp = await c.post("/v1/sessions/conv_cc/skills/resolve", json={"name": "missing-skill"})

    assert resp.status_code == 404, resp.text
    assert "superpowers:using-superpowers" in resp.json()["available"]


@pytest.mark.asyncio
async def test_codex_session_does_not_list_claude_plugin_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parity: a codex session must not show Claude plugin skills."""
    home = tmp_path / "home"
    _seed_claude_plugin(home)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = _make_app(bundle, [], "all", workspace=workspace, harness="codex-native")

    async for c in _client(app):
        resp = await c.post(
            "/v1/sessions/conv_codex/skills/resolve", json={"name": "missing-skill"}
        )

    assert resp.status_code == 404, resp.text
    assert "superpowers:using-superpowers" not in resp.json()["available"]


@pytest.mark.asyncio
async def test_wrapped_codex_resolves_workspace_and_repo_root_agents_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrapped Codex runs ``.agents`` skills from the workspace up to the repository root."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.delenv("CODEX_HOME", raising=False)

    bundled_dir = tmp_path / "bundle" / "skills" / "shared"
    bundled_dir.mkdir(parents=True)
    (bundled_dir / "SKILL.md").write_text(_skill_md("shared", "Bundled copy."))
    bundled = [
        SkillSpec(
            name="shared",
            description="Bundled copy.",
            content="Bundled body.",
            skill_dir=bundled_dir,
        )
    ]
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    workspace = repo / "app"
    skill_paths = {}
    for name, root in (("ws-skill", workspace), ("root-skill", repo), ("shared", workspace)):
        skill_dir = root / ".agents" / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(_skill_md(name, f"{name} description."))
        skill_paths[name] = skill_dir.resolve() / "SKILL.md"
    app = _make_app(tmp_path / "bundle", bundled, "all", workspace=workspace, harness="codex")

    async for c in _client(app):
        responses = {
            name: await c.post(
                "/v1/sessions/conv_wrapped_codex/skills/resolve",
                json={"name": name, "arguments": f"run {name}"},
            )
            for name in skill_paths
        }

    # The agent's bundled skill outranks a same-named workspace skill, as in the menu.
    shared = responses.pop("shared")
    assert shared.status_code == 200, shared.text
    shared_meta = shared.json()["meta_text"]
    assert f"<path>{bundled_dir / 'SKILL.md'}</path>" in shared_meta
    assert "Bundled body." in shared_meta
    assert "body for shared" not in shared_meta
    for name, resp in responses.items():
        assert resp.status_code == 200, resp.text
        meta = resp.json()["meta_text"]
        assert f"<name>{name}</name>" in meta
        assert f"<path>{skill_paths[name]}</path>" in meta
        assert f"body for {name}" in meta
        assert f"<user_request>\nrun {name}\n</user_request>" in meta


@pytest.mark.asyncio
async def test_resolve_finds_a_surfaced_plugin_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plugin skills resolve by the names exposed in the host menu."""
    home = tmp_path / "home"
    _seed_claude_plugin(home)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = _make_app(bundle, [], "all", workspace=workspace, harness="claude-native")

    async for c in _client(app):
        resp = await c.post(
            "/v1/sessions/conv_cc/skills/resolve",
            json={"name": "superpowers:using-superpowers", "arguments": "go"},
        )

    assert resp.status_code == 200, resp.text
    assert "<skill>" in resp.json()["meta_text"]


@pytest.mark.posix_only
@pytest.mark.asyncio
async def test_resolve_waits_for_a_claude_launch_only_for_an_unlisted_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _no_configured_claude_launch: Callable[[], bool],
) -> None:
    """A listed skill resolves while the configured launch is pending; its plugin skill waits.

    The server's lookup is answered before its runner timeout while a launch is held. A
    catalog missing a stalled or failed launch's plugins is rebuilt once the launch answers.
    """
    for env_name in ("OMNIGENT_CLAUDE_PATH", "HARNESS_CLAUDE_PATH", "OMNIGENT_CLAUDE_LAUNCHER"):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setattr(
        plugin_discovery, "claude_launch_is_customized", _no_configured_claude_launch
    )
    now = [0.0]
    monkeypatch.setattr(plugin_discovery, "time", SimpleNamespace(monotonic=lambda: now[0]))
    # A held launch outlives the readers' wait; only the fake clock passes its deadline.
    monkeypatch.setattr(plugin_discovery, "LAUNCH_PLUGINS_TIMEOUT_S", 60)
    home = tmp_path / "home"
    plugin = tmp_path / "launch" / "dev"
    for root, name in ((home / ".claude", "local"), (plugin, "review")):
        skill = root / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(_skill_md(name, f"Use {name}."))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    release = tmp_path / "release"
    fail = tmp_path / "fail"
    plugins = [{"name": "dev", "path": str(plugin), "source": "dev@inline"}]
    init = {"type": "system", "subtype": "init", "plugins": plugins}
    config_home = isolate_provider_resolution(monkeypatch, tmp_path / "config")
    launches = tmp_path / "launches.jsonl"
    script = (
        f"while not os.path.exists({str(release)!r}):\n    time.sleep(0.05)\n"
        f"if os.path.exists({str(fail)!r}):\n    sys.exit(3)\n"
        f"print({json.dumps(init)!r})\n"
    )
    configure_claude_launch(config_home, launches, script)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = _make_app(bundle, [], "all", workspace=workspace, harness="claude-native")
    url = "/v1/sessions/conv_launch/skills/resolve"
    late_url = "/v1/sessions/conv_late/skills/resolve"
    failed_url = "/v1/sessions/conv_failed/skills/resolve"
    review = {"name": "dev:review"}

    async with _socket_client(app) as c:
        try:
            listed = await asyncio.wait_for(c.post(url, json={"name": "local"}), 5)
            pending = asyncio.ensure_future(c.post(url, json=review))
            await asyncio.sleep(0.5)
            waited = not pending.done()
        finally:
            release.touch()
        launched = await asyncio.wait_for(pending, 15)

        # A new launch identity is held: the server's lookup hears the skill is missing before
        # its runner timeout, and past the probe's deadline the launch is reported stalled.
        release.unlink()
        configure_claude_launch(config_home, launches, script, args=("--", "late"))
        try:
            with pytest.raises(plugin_discovery.LaunchPluginsPending):
                plugin_discovery.claude_launch_plugin_dirs(wait=False)
            with pytest.raises(OmnigentError, match="not found") as held:
                await _resolve_skill_meta_text_via_runner("conv_held", "dev:review", "", c)
            await poll_until_async(lambda: len(launches.read_text().splitlines()) == 2)
            now[0] += plugin_discovery.LAUNCH_PLUGINS_DEADLINE_S
            stalled = await c.post(late_url, json=review)
            stalled_generation = plugin_discovery.launch_plugins_generation()
        finally:
            release.touch()
        await poll_until_async(
            lambda before=stalled_generation: plugin_discovery.launch_plugins_generation() > before
        )
        late = await c.post(late_url, json=review)

    # A failed launch is asked again once its backoff and its list's short expiry have passed.
    async for c in _client(app):
        runner_now = [time.monotonic()]
        monkeypatch.setattr(
            "omnigent.runner.app.time",
            SimpleNamespace(monotonic=lambda clock=runner_now: clock[0], time=time.time),
        )
        fail.touch()
        configure_claude_launch(config_home, launches, script, args=("--", "failing"))
        failed = await c.post(failed_url, json=review)
        fail.unlink()
        now[0] += plugin_discovery.FAILED_LAUNCH_BACKOFF_S
        runner_now[0] += runner_app._INCOMPLETE_SESSION_SKILLS_CACHE_TTL_SECONDS
        retried = await c.post(failed_url, json=review)

    assert listed.status_code == 200, listed.text
    assert waited
    assert launched.status_code == 200, launched.text
    assert "body for review" in launched.json()["meta_text"]
    assert held.value.code == ErrorCode.INVALID_INPUT, held.value
    assert stalled.status_code == 404, stalled.text
    assert late.status_code == 200, late.text
    assert failed.status_code == 404, failed.text
    assert retried.status_code == 200, retried.text
    assert len(launches.read_text().splitlines()) == 4


@pytest.mark.asyncio
async def test_invocation_available_skills_excludes_user_invocable_false_bundled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user-invocable:false bundled skill is kept out of the composer menu."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bundled = [
        SkillSpec(name="visible", description="Shown.", content="c"),
        SkillSpec(name="internal", description="Hidden.", content="c", user_invocable=False),
    ]
    app = _make_app(bundle, bundled, "all", workspace=workspace, harness="claude-native")

    async for c in _client(app):
        resp = await c.post("/v1/sessions/conv_ui/skills/resolve", json={"name": "missing-skill"})

    assert resp.status_code == 404, resp.text
    names = resp.json()["available"]
    assert "visible" in names
    assert "internal" not in names


@pytest.mark.asyncio
async def test_non_invocable_bundled_skill_does_not_unshadow_host_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A bundled skill marked user-invocable:false stays hidden AND keeps a
    same-named host skill hidden — the non-invocable name still shadows.
    """
    home = tmp_path / "home"
    # Host skill that shares the bundled skill's name.
    hostdir = home / ".claude" / "skills" / "shared"
    hostdir.mkdir(parents=True)
    (hostdir / "SKILL.md").write_text(_skill_md("shared", "Host version."))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bundled = [
        SkillSpec(name="shared", description="Internal.", content="c", user_invocable=False),
    ]
    app = _make_app(bundle, bundled, "all", workspace=workspace, harness="claude-native")

    async for c in _client(app):
        resp = await c.post(
            "/v1/sessions/conv_shadow/skills/resolve", json={"name": "missing-skill"}
        )

    assert resp.status_code == 404, resp.text
    # Neither the hidden bundled skill nor the host skill of the same name shows.
    assert "shared" not in resp.json()["available"]


@pytest.mark.asyncio
async def test_codex_bundle_skill_not_duplicated_when_dir_differs_from_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A codex session must not double-list a bundle skill whose directory name
    differs from its SKILL.md frontmatter name: spec.skills adds it by
    frontmatter name, the codex provider rediscovers it by dir name, and the
    skill_dir dedup collapses the two into one entry.
    """
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    bundle = tmp_path / "bundle"
    skill_dir = bundle / "skills" / "sra--triage"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_skill_md("triage", "Triage things."))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # The runner's spec.skills carries the bundle skill by frontmatter name.
    bundled = [
        SkillSpec(
            name="triage",
            description="Triage things.",
            content="c",
            skill_dir=skill_dir,
        )
    ]
    app = _make_app(bundle, bundled, "all", workspace=workspace, harness="codex-native")

    async for c in _client(app):
        resp = await c.post("/v1/sessions/conv_dup/skills/resolve", json={"name": "missing-skill"})

    assert resp.status_code == 404, resp.text
    names = resp.json()["available"]
    # Exactly one entry for the skill (no phantom dir-named duplicate).
    assert names.count("triage") + names.count("sra--triage") == 1
