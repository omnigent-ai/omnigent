"""Sub-agent bundle isolation across the runner's spec-resolution sites.

A sub-agent's skills and local tools live in its own bundle directory,
``<parent bundle>/agents/<dir>``. Every runner site that swaps a parent
spec for a named sub-agent must swap the bundle dir with it — otherwise
the child boots advertising the PARENT's bundle and inherits skills and
tools it has no claim to.

These tests cover the harness-facing surfaces that carry a bundle dir:

* the SDK / subprocess harnesses, via ``HARNESS_*_BUNDLE_DIR`` in the
  spawn-env (claude-sdk, codex, pi, cursor, copilot);
* the native TUI harnesses, via the launch inputs of the auto-create
  paths (claude-native's ``--plugin-dir``, codex-native's
  ``CODEX_HOME`` skills source);
* the turn-dispatch spec cache, which must keep the resolved entry
  wrapped so a "no bundle" answer is not re-widened downstream.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnigent.entities.session_resources import SessionResourceView
from omnigent.runner import create_runner_app
from omnigent.runner import tool_dispatch as _tool_dispatch
from omnigent.runner.app import _resolve_harness_config, _spec_with_workdir_paths
from omnigent.runner.native.orchestration import (
    ResolvedSpec,
    _ensure_orchestrator_skills_in_bundle,
    _resolve_sub_agent_spec_entry,
)
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.spec.parser import parse
from omnigent.spec.types import AgentSpec, LocalToolInfo
from omnigent.terminals import TerminalRegistry
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient

# The env var each wrapped harness uses to advertise its bundle dir.
_BUNDLE_DIR_ENV = {
    "claude-sdk": "HARNESS_CLAUDE_SDK_BUNDLE_DIR",
    "codex": "HARNESS_CODEX_BUNDLE_DIR",
    "pi": "HARNESS_PI_BUNDLE_DIR",
    "cursor": "HARNESS_CURSOR_BUNDLE_DIR",
    "copilot": "HARNESS_COPILOT_BUNDLE_DIR",
}


def _write_agent(
    directory: Path,
    name: str,
    *,
    harness: str = "claude-sdk",
    extra: dict[str, Any] | None = None,
) -> Path:
    """Create an agent bundle at *directory* pinned to *harness*."""
    directory.mkdir(parents=True, exist_ok=True)
    config: dict[str, Any] = {
        "spec_version": 1,
        "name": name,
        "executor": {"type": "omnigent", "config": {"harness": harness}},
    }
    config.update(extra or {})
    (directory / "config.yaml").write_text(yaml.dump(config))
    return directory


def _write_skill(bundle: Path, name: str) -> None:
    """Add a bundled skill named *name* to *bundle*."""
    skill_dir = bundle / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill\n---\n\n# {name}\n"
    )


def _bundle_with_child(tmp_path: Path, *, harness: str = "claude-sdk") -> tuple[Path, Path]:
    """A parent bundle with one sub-agent ``worker``; both own a skill."""
    root = _write_agent(tmp_path / "root", "root", harness=harness)
    _write_skill(root, "parent-only")

    child = _write_agent(root / "agents" / "worker", "worker", harness=harness)
    _write_skill(child, "child-only")
    return root, child


# ── Wrapped harnesses: spawn-env bundle dir ───────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", sorted(_BUNDLE_DIR_ENV))
async def test_spawn_env_bundle_dir_is_the_child_bundle(tmp_path: Path, harness: str) -> None:
    """``_resolve_harness_config`` advertises the CHILD's bundle dir.

    The bound ``agent_id`` resolves to the PARENT spec, so without
    re-resolving the workdir alongside the spec the harness would be
    handed the parent bundle and load the parent's skills.
    """
    root, child = _bundle_with_child(tmp_path, harness=harness)

    async def _resolver(agent_id: str, session_id: str | None) -> Any:
        return ResolvedSpec(spec=parse(root), workdir=root)

    resolved_harness, spawn_env = await _resolve_harness_config(
        agent_id="agent-1",
        spec_resolver=_resolver,
        session_id="sess-1",
        sub_agent_name="worker",
    )

    assert resolved_harness == harness
    assert spawn_env is not None
    assert spawn_env[_BUNDLE_DIR_ENV[harness]] == str(child)


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", sorted(_BUNDLE_DIR_ENV))
async def test_spawn_env_omits_bundle_dir_when_child_has_none(
    tmp_path: Path, harness: str
) -> None:
    """An unresolvable child bundle yields no bundle dir — never the parent's.

    Here the parent entry carries no workdir at all, so there is nothing
    to resolve the child against. Falling back to the parent bundle (or
    the runner workspace) is exactly the leak under test.
    """
    root, _child = _bundle_with_child(tmp_path, harness=harness)

    async def _resolver(agent_id: str, session_id: str | None) -> Any:
        return ResolvedSpec(spec=parse(root), workdir=None)

    _resolved_harness, spawn_env = await _resolve_harness_config(
        agent_id="agent-1",
        spec_resolver=_resolver,
        session_id="sess-1",
        sub_agent_name="worker",
    )

    assert spawn_env is not None
    assert _BUNDLE_DIR_ENV[harness] not in spawn_env
    assert str(root) not in str(spawn_env)


@pytest.mark.asyncio
async def test_spawn_env_bundle_dir_unchanged_for_top_level_sessions(tmp_path: Path) -> None:
    """A session with no ``sub_agent_name`` still gets the parent bundle.

    Guards the other direction: the fix must not strip the bundle dir
    from ordinary top-level sessions.
    """
    root, _child = _bundle_with_child(tmp_path)

    async def _resolver(agent_id: str, session_id: str | None) -> Any:
        return ResolvedSpec(spec=parse(root), workdir=root)

    _harness, spawn_env = await _resolve_harness_config(
        agent_id="agent-1",
        spec_resolver=_resolver,
        session_id="sess-1",
    )

    assert spawn_env is not None
    assert spawn_env["HARNESS_CLAUDE_SDK_BUNDLE_DIR"] == str(root)


@pytest.mark.asyncio
async def test_child_harness_still_wins_over_the_parents(tmp_path: Path) -> None:
    """Rooting the child correctly must not disturb harness selection.

    The sub-agent swap exists so the child's own harness drives its turn;
    the bundle-dir fix rides the same resolution and must preserve that.
    """
    root = _write_agent(tmp_path / "root", "root", harness="claude-sdk")
    _write_agent(root / "agents" / "worker", "worker", harness="codex")

    async def _resolver(agent_id: str, session_id: str | None) -> Any:
        return ResolvedSpec(spec=parse(root), workdir=root)

    harness, spawn_env = await _resolve_harness_config(
        agent_id="agent-1",
        spec_resolver=_resolver,
        session_id="sess-1",
        sub_agent_name="worker",
    )

    assert harness == "codex"
    assert spawn_env is not None
    assert spawn_env["HARNESS_CODEX_BUNDLE_DIR"] == str(root / "agents" / "worker")


# ── Native harnesses: launch-time bundle root ─────────────────


def _capture_auto_create(
    calls: list[dict[str, Any]],
) -> Any:
    """Stub ``_auto_create_*_terminal`` that records its launch inputs."""

    async def _stub(
        session_id: str,
        resource_registry: object,
        publish_event: object,
        **kwargs: Any,
    ) -> SessionResourceView:
        del resource_registry, publish_event
        calls.append(kwargs)
        return SessionResourceView(
            id="terminal_x_main", type="terminal", session_id=session_id, name="auto-created"
        )

    return _stub


async def _no_terminal(self: object, session_id: str, terminal_id: str) -> None:
    """Registry stub: no live terminal, so the ensure branch auto-creates."""
    del self, session_id, terminal_id
    return


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal", "target"),
    [
        ("claude", "_auto_create_claude_terminal"),
        ("codex", "_auto_create_codex_terminal"),
    ],
)
async def test_native_terminal_ensure_launches_against_child_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
    target: str,
) -> None:
    """The native ensure paths launch rooted at the CHILD's bundle dir.

    ``bundle_dir`` is what becomes claude-native's ``--plugin-dir`` root
    and codex-native's ``CODEX_HOME`` skills source. The ensure endpoint
    resolves the session spec from the bound ``agent_id``, which is the
    PARENT — so without re-resolving the workdir alongside the sub-agent
    swap, the child's TUI comes up carrying the parent's skills.
    """
    root, child = _bundle_with_child(tmp_path, harness=f"{terminal}-native")
    calls: list[dict[str, Any]] = []

    async def _resolver(agent_id: str, session_id: str | None = None) -> Any:
        del agent_id, session_id
        return ResolvedSpec(spec=parse(root), workdir=root)

    # The ensure endpoint dispatches through the native provider registry's
    # launch adapter, which forwards ``ctx.bundle_dir`` to the builder — so the
    # builder is patched where the adapter looks it up.
    monkeypatch.setattr(
        f"omnigent.runner.native.orchestration.{target}", _capture_auto_create(calls)
    )
    monkeypatch.setattr(SessionResourceRegistry, "get_terminal_resource", _no_terminal)

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_ChildSnapshotServer(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{_CHILD_SESSION_ID}/resources/terminals",
            json={
                "terminal": terminal,
                "session_key": "main",
                "ensure_native_terminal": True,
            },
        )

    assert resp.status_code == 200, resp.text
    assert calls, f"the {terminal} ensure branch never reached auto-create"
    bundle_dir = calls[0].get("bundle_dir")
    assert bundle_dir == child, (
        f"{terminal}-native ensure launched with bundle_dir={bundle_dir!r}; expected "
        f"the child bundle {child!r}. The parent root {root!r} puts the parent's "
        "skills on the child's plugin dir."
    )


def test_child_bundle_skills_exclude_the_parents(tmp_path: Path) -> None:
    """The resolved child spec carries only its own bundled skills.

    Both native ensure paths derive their skill set from this entry, so
    this is the assertion that the leak is actually closed rather than
    merely re-pathed.
    """
    root, child = _bundle_with_child(tmp_path)
    parent_entry = ResolvedSpec(spec=parse(root), workdir=root)

    child_entry = _resolve_sub_agent_spec_entry(parent_entry, "worker")

    assert child_entry is not None
    assert child_entry.workdir == child
    skill_names = {s.name for s in child_entry.spec.skills}
    assert skill_names == {"child-only"}
    assert not (child_entry.workdir / "skills" / "parent-only").exists()


def test_isolated_framework_bundle_carries_only_the_framework_skill(
    tmp_path: Path,
) -> None:
    """A child with no bundle dir gets a fresh dir, never parent content.

    Pins the native auto-create fallback: when no bundle dir resolves, the
    runner mints an EMPTY temp dir and seeds at most the framework-owned
    ``build-omnigent`` skill, which every agent gets unconditionally. It
    never copies from the parent — which is why those two call sites need
    no change. This fails loudly if that fallback ever starts sourcing a
    parent bundle.

    The seeding is a no-op in a source checkout (its skill source resolves
    under ``omnigent/runner/onboarding``, which does not exist), so the
    upper bound is asserted as a subset rather than an exact match.
    """
    root, _child = _bundle_with_child(tmp_path)
    isolated = tmp_path / "isolated-bundle"
    isolated.mkdir()

    _ensure_orchestrator_skills_in_bundle(isolated, parse(root))

    skills_dir = isolated / "skills"
    seeded = {p.name for p in skills_dir.iterdir()} if skills_dir.is_dir() else set()
    assert seeded <= {"build-omnigent"}
    # No parent asset reached the isolated bundle by any route.
    assert not list(isolated.rglob("parent-only"))
    assert not list(isolated.rglob("config.yaml"))


# ── Turn dispatch (site B): cache wrapper + local-tool rooting ─


class _ChildSnapshotServer(NullServerClient):
    """Server client reporting ``sub_agent_name`` on the child session GET.

    Mirrors ``SessionResponse.sub_agent_name`` — the authoritative source
    the runner recovers the sub-agent identity from when its in-memory
    map is empty (the post-reconnect state).
    """

    class _Resp:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.status_code = 200
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self) -> dict[str, Any]:
            return self._payload

        def raise_for_status(self) -> None:
            return None

    async def get(self, url: str, **kwargs: Any) -> Any:
        del kwargs
        if url.rstrip("/").endswith(_CHILD_SESSION_ID):
            return self._Resp(
                {
                    "agent_id": _PARENT_AGENT_ID,
                    "sub_agent_name": "worker",
                    "parent_session_id": "conv_parent",
                    "created_at": 0,
                    "workspace": None,
                }
            )
        if url.rstrip("/").endswith("/items"):
            return self._Resp({"data": [], "has_more": False})
        return super()._Response()


_PARENT_AGENT_ID = "ag_parent"
_CHILD_SESSION_ID = "conv_child_worker"


@pytest.mark.asyncio
async def test_turn_dispatch_spawns_child_with_child_bundle_dir(tmp_path: Path) -> None:
    """A dispatched sub-agent turn advertises the CHILD's bundle dir.

    End-to-end through ``POST /v1/sessions/{child}/events`` with no prior
    ``POST /v1/sessions``. This is the site that writes the resolved spec
    into the session cache TWICE; if either write collapses the resolved
    entry back to a bare spec, the workdir is lost and the harness is
    spawned against the parent bundle (or the runner workspace) instead.
    """
    root, child = _bundle_with_child(tmp_path)

    async def _resolver(agent_id: str, session_id: str | None = None) -> Any:
        del agent_id, session_id
        return ResolvedSpec(spec=parse(root), workdir=root)

    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "r1"}}),
            _sse({"type": "response.completed", "response": {"id": "r1"}}),
        ]
    )
    pm = _FakeProcessManager(hc)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_ChildSnapshotServer(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{_CHILD_SESSION_ID}/events",
            params={"stream": "true"},
            json={
                "type": "message",
                "role": "user",
                "agent_id": _PARENT_AGENT_ID,
                "content": [{"type": "input_text", "text": "hi"}],
            },
        )
        assert resp.status_code == 200, f"{resp.status_code} {resp.text}"
        _ = resp.text

    envs = [env for (conv, _h, env) in pm.get_client_calls if conv == _CHILD_SESSION_ID]
    assert envs, "the turn never asked the process manager for a harness"
    for env in envs:
        assert env is not None
        advertised = env.get("HARNESS_CLAUDE_SDK_BUNDLE_DIR")
        assert advertised == str(child), (
            f"sub-agent turn advertised bundle dir {advertised!r}; expected the "
            f"child bundle {str(child)!r}. The parent root {str(root)!r} means "
            "the resolved entry lost its workdir on a spec-cache write."
        )


def test_child_relative_local_tool_paths_root_at_the_child_bundle(tmp_path: Path) -> None:
    """Relative local-tool paths resolve under the child's bundle dir.

    Ordering pin for the turn-dispatch site: the workdir must be swapped
    to the child BEFORE local-tool paths are resolved. Resolving first
    would anchor ``tools/python/probe.py`` at the parent bundle, loading
    the parent's tool code into the child.
    """
    root = _write_agent(tmp_path / "root", "root")
    child = _write_agent(root / "agents" / "worker", "worker")
    tool_dir = child / "tools" / "python"
    tool_dir.mkdir(parents=True)
    (tool_dir / "probe.py").write_text("def probe() -> str:\n    return 'ok'\n")

    parent_entry = ResolvedSpec(spec=parse(root), workdir=root)
    child_entry = _resolve_sub_agent_spec_entry(parent_entry, "worker")
    assert child_entry is not None

    rooted = _spec_with_workdir_paths(child_entry.spec, child_entry.workdir)

    (tool,) = rooted.local_tools
    assert tool.path == str((child / "tools" / "python" / "probe.py").resolve())
    assert not tool.path.startswith(str(root / "tools"))


# ── Fallback preservation for non-sub-agent sessions ──────────


def _python_tool_spec(name: str, rel_path: str) -> AgentSpec:
    """A spec declaring one relative-path local python tool."""
    return AgentSpec(
        spec_version=1,
        name=name,
        local_tools=[LocalToolInfo(name="bundle_tool", path=rel_path, language="python")],
    )


def _write_python_tool(root: Path, rel_path: str) -> None:
    """Materialize the local python tool at ``root/rel_path``."""
    target = root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "from omnigent_client.tools import tool\n\n"
        "@tool\n"
        "def bundle_tool(text: str) -> str:\n"
        "    return text\n"
    )


@pytest.mark.asyncio
async def test_top_level_bare_spec_still_dispatches_in_runner_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare (never-resolved) spec keeps the runner-workspace fallback.

    Ordinary top-level sessions resolve to a plain ``AgentSpec`` with no
    bundle information at all. The turn-dispatch spec cache must leave
    those bare rather than wrapping them, because a wrapped entry's
    workdir is honoured verbatim — wrapping a bare spec would hand
    dispatch ``None`` and strand every local tool of every non-bundle
    agent. Only entries that actually went through resolution carry a
    verdict worth preserving.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_python_tool(workspace, "tools/python/bundle_tool.py")
    spec = _python_tool_spec("plain-agent", "tools/python/bundle_tool.py")

    captured: list[Path | None] = []

    async def _fake_dispatch(*, runner_workspace: Path | None = None, **kwargs: Any) -> str:
        del kwargs
        captured.append(runner_workspace)
        return "ok"

    monkeypatch.setattr(_tool_dispatch, "dispatch_tool_locally", _fake_dispatch)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec  # BARE — no ResolvedSpec wrapper, as non-bundle agents resolve

    pm = _FakeProcessManager(
        _ScriptedHarnessClient(
            [
                _sse({"type": "response.created", "response": {"id": "resp_1"}}),
                _sse(
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call",
                            "status": "action_required",
                            "name": "bundle_tool",
                            "call_id": "call_bundle",
                            "arguments": json.dumps({"text": "hi"}),
                        },
                    }
                ),
                _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
            ]
        )
    )
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        runner_workspace=workspace,
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions/c7f36aa769270cac30144784fad50acc/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "31ebfedf721b44dabd76f662cb70a400",
                "model": "plain-agent",
                "content": [{"type": "input_text", "text": "hi"}],
                "harness": "openai-agents",
            },
        )
        assert resp.status_code == 202
        for _ in range(100):
            if captured:
                break
            await asyncio.sleep(0.05)

    assert captured, "the local tool was never dispatched"
    assert captured[0] == workspace, (
        f"a top-level bare spec dispatched against {captured[0]!r}; expected the "
        f"runner workspace {workspace!r}. None here means the spec cache wrapped a "
        "spec that never carried bundle information, so the fallback was lost."
    )


# ── /mcp/execute (sibling site) ───────────────────────────────


async def _seed_and_execute(
    app: Any, session_id: str, harness_client: _ScriptedHarnessClient
) -> Any:
    """Seed the session spec cache with a turn, then call ``/mcp/execute``."""
    async with _runner_client(app) as client:
        seed = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "31ebfedf721b44dabd76f662cb70a400",
                "model": "probe-agent",
                "content": [{"type": "input_text", "text": "seed"}],
                "harness": "openai-agents",
            },
        )
        assert seed.status_code == 202
        for _ in range(100):
            if harness_client.posted_bodies:
                break
            await asyncio.sleep(0.05)

        return await client.post(
            f"/v1/sessions/{session_id}/mcp/execute",
            json={
                "method": "tools/call",
                "params": {"name": "bundle_tool", "arguments": {"text": "hi"}},
            },
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wrap_workdir", "expected_is_workspace"),
    [
        pytest.param(True, False, id="wrapped_bundle_dir_uses_bundle"),
        pytest.param(False, False, id="wrapped_none_gets_no_workspace_fallback"),
    ],
)
async def test_mcp_execute_spec_local_tool_honours_resolved_workdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wrap_workdir: bool,
    expected_is_workspace: bool,
) -> None:
    """``/mcp/execute`` routes spec-local tools by the RESOLVED workdir.

    This endpoint carried the same ``... else runner_workspace`` fallback
    the other sites had. For a sub-agent whose bundle dir cannot be
    resolved the cached entry is wrapped with ``workdir=None``; falling
    back to the runner workspace there runs the child against a tool tree
    its own bundle does not contain — the same leak class, one endpoint
    over.
    """
    del expected_is_workspace
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_python_tool(bundle_dir, "tools/python/bundle_tool.py")
    _write_python_tool(workspace, "tools/python/bundle_tool.py")
    spec = _python_tool_spec("probe-agent", "tools/python/bundle_tool.py")

    captured: list[Path | None] = []

    async def _fake_execute_tool(*, runner_workspace: Path | None = None, **kwargs: Any) -> str:
        del kwargs
        captured.append(runner_workspace)
        return "ok"

    monkeypatch.setattr(_tool_dispatch, "execute_tool", _fake_execute_tool)

    async def _resolver(agent_id: str, session_id: str | None = None) -> ResolvedSpec:
        del agent_id, session_id
        return ResolvedSpec(spec=spec, workdir=bundle_dir if wrap_workdir else None)

    harness_client = _ScriptedHarnessClient(
        [_sse({"type": "response.completed", "response": {"id": "resp_1"}})]
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness_client),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        runner_workspace=workspace,
    )

    resp = await _seed_and_execute(app, "38f6cf055029a2a23b227a8305f76c9d", harness_client)

    assert resp.status_code == 200, resp.text
    assert captured, "the spec-local tool was never dispatched"
    expected = bundle_dir if wrap_workdir else None
    assert captured[0] == expected, (
        f"/mcp/execute dispatched the spec-local tool against {captured[0]!r}; "
        f"expected {expected!r}. The runner workspace {workspace!r} means the "
        "endpoint still falls back instead of honouring the resolved workdir."
    )
