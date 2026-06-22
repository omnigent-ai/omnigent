"""Tests for runner-side session resource endpoints (Phase 1a + 1b)."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.entities import DEFAULT_ENVIRONMENT_ID
from omnigent.entities.session_resources import (
    SessionResourceView,
    default_environment_resource,
    environment_safety_metadata,
    terminal_resource_id,
    terminal_resource_view,
)
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.inner.os_env import EditEntry, OpResult, OSEnvironment
from omnigent.inner.terminal import TerminalInstance
from omnigent.runner import create_runner_app
from omnigent.runner import resource_registry as resource_registry_mod
from omnigent.runner.resource_registry import (
    _CLAUDE_NATIVE_STATUS_IDLE_THRESHOLD_SECONDS,
    _CLAUDE_NATIVE_STATUS_POLL_INTERVAL_SECONDS,
    _TERMINAL_ACTIVITY_EMIT_MIN_INTERVAL_SECONDS,
    CLAUDE_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import TerminalListEntry, TerminalRegistry
from tests.unit.runner.helpers import NullServerClient, make_test_terminal_instance


@dataclass
class _FakeOSEnvironment(OSEnvironment):
    """Minimal concrete OSEnvironment for resource-list tests."""

    async def read(
        self,
        path: str,
        offset: int = 1,
        limit: int | None = None,
    ) -> OpResult:
        del path, offset, limit
        return {}

    async def write(self, path: str, content: str) -> OpResult:
        del path, content
        return {}

    async def edit(
        self,
        path: str,
        *,
        old_text: str | None = None,
        new_text: str | None = None,
        edits: Sequence[EditEntry] | None = None,
    ) -> OpResult:
        del path, old_text, new_text, edits
        return {}

    async def shell(self, command: str, timeout: int | None = None) -> OpResult:
        del command, timeout
        return {}

    def close(self) -> None:
        return None


def _make_instance(
    name: str,
    session_key: str,
    tmp_path: Path,
    *,
    running: bool = True,
    os_env: OSEnvironment | None = None,
) -> TerminalInstance:
    """
    Build a terminal instance stub for resource endpoint tests.

    :param name: Terminal name, e.g. ``"bash"``.
    :param session_key: Per-launch session key, e.g. ``"s1"``.
    :param tmp_path: Temporary directory for placeholder paths.
    :param running: Initial in-memory running flag.
    :param os_env: Optional terminal-specific OS environment.
    :returns: A test terminal instance.
    """
    return make_test_terminal_instance(
        name,
        session_key,
        tmp_path,
        running=running,
        os_env=os_env,
    )


def _seed_registry(
    registry: TerminalRegistry,
    conversation_id: str,
    instances: list[TerminalInstance],
) -> None:
    slot = registry._by_conversation.setdefault(conversation_id, {})
    for instance in instances:
        slot[(instance.name, instance.session_key)] = instance


class _CapturingResourceRegistry:
    """Resource registry stub that records terminal launch specs."""

    def __init__(self, tmp_path: Path, *, runner_workspace: Path | None = None) -> None:
        """
        Initialize the stub.

        :param tmp_path: Temporary directory used to build a fake
            terminal instance.
        :param runner_workspace: Optional workspace path returned by
            :meth:`compute_default_env_root` when no agent spec overrides it.
        :returns: None.
        """
        self.tmp_path = tmp_path
        self._runner_workspace = runner_workspace
        self.launches: list[TerminalEnvSpec] = []
        self.parent_os_envs: list[Any | None] = []
        self.resource_roles: list[str | None] = []
        self.launch_lifecycles: list[str] = []

    def set_terminal_activity_publisher(
        self,
        publisher: Callable[[str, str], None],
    ) -> None:
        """
        Accept the terminal-activity publisher installed by the runner app.

        The stub never launches a real terminal, so it just retains the
        callback (unused) to satisfy ``create_runner_app``'s wiring.

        :param publisher: Callable ``(session_id, terminal_id) -> None``.
        :returns: None.
        """
        self._terminal_activity_publisher = publisher

    def set_session_status_publisher(
        self,
        publisher: Callable[[str, str], None],
    ) -> None:
        """
        Accept the session-status publisher installed by the runner app.

        The stub never launches a real terminal, so it just retains the
        callback (unused) to satisfy ``create_runner_app``'s wiring.

        :param publisher: Callable ``(session_id, status) -> None``.
        :returns: None.
        """
        self._session_status_publisher = publisher

    def set_terminal_exit_publisher(
        self,
        publisher: Callable[[Any], None],
    ) -> None:
        """
        Accept the terminal-exit publisher installed by the runner app.

        :param publisher: Callable receiving a terminal-exit event.
        :returns: None.
        """
        self._terminal_exit_publisher = publisher

    def compute_default_env_root(self, session_id: str, agent_spec: Any) -> str | None:
        """Return the runner workspace as the default cwd, or None.

        :param session_id: Session/conversation identifier.
        :param agent_spec: Agent spec for the session (unused in stub).
        :returns: Runner workspace path string, or ``None`` when not set.
        """
        return str(self._runner_workspace) if self._runner_workspace is not None else None

    async def launch_required_terminal(
        self,
        session_id: str,
        terminal_name: str,
        session_key: str,
        spec: TerminalEnvSpec,
        cwd_override: str | None = None,
        sandbox_override: str | None = None,
        parent_os_env: Any | None = None,
        resource_role: str | None = None,
    ) -> SessionResourceView:
        """Capture a required terminal launch."""
        return await self._launch(
            "required",
            session_id=session_id,
            terminal_name=terminal_name,
            session_key=session_key,
            spec=spec,
            cwd_override=cwd_override,
            sandbox_override=sandbox_override,
            parent_os_env=parent_os_env,
            resource_role=resource_role,
        )

    async def launch_auxiliary_terminal(
        self,
        session_id: str,
        terminal_name: str,
        session_key: str,
        spec: TerminalEnvSpec,
        cwd_override: str | None = None,
        sandbox_override: str | None = None,
        parent_os_env: Any | None = None,
        resource_role: str | None = None,
    ) -> SessionResourceView:
        """Capture an auxiliary terminal launch."""
        return await self._launch(
            "auxiliary",
            session_id=session_id,
            terminal_name=terminal_name,
            session_key=session_key,
            spec=spec,
            cwd_override=cwd_override,
            sandbox_override=sandbox_override,
            parent_os_env=parent_os_env,
            resource_role=resource_role,
        )

    async def _launch(
        self,
        lifecycle: str,
        *,
        session_id: str,
        terminal_name: str,
        session_key: str,
        spec: TerminalEnvSpec,
        cwd_override: str | None = None,
        sandbox_override: str | None = None,
        parent_os_env: Any | None = None,
        resource_role: str | None = None,
    ) -> SessionResourceView:
        """
        Capture the launch spec and return a terminal resource view.

        :param lifecycle: ``"required"`` or ``"auxiliary"``.
        :param session_id: Session/conversation identifier.
        :param terminal_name: Terminal name from the request.
        :param session_key: Per-launch terminal key.
        :param spec: Terminal environment spec built by the runner.
        :param cwd_override: Optional cwd override.
        :param sandbox_override: Optional sandbox override.
        :param parent_os_env: Inherited from the agent spec; the
            stub captures it via ``parent_os_envs[-1]`` for
            assertions.
        :param resource_role: Runner-private role marker (e.g.
            ``"claude-native"``); captured via ``resource_roles[-1]``
            for assertions.
        :returns: Terminal resource view for the fake instance.
        """
        assert cwd_override is None
        assert sandbox_override is None
        self.launch_lifecycles.append(lifecycle)
        self.launches.append(spec)
        self.parent_os_envs.append(parent_os_env)
        self.resource_roles.append(resource_role)
        instance = _make_instance(terminal_name, session_key, self.tmp_path)
        return terminal_resource_view(
            session_id,
            TerminalListEntry(
                terminal_name=terminal_name,
                session_key=session_key,
                instance=instance,
            ),
        )


@pytest.fixture
def registry(tmp_path: Path) -> TerminalRegistry:
    terminal_registry = TerminalRegistry(
        conversation_link_base_url="http://127.0.0.1:8000",
    )
    terminal_env = _FakeOSEnvironment(
        spec=OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path / "terminal-root"),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
        cwd=tmp_path / "terminal-root",
    )
    _seed_registry(
        terminal_registry,
        "conv_abc",
        [
            _make_instance("bash", "s1", tmp_path, os_env=terminal_env),
            _make_instance("python", "s2", tmp_path),
            _make_instance("stale", "s3", tmp_path, running=False),
        ],
    )
    _seed_registry(
        terminal_registry,
        "conv_other",
        [_make_instance("other", "s1", tmp_path)],
    )
    return terminal_registry


@pytest.fixture
def app(registry: TerminalRegistry) -> FastAPI:
    return create_runner_app(
        terminal_registry=registry,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
        yield c


@pytest.mark.asyncio
async def test_session_resources_new_session_lists_default_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_runner_app(
        runner_workspace=workspace,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
        resp = await c.get("/v1/sessions/conv_new/resources")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "object": "list",
        "data": [
            {
                "id": DEFAULT_ENVIRONMENT_ID,
                "object": "session.resource",
                "type": "environment",
                "session_id": "conv_new",
                "name": "Primary environment",
                "metadata": {
                    "environment_type": "caller_process",
                    "role": "primary",
                },
            }
        ],
        "first_id": DEFAULT_ENVIRONMENT_ID,
        "last_id": DEFAULT_ENVIRONMENT_ID,
        "has_more": False,
    }


@pytest.mark.asyncio
async def test_session_resources_include_running_terminal_resources(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/v1/sessions/conv_abc/resources?order=asc")

    assert resp.status_code == 200
    body = resp.json()
    ids = [resource["id"] for resource in body["data"]]
    assert ids == [
        DEFAULT_ENVIRONMENT_ID,
        "terminal_bash_s1",
        "env_terminal_bash_s1",
        "terminal_python_s2",
    ]
    assert "terminal_stale_s3" not in ids


@pytest.mark.asyncio
async def test_session_terminal_resources_point_to_actual_environment(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/v1/sessions/conv_abc/resources?order=asc")

    assert resp.status_code == 200
    resources = {resource["id"]: resource for resource in resp.json()["data"]}
    assert resources["terminal_bash_s1"]["environment"] == "env_terminal_bash_s1"
    assert resources["terminal_python_s2"]["environment"] == DEFAULT_ENVIRONMENT_ID
    terminal_env = resources["env_terminal_bash_s1"]
    assert terminal_env["type"] == "environment"
    assert terminal_env["metadata"]["environment_type"] == "caller_process"
    assert terminal_env["metadata"]["role"] == "terminal"
    assert terminal_env["metadata"]["terminal_name"] == "bash"
    assert terminal_env["metadata"]["session_key"] == "s1"
    assert terminal_env["metadata"]["root"].endswith("terminal-root")


@pytest.mark.asyncio
async def test_session_resources_are_scoped_by_session(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/v1/sessions/conv_other/resources?order=asc")

    assert resp.status_code == 200
    ids = [resource["id"] for resource in resp.json()["data"]]
    assert ids == [DEFAULT_ENVIRONMENT_ID, "terminal_other_s1"]


# ── Phase 1b: typed collections ─────────────────────────────────


@pytest.mark.asyncio
async def test_list_environments_returns_only_environment_resources(
    client: httpx.AsyncClient,
) -> None:
    """GET /resources/environments filters to environment type only."""
    resp = await client.get("/v1/sessions/conv_abc/resources/environments")

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    types = {r["type"] for r in body["data"]}
    assert types == {"environment"}
    ids = [r["id"] for r in body["data"]]
    assert DEFAULT_ENVIRONMENT_ID in ids
    assert "env_terminal_bash_s1" in ids
    assert "terminal_bash_s1" not in ids


@pytest.mark.asyncio
async def test_list_terminals_returns_only_terminal_resources(
    client: httpx.AsyncClient,
) -> None:
    """GET /resources/terminals filters to terminal type only."""
    resp = await client.get("/v1/sessions/conv_abc/resources/terminals")

    assert resp.status_code == 200
    body = resp.json()
    types = {r["type"] for r in body["data"]}
    assert types == {"terminal"}
    ids = [r["id"] for r in body["data"]]
    assert "terminal_bash_s1" in ids
    assert "terminal_python_s2" in ids
    assert DEFAULT_ENVIRONMENT_ID not in ids


@pytest.mark.asyncio
async def test_get_environment_by_id_returns_default(
    client: httpx.AsyncClient,
) -> None:
    """GET /resources/environments/default returns the primary env."""
    resp = await client.get(
        f"/v1/sessions/conv_abc/resources/environments/{DEFAULT_ENVIRONMENT_ID}"
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == DEFAULT_ENVIRONMENT_ID
    assert body["type"] == "environment"
    assert body["metadata"]["role"] == "primary"


class _SwitchableServerClient:
    """AP-server stub whose session snapshot reports a mutable ``agent_id``.

    Flipping :attr:`agent_id` simulates an in-place agent switch rebinding
    the conversation to a new agent. ``GET /v1/sessions/{id}`` is the only
    call the spec-resolution path makes; POST/PATCH are stubbed for any
    incidental runner calls.
    """

    class _Response:
        """Minimal 200 response carrying a fixed JSON body."""

        def __init__(self, body: dict[str, Any]) -> None:
            """:param body: JSON body returned by :meth:`json`."""
            self.status_code = 200
            self._body = body

        def json(self) -> dict[str, Any]:
            """:returns: The fixed JSON body."""
            return self._body

        def raise_for_status(self) -> None:
            """No-op: the stub always succeeds."""

    def __init__(self, workspace: str) -> None:
        """:param workspace: Absolute workspace path reported in the snapshot."""
        self.agent_id = "agent_a"
        self._workspace = workspace

    async def get(self, url: str, **kwargs: Any) -> _SwitchableServerClient._Response:
        """Report the session snapshot with the current ``agent_id`` binding.

        :param url: Request URL (ignored — only the session GET is exercised).
        :param kwargs: Extra kwargs (ignored).
        :returns: A 200 snapshot whose ``agent_id`` reflects the current binding.
        """
        del url, kwargs
        return self._Response(
            {"created_at": 0.0, "workspace": self._workspace, "agent_id": self.agent_id}
        )

    async def post(self, url: str, **kwargs: Any) -> _SwitchableServerClient._Response:
        """Stub POST returning an empty 200."""
        del url, kwargs
        return self._Response({})

    async def patch(self, url: str, **kwargs: Any) -> _SwitchableServerClient._Response:
        """Stub PATCH returning an empty 200."""
        del url, kwargs
        return self._Response({})


@pytest.mark.asyncio
async def test_reset_state_rematerializes_env_from_new_agent_spec(tmp_path: Path) -> None:
    """``POST /reset-state`` makes the next filesystem access resolve the
    NEW agent's spec — and therefore its ``os_env`` / sandbox.

    Regression guard for the in-place agent-switch sandbox fix. The web
    filesystem/shell endpoints materialize the primary OSEnv from the
    resolved agent spec (cached in ``_session_spec_cache``, keyed via the
    snapshot's ``agent_id``). ``compute_default_env_root`` is handed that
    same resolved spec, so capturing its argument tells us which agent is
    live. Without the cache invalidation in ``reset-state``, the second
    GET would still see ``agent_a`` (the bug: the env rebuilds from the
    stale old spec and the new sandbox never applies). The flip to
    ``agent_b`` proves the new agent's os_env now governs these endpoints.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Two agents with DISTINCT sandboxes so the captured spec is unambiguous.
    spec_a = AgentSpec(
        spec_version=1,
        name="agent_a",
        os_env=OSEnvSpec(type="caller_process", cwd=".", sandbox=OSEnvSandboxSpec(type="none")),
    )
    spec_b = AgentSpec(
        spec_version=1,
        name="agent_b",
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=".",
            sandbox=OSEnvSandboxSpec(type="linux_bwrap"),
        ),
    )

    async def _spec_resolver(agent_id: str, session_id: str | None) -> AgentSpec:
        """Resolve agent_a→spec_a, agent_b→spec_b (the switch target)."""
        del session_id
        return spec_a if agent_id == "agent_a" else spec_b

    terminal_registry = TerminalRegistry(conversation_link_base_url="http://127.0.0.1:8000")
    registry = SessionResourceRegistry(
        terminal_registry=terminal_registry,
        runner_workspace=workspace,
        per_session_workspace=False,
    )
    # Capture the resolved spec each time the env root is computed without
    # disturbing the real resolution logic.
    captured_specs: list[Any] = []
    _orig_compute_root = registry.compute_default_env_root

    def _recording_compute_root(session_id: str, agent_spec: Any) -> str | None:
        """Record the resolved spec, then delegate to the real method."""
        captured_specs.append(agent_spec)
        return _orig_compute_root(session_id, agent_spec)

    registry.compute_default_env_root = _recording_compute_root  # type: ignore[method-assign]

    server = _SwitchableServerClient(str(workspace.resolve()))
    app = create_runner_app(
        server_client=server,  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
        resource_registry=registry,
        spec_resolver=_spec_resolver,
        runner_workspace=workspace,
        per_session_workspace=False,
    )

    env_path = f"/v1/sessions/conv_switch/resources/environments/{DEFAULT_ENVIRONMENT_ID}"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
        resp1 = await c.get(env_path)
        assert resp1.status_code == 200, resp1.text
        # First access resolved agent_a (sandbox=none). A different value here
        # would mean spec resolution is broken, not the switch path.
        assert captured_specs[-1].name == "agent_a"
        assert captured_specs[-1].os_env.sandbox.type == "none"

        # Simulate the in-place switch: rebind the conversation to agent_b on
        # the server, then run the switch's runner-side reset.
        server.agent_id = "agent_b"
        reset = await c.post("/v1/sessions/conv_switch/reset-state")
        assert reset.status_code == 200, reset.text
        assert reset.json()["reset"] is True

        resp2 = await c.get(env_path)
        assert resp2.status_code == 200, resp2.text

    # After the reset the next access resolved agent_b (sandbox=linux_bwrap).
    # If reset-state had NOT dropped the spec/snapshot caches, this would
    # still be agent_a/"none" — the cross-agent sandbox leak this guards.
    assert captured_specs[-1].name == "agent_b"
    assert captured_specs[-1].os_env.sandbox.type == "linux_bwrap"


@pytest.mark.asyncio
async def test_reset_state_closes_terminals_and_publishes_deleted(tmp_path: Path) -> None:
    """``POST /reset-state`` (in-place agent switch) closes the session's
    terminals AND announces each close with ``session.resource.deleted``.

    Regression guard for the switch-agent stale-terminal bug: the
    switch's runner-side reset used to pop terminals from the registry
    silently (``cleanup_session`` emits no events), so the web UI —
    whose terminal list is SSE-primary — kept showing the old agent's
    dead terminal, and attaching to it failed with "terminal resource
    not found or not running".
    """
    from omnigent.runner.app import _session_event_queues_ref

    conv_id = "conv_switch_term_teardown"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    terminal_registry = TerminalRegistry(conversation_link_base_url="http://127.0.0.1:8000")
    # Seed the old agent's live terminal (the debby CLI's ``tui:main``).
    # Private-attr seed matches the existing resource-registry test
    # convention (no real tmux).
    _seed_registry(terminal_registry, conv_id, [_make_instance("tui", "main", tmp_path)])
    registry = SessionResourceRegistry(
        terminal_registry=terminal_registry,
        runner_workspace=workspace,
        per_session_workspace=False,
    )

    async def _spec_resolver(agent_id: str, session_id: str | None) -> AgentSpec:
        """Return a minimal spec; reset-state never resolves it."""
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="any")

    app = create_runner_app(
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
        resource_registry=registry,
        spec_resolver=_spec_resolver,
        runner_workspace=workspace,
        per_session_workspace=False,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
        # Precondition: the terminal is live before the reset, so a later
        # absence proves the reset closed it (not that it was never there).
        assert terminal_registry.get(conv_id, "tui", "main") is not None

        reset = await c.post(f"/v1/sessions/{conv_id}/reset-state")
        assert reset.status_code == 200, reset.text
        assert reset.json()["reset"] is True

        queue = _session_event_queues_ref.get(conv_id)
        queued_events: list[dict[str, Any]] = []
        while queue is not None and not queue.empty():
            item = queue.get_nowait()
            if isinstance(item, dict):
                queued_events.append(item)

    # The terminal is gone from the registry → the /resources/terminals
    # list the web UI seeds from no longer shows it. Still present =
    # reset-state skipped the teardown entirely.
    assert terminal_registry.get(conv_id, "tui", "main") is None, (
        "reset-state must close the session's terminals; this one is "
        "still registered, so the web UI would keep listing it."
    )

    # Exactly one session.resource.deleted for the closed terminal so
    # connected clients drop it live (the server relay also persists it).
    # 0 = reset closed the terminal silently (the stale-tab bug this
    # guards against); 2+ = double-publish (teardown ran twice).
    deleted = [e for e in queued_events if e.get("type") == "session.resource.deleted"]
    assert deleted == [
        {
            "type": "session.resource.deleted",
            "resource_id": "terminal_tui_main",
            "resource_type": "terminal",
            "session_id": conv_id,
        }
    ], f"expected one terminal session.resource.deleted event, got {deleted!r}"


@pytest.mark.asyncio
async def test_get_environment_reports_root_and_home(tmp_path: Path) -> None:
    """Default env GET carries metadata.root AND metadata.home.

    The Web UI needs ``home`` to expand a leading ``~`` in agent-mentioned
    paths and resolve them against ``root``. ``home`` is the runner process's
    own home — the same one the agent's ``~`` expands to. The endpoint only
    emits it when ``os.path.expanduser("~")`` resolves to an absolute path;
    when it can't (``~`` left literal), the field is omitted, so the
    assertion matches that conditional behavior.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_runner_app(
        runner_workspace=workspace,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
        resp = await c.get(
            f"/v1/sessions/conv_new/resources/environments/{DEFAULT_ENVIRONMENT_ID}"
        )

    assert resp.status_code == 200
    metadata = resp.json()["metadata"]
    # root resolves under the configured runner workspace (a per-session subdir
    # when isolated); a value outside it means compute_default_env_root
    # regressed, not the home addition.
    assert metadata["root"].startswith(str(workspace.resolve()))
    # home is the runner's own home — the value the agent's ``~`` expands to.
    # Mirror the endpoint's guard: emitted only when expanduser yields an
    # absolute path; omitted entirely when it can't resolve ``~``.
    expanded_home = os.path.expanduser("~")
    if os.path.isabs(expanded_home):
        assert metadata["home"] == expanded_home
    else:
        assert "home" not in metadata


@pytest.mark.asyncio
async def test_get_environment_returns_404_for_unknown(
    client: httpx.AsyncClient,
) -> None:
    """GET /resources/environments/{bad_id} returns 404."""
    resp = await client.get("/v1/sessions/conv_abc/resources/environments/nonexistent")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_get_terminal_by_id(
    client: httpx.AsyncClient,
) -> None:
    """GET /resources/terminals/{id} returns the terminal resource."""
    resp = await client.get("/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "terminal_bash_s1"
    assert body["type"] == "terminal"
    assert body["environment"] == "env_terminal_bash_s1"
    assert body["metadata"]["terminal_name"] == "bash"
    assert body["metadata"]["tmux_socket"].endswith("bash-s1.sock")
    assert body["metadata"]["tmux_target"] == "main"


@pytest.mark.asyncio
async def test_get_terminal_by_id_returns_404_when_tmux_exited(
    client: httpx.AsyncClient,
    registry: TerminalRegistry,
) -> None:
    """
    GET verifies live tmux state instead of trusting a stale flag.

    This is the signal ``omnigent claude`` uses after its attach
    WebSocket closes cleanly. If this endpoint returns 200 after the
    command's tmux server has exited, the native wrapper treats normal
    Claude exit as a server bounce and reconnects forever.
    """
    instance = registry.get("conv_abc", "bash", "s1")
    assert instance is not None

    async def dead_tmux() -> bool:
        """
        Simulate :meth:`TerminalInstance.is_alive` observing tmux gone.

        :returns: ``False`` after marking the optimistic flag stale.
        """
        instance.running = False
        return False

    instance.is_alive = dead_tmux  # type: ignore[method-assign]

    resp = await client.get("/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1")

    # 404 is the definitive "terminal gone" probe result consumed by
    # claude-native. A 200 here would preserve the reconnect loop bug.
    assert resp.status_code == 404
    assert instance.running is False


@pytest.mark.asyncio
async def test_transfer_terminal_moves_resource_without_closing(
    client: httpx.AsyncClient,
    registry: TerminalRegistry,
) -> None:
    """
    POST /resources/terminals/{id}/transfer reparents the terminal.

    This catches the ``/clear`` bug class where moving ownership would
    accidentally close tmux or leave the runner registry keyed under
    the old conversation, making the new Omnigent conversation unable to
    attach to the still-running Claude pane.
    """
    source = registry.get("conv_abc", "bash", "s1")
    assert source is not None

    resp = await client.post(
        "/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1/transfer",
        json={"target_session_id": "conv_new"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "terminal_bash_s1"
    assert body["session_id"] == "conv_new"
    assert body["metadata"]["terminal_name"] == "bash"
    assert registry.get("conv_abc", "bash", "s1") is None
    assert registry.get("conv_new", "bash", "s1") is source
    assert source.conversation_link == "http://127.0.0.1:8000/c/conv_new"
    assert source.running is True


@pytest.mark.asyncio
async def test_get_terminal_returns_404_for_unknown(
    client: httpx.AsyncClient,
) -> None:
    """GET /resources/terminals/{bad_id} returns 404."""
    resp = await client.get("/v1/sessions/conv_abc/resources/terminals/terminal_nope_s1")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_resource_by_id_generic(
    client: httpx.AsyncClient,
) -> None:
    """GET /resources/{id} returns any resource type."""
    resp_env = await client.get(f"/v1/sessions/conv_abc/resources/{DEFAULT_ENVIRONMENT_ID}")
    assert resp_env.status_code == 200
    assert resp_env.json()["type"] == "environment"

    resp_term = await client.get("/v1/sessions/conv_abc/resources/terminal_bash_s1")
    assert resp_term.status_code == 200
    assert resp_term.json()["type"] == "terminal"


@pytest.mark.asyncio
async def test_get_resource_returns_404_for_unknown(
    client: httpx.AsyncClient,
) -> None:
    """GET /resources/{bad_id} returns 404."""
    resp = await client.get("/v1/sessions/conv_abc/resources/nonexistent")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_typed_routes_not_captured_as_resource_id(
    client: httpx.AsyncClient,
) -> None:
    """Verify that 'environments' and 'terminals' are not captured
    as resource_id by the generic GET /resources/{resource_id} route.
    """
    resp_env = await client.get("/v1/sessions/conv_abc/resources/environments")
    assert resp_env.status_code == 200
    assert resp_env.json()["object"] == "list"

    resp_term = await client.get("/v1/sessions/conv_abc/resources/terminals")
    assert resp_term.status_code == 200
    assert resp_term.json()["object"] == "list"


@pytest.mark.asyncio
async def test_list_resources_type_filter(
    client: httpx.AsyncClient,
) -> None:
    """GET /resources?type=terminal returns only terminals."""
    resp = await client.get("/v1/sessions/conv_abc/resources?type=terminal&order=asc")
    assert resp.status_code == 200
    body = resp.json()
    types = {r["type"] for r in body["data"]}
    assert types == {"terminal"}


# ── Phase 1b: terminal lifecycle ────────────────────────────────


@pytest.mark.asyncio
async def test_delete_terminal_closes_and_returns_confirmation(
    client: httpx.AsyncClient,
    registry: TerminalRegistry,
) -> None:
    """DELETE /resources/terminals/{id} closes the terminal."""
    resp = await client.delete("/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "terminal_bash_s1"
    assert body["object"] == "session.resource.deleted"
    assert body["deleted"] is True

    instance = registry.get("conv_abc", "bash", "s1")
    assert instance is None or not instance.running


@pytest.mark.asyncio
async def test_delete_terminal_returns_404_for_unknown(
    client: httpx.AsyncClient,
) -> None:
    """DELETE /resources/terminals/{bad_id} returns 404."""
    resp = await client.delete("/v1/sessions/conv_abc/resources/terminals/terminal_nope_s1")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_create_terminal_uses_runner_workspace_as_default_cwd(
    tmp_path: Path,
) -> None:
    """
    Runner-created terminals default to the local runner workspace.

    This is the resource endpoint used by ``omnigent claude``. The
    request body intentionally avoids embedding the client's cwd, so
    the runner must supply its own workspace default before launching
    tmux.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resource_registry = _CapturingResourceRegistry(tmp_path, runner_workspace=workspace)
    app = create_runner_app(
        resource_registry=resource_registry,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
        resp = await c.post(
            "/v1/sessions/conv_abc/resources/terminals",
            json={
                "terminal": "claude",
                "session_key": "main",
                "spec": {
                    "command": "claude",
                    "args": ["--resume", "native"],
                    "os_env_type": "caller_process",
                },
            },
        )

    assert resp.status_code == 200
    # Exactly one captured launch proves the runner endpoint forwarded
    # this create request once; zero would mean the terminal was never
    # launched, and more than one would mean duplicate terminal spawns.
    assert len(resource_registry.launches) == 1
    launch = resource_registry.launches[0]
    assert isinstance(launch.os_env, OSEnvSpec)
    assert launch.os_env.cwd == str(workspace)
    assert launch.command == "claude"
    assert launch.args == ["--resume", "native"]
    assert launch.log_file is None


@pytest.mark.asyncio
async def test_create_terminal_forwards_tmux_passthrough_opt_in(
    tmp_path: Path,
) -> None:
    """
    Terminal launch requests can opt into tmux passthrough.

    Codex native uses this internal knob so its TUI can query the
    attached terminal's background color through tmux. A regression in
    the runner request decoder would silently drop the flag before the
    terminal process launches.

    :param tmp_path: Temporary directory used for the fake registry.
    :returns: None.
    """
    resource_registry = _CapturingResourceRegistry(tmp_path)
    app = create_runner_app(
        resource_registry=resource_registry,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
        resp = await c.post(
            "/v1/sessions/conv_abc/resources/terminals",
            json={
                "terminal": "codex",
                "session_key": "main",
                "spec": {
                    "command": "codex",
                    "os_env_type": "caller_process",
                    "tmux_allow_passthrough": True,
                    "tmux_start_on_attach": True,
                },
            },
        )

    assert resp.status_code == 200
    assert len(resource_registry.launches) == 1
    assert resource_registry.launches[0].tmux_allow_passthrough is True
    assert resource_registry.launches[0].tmux_start_on_attach is True


@pytest.mark.asyncio
async def test_create_terminal_threads_agent_parent_os_env_through(
    tmp_path: Path,
) -> None:
    """The REST terminal-create endpoint must thread the agent's
    ``os_env`` (with its sandbox / egress_rules / env_passthrough)
    through as ``parent_os_env``.

    Regression: the previous implementation built a fresh
    ``TerminalEnvSpec`` from the body with **no** sandbox at all,
    so every REST-launched terminal ran completely outside the
    agent's configured sandbox — operator/API callers (e.g. the
    ``omnigent claude`` wrapper) could spawn an unsandboxed
    terminal in a session whose YAML declared an egress allow-list.
    """
    from omnigent.inner.datamodel import AgentDef, OSEnvSandboxSpec, OSEnvSpec

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent = AgentDef(
        name="hardened",
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=OSEnvSandboxSpec(
                type="darwin_seatbelt",
                egress_rules=["* api.github.com/**"],
                env_passthrough=["DATABRICKS_TOKEN"],
            ),
        ),
    )

    async def _session_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/conv_test"
        return httpx.Response(200, json={"id": "conv_test", "agent_id": "agent_hardened"})

    async def _resolver(agent_id: str, session_id: str) -> AgentDef:
        assert agent_id == "agent_hardened"
        assert session_id == "conv_test"
        return agent

    server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_session_handler),
        base_url="http://server",
    )
    resource_registry = _CapturingResourceRegistry(tmp_path, runner_workspace=workspace)
    app = create_runner_app(
        resource_registry=resource_registry,
        server_client=server_client,
        spec_resolver=_resolver,
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        server_client,
        httpx.AsyncClient(transport=transport, base_url="http://runner") as c,
    ):
        resp = await c.post(
            "/v1/sessions/conv_test/resources/terminals",
            json={
                "terminal": "claude",
                "session_key": "main",
                "spec": {"command": "claude", "os_env_type": "caller_process"},
            },
        )

    assert resp.status_code == 200
    assert len(resource_registry.launches) == 1
    # parent_os_env is the agent's os_env, so build_terminal_os_env_spec
    # can inherit the agent's sandbox (egress_rules, env_passthrough,
    # etc.) for the launched terminal.
    parent_os_env = resource_registry.parent_os_envs[0]
    assert parent_os_env is agent.os_env
    # The synthesised TerminalEnvSpec also carries the agent's sandbox
    # by reference, so even paths that don't use parent_os_env still
    # apply egress_rules.
    launch = resource_registry.launches[0]
    assert launch.os_env.sandbox is not None
    assert launch.os_env.sandbox.egress_rules == ["* api.github.com/**"]


@pytest.mark.asyncio
async def test_create_terminal_uses_declared_terminal_spec_over_body(
    tmp_path: Path,
) -> None:
    """When the agent YAML declares a terminal with the requested
    name, the runner uses that operator-blessed spec verbatim and
    ignores the body's command/args/sandbox.

    Closes a previously unaudited API surface where a REST caller
    could spawn the YAML-declared terminal name with a completely
    different command.
    """
    from omnigent.inner.datamodel import (
        AgentDef,
        OSEnvSandboxSpec,
        OSEnvSpec,
        TerminalEnvSpec,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    declared_zsh = TerminalEnvSpec(
        command="zsh",
        args=["-l"],
        env={"OPERATOR": "set"},
        os_env="inherit",
    )
    agent = AgentDef(
        name="hardened",
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=OSEnvSandboxSpec(type="darwin_seatbelt"),
        ),
        terminals={"sandboxed_zsh": declared_zsh},
    )

    async def _session_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "conv_test", "agent_id": "agent_hardened"})

    async def _resolver(agent_id: str, session_id: str) -> AgentDef:
        return agent

    server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_session_handler),
        base_url="http://server",
    )
    resource_registry = _CapturingResourceRegistry(tmp_path, runner_workspace=workspace)
    app = create_runner_app(
        resource_registry=resource_registry,
        server_client=server_client,
        spec_resolver=_resolver,
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        server_client,
        httpx.AsyncClient(transport=transport, base_url="http://runner") as c,
    ):
        # Attempt to spawn the declared terminal name with attacker-
        # picked command and args. The declared spec must win.
        resp = await c.post(
            "/v1/sessions/conv_test/resources/terminals",
            json={
                "terminal": "sandboxed_zsh",
                "session_key": "s1",
                "spec": {
                    "command": "bash",
                    "args": ["-c", "curl evil.example.com"],
                    "env": {"INJECTED": "true"},
                },
            },
        )

    assert resp.status_code == 200
    launch = resource_registry.launches[0]
    assert launch is declared_zsh
    assert launch.command == "zsh"
    assert launch.args == ["-l"]
    assert launch.env == {"OPERATOR": "set"}


@pytest.mark.asyncio
async def test_create_terminal_publishes_bridge_tmux_target(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Claude terminal launch writes tmux attach metadata for web chat injection.

    The seeded ``bash/s1`` terminal is already running, so this route
    exercises the bridge-publish branch without spawning tmux. The
    regression this catches was using a nonexistent registry method:
    the terminal existed, but the launch request failed with a 500
    while trying to write ``tmux.json``.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "claude-native")

    resp = await client.post(
        "/v1/sessions/conv_abc/resources/terminals",
        json={
            "terminal": "bash",
            "session_key": "s1",
            "spec": {
                "command": "bash",
                "os_env_type": "caller_process",
            },
            "bridge_inject_dir": True,
        },
    )

    assert resp.status_code == 200
    from omnigent.claude_native_bridge import bridge_dir_for_conversation_id

    derived = bridge_dir_for_conversation_id("conv_abc")
    payload = json.loads((derived / "tmux.json").read_text(encoding="utf-8"))
    assert payload["socket_path"] == str(tmp_path / "bash-s1.sock")
    assert payload["tmux_target"] == "main"


@pytest.mark.asyncio
async def test_create_terminal_ignores_client_supplied_bridge_path(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A client-supplied ``bridge_inject_dir`` path does not redirect the write.

    Pins the directory-traversal fix: a regression that resurrects
    ``Path(body["bridge_inject_dir"])`` would let the attacker path
    below win, and ``tmux.json`` (which carries a live tmux socket)
    would land under it instead of the session-derived directory.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "claude-native")

    attacker_path = tmp_path / "attacker-controlled-dir"
    attacker_path.mkdir()

    resp = await client.post(
        "/v1/sessions/conv_abc/resources/terminals",
        json={
            "terminal": "bash",
            "session_key": "s1",
            "spec": {
                "command": "bash",
                "os_env_type": "caller_process",
            },
            "bridge_inject_dir": str(attacker_path),
        },
    )

    assert resp.status_code == 200
    assert not (attacker_path / "tmux.json").exists()
    from omnigent.claude_native_bridge import bridge_dir_for_conversation_id

    derived = bridge_dir_for_conversation_id("conv_abc")
    assert (derived / "tmux.json").exists()


@pytest.mark.asyncio
async def test_list_resources_does_not_materialize_primary_env(
    tmp_path: Path,
) -> None:
    """GET /resources must not create the primary OSEnvironment as a side-effect.

    The listing endpoint is logical/lazy per SESSION_RESOURCES_API_DESIGN.md.
    After the request, ``_primary_envs`` on the registry must still be empty.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    resource_registry = SessionResourceRegistry(runner_workspace=workspace)
    app = create_runner_app(
        resource_registry=resource_registry,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
        resp = await c.get("/v1/sessions/conv_lazy/resources?order=asc")

    assert resp.status_code == 200
    # The primary OSEnvironment must NOT have been materialized.
    assert resource_registry._primary_envs == {}


# ---------------------------------------------------------------------------
# Environment share-safety metadata
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sandbox_spec,expected_type,expected_active",
    [
        (None, "none", False),
        (OSEnvSandboxSpec(type="none"), "none", False),
        (OSEnvSandboxSpec(type="linux_bwrap"), "linux_bwrap", True),
        (OSEnvSandboxSpec(type="darwin_seatbelt"), "darwin_seatbelt", True),
    ],
    ids=["no_sandbox", "type_none", "bwrap", "seatbelt"],
)
def test_environment_safety_metadata_reflects_sandbox(
    sandbox_spec: OSEnvSandboxSpec | None,
    expected_type: str,
    expected_active: bool,
) -> None:
    """``sandbox_active`` is True only when a real sandbox backend confines the env."""
    spec = OSEnvSpec(type="caller_process", cwd="/ws", sandbox=sandbox_spec)
    meta = environment_safety_metadata(spec)
    assert meta["sandbox_type"] == expected_type
    assert meta["sandbox_active"] is expected_active
    assert meta["environment_type"] == "caller_process"


def test_environment_safety_metadata_preserves_non_caller_process_type() -> None:
    spec = OSEnvSpec(
        type="lakebox",
        cwd="/workspace",
        sandbox=OSEnvSandboxSpec(type="none"),
    )
    meta = environment_safety_metadata(spec)
    assert meta["environment_type"] == "lakebox"
    assert meta["sandbox_type"] == "none"
    assert meta["sandbox_active"] is False


def test_environment_safety_metadata_none_spec_is_empty() -> None:
    """A ``None`` spec yields ``{}`` so the legacy projection is preserved."""
    assert environment_safety_metadata(None) == {}


def test_default_environment_resource_merges_safety_metadata() -> None:
    """A resolved spec adds sandbox fields while keeping ``role: primary``."""
    spec = OSEnvSpec(
        type="caller_process",
        cwd="/ws",
        sandbox=OSEnvSandboxSpec(type="linux_bwrap"),
    )
    resource = default_environment_resource("conv_x", spec)
    assert resource.metadata["role"] == "primary"
    assert resource.metadata["sandbox_type"] == "linux_bwrap"
    assert resource.metadata["sandbox_active"] is True


def test_default_environment_resource_without_spec_is_legacy_shape() -> None:
    """Without a spec the resource keeps the exact legacy metadata (backward compat)."""
    resource = default_environment_resource("conv_x")
    assert resource.metadata == {
        "environment_type": "caller_process",
        "role": "primary",
    }


@pytest.mark.asyncio
async def test_list_resources_primary_env_carries_sandbox_metadata(
    tmp_path: Path,
) -> None:
    """list_resources threads the agent spec's sandbox onto the primary env resource."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = SessionResourceRegistry(runner_workspace=workspace)
    agent_spec = SimpleNamespace(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=OSEnvSandboxSpec(type="linux_bwrap"),
        ),
    )
    page = registry.list_resources("conv_sandbox", agent_spec=agent_spec)
    primary = next(r for r in page.data if r.id == DEFAULT_ENVIRONMENT_ID)
    assert primary.metadata["sandbox_type"] == "linux_bwrap"
    assert primary.metadata["sandbox_active"] is True


@dataclass
class _StatusEdge:
    """One session-status edge captured from the status publisher.

    :param session_id: Session id the edge was published for, e.g.
        ``"conv_x"``.
    :param status: The published working status, ``"running"`` or
        ``"idle"``.
    """

    session_id: str
    status: str


@dataclass
class _WatcherCapture:
    """Records the callbacks the registry wires onto a terminal's watcher.

    Stands in for the real daemon thread so the test can invoke the
    activity/idle edges synchronously instead of polling tmux.

    :param started: Whether ``start_idle_watcher_thread`` was called.
    :param on_activity: The activity-edge callback the registry passed,
        or ``None`` if none was wired.
    :param on_idle: The idle-edge callback the registry passed, or
        ``None`` if none was wired.
    :param on_exit: The exit callback the registry passed, or ``None`` if none
        was wired.
    :param idle_threshold_s: The per-watcher idle threshold the registry
        passed, or ``None`` for the module default.
    :param poll_interval_s: The per-watcher poll interval the registry
        passed, or ``None`` for the module default.
    """

    started: bool = False
    on_activity: Callable[[], None] | None = None
    on_idle: Callable[[], None] | None = None
    on_exit: Callable[[], None] | None = None
    idle_threshold_s: float | None = None
    poll_interval_s: float | None = None
    replace: bool = False


def _make_capturing_instance(
    tmp_path: Path,
    capture: _WatcherCapture,
    *,
    name: str,
    session_key: str,
) -> TerminalInstance:
    """Build a terminal instance whose watcher start is captured, not run.

    Shadows :meth:`TerminalInstance.start_idle_watcher_thread` on the
    instance with a recorder so the test can drive the activity/idle
    edges by hand and assert what the registry wired — without spawning
    the real tmux-polling daemon thread.

    :param tmp_path: Temporary directory for the instance's placeholder
        socket/dir paths.
    :param capture: Recorder the shadow method writes the wired callbacks
        and threshold into.
    :param name: Terminal name, e.g. ``"claude"``.
    :param session_key: Per-launch session key, e.g. ``"main"``.
    :returns: The instance with a capturing ``start_idle_watcher_thread``.
    """
    instance = make_test_terminal_instance(name, session_key, tmp_path)

    def _capture(
        on_idle: Callable[[], None] | None = None,
        *,
        on_activity: Callable[[], None] | None = None,
        on_exit: Callable[[], None] | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        capture.started = True
        capture.on_idle = on_idle
        capture.on_activity = on_activity
        capture.on_exit = on_exit
        capture.idle_threshold_s = idle_threshold_s
        capture.poll_interval_s = poll_interval_s
        capture.replace = replace

    # Instance attribute shadows the bound method, so the registry's call
    # lands on the recorder (no real daemon thread / tmux poll).
    instance.start_idle_watcher_thread = _capture  # type: ignore[method-assign]
    return instance


class _LaunchReturningRegistry:
    """Terminal-registry stub whose ``launch`` returns a fixed instance.

    The real terminal launch helpers only call
    ``launch`` on its terminal registry; returning a prepared instance
    lets the test exercise the real ``_start_terminal_activity_watcher``
    wiring without spawning a terminal.

    :param instance: The instance every ``launch`` returns.
    """

    def __init__(self, instance: TerminalInstance) -> None:
        self._instance = instance

    async def launch(
        self,
        *,
        conversation_id: str,
        terminal_name: str,
        session_key: str,
        spec: TerminalEnvSpec,
        parent_os_env: Any | None = None,
        cwd_override: str | None = None,
        sandbox_override: str | None = None,
    ) -> TerminalInstance:
        """Return the fixed instance, ignoring the launch spec.

        :param conversation_id: Session/conversation id (unused).
        :param terminal_name: Terminal name (unused).
        :param session_key: Per-launch key (unused).
        :param spec: Terminal env spec (unused).
        :param parent_os_env: Agent os_env (unused).
        :param cwd_override: cwd override (unused).
        :param sandbox_override: sandbox override (unused).
        :returns: The prepared terminal instance.
        """
        del (
            conversation_id,
            terminal_name,
            session_key,
            spec,
            parent_os_env,
            cwd_override,
            sandbox_override,
        )
        return self._instance


def _claude_terminal_spec(tmp_path: Path) -> TerminalEnvSpec:
    """Build a minimal terminal spec for the status-wiring tests.

    The launch spec is ignored by :class:`_LaunchReturningRegistry`, so
    only its construction needs to be valid.

    :param tmp_path: Directory used as the terminal cwd.
    :returns: A minimal :class:`TerminalEnvSpec`.
    """
    return TerminalEnvSpec(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
        command="claude",
    )


@pytest.mark.asyncio
async def test_claude_native_terminal_drives_session_status_from_pane_activity(
    tmp_path: Path,
) -> None:
    """The claude-native agent terminal's pane edges drive session status.

    Launching with ``CLAUDE_NATIVE_TERMINAL_ROLE`` must wire the idle
    edge (with the short claude-native status threshold) and translate
    pane activity → ``running`` / quiescence → ``idle``, deduped to the
    transition so a continuously-redrawing pane doesn't spam ``running``.
    This is the PTY-activity-derived status that replaces the hook-based
    ``UserPromptSubmit``/``Stop`` bracketing.
    """
    capture = _WatcherCapture()
    instance = _make_capturing_instance(tmp_path, capture, name="claude", session_key="main")
    registry = SessionResourceRegistry(terminal_registry=_LaunchReturningRegistry(instance))
    status_edges: list[_StatusEdge] = []
    registry.set_terminal_activity_publisher(lambda _sid, _tid: None)
    registry.set_session_status_publisher(
        lambda sid, status: status_edges.append(_StatusEdge(session_id=sid, status=status))
    )

    await registry.launch_required_terminal(
        session_id="conv_x",
        terminal_name="claude",
        session_key="main",
        spec=_claude_terminal_spec(tmp_path),
        resource_role=CLAUDE_NATIVE_TERMINAL_ROLE,
    )

    # The agent terminal wires the idle edge with the short status
    # threshold and the fast (200ms) poll interval; absence here would mean
    # status never flips to idle (the regression the hook-based path
    # suffered on interrupts) or transitions lag at the 1s default.
    assert capture.started is True
    assert capture.on_idle is not None
    assert capture.on_activity is not None
    assert capture.idle_threshold_s == _CLAUDE_NATIVE_STATUS_IDLE_THRESHOLD_SECONDS
    assert capture.poll_interval_s == _CLAUDE_NATIVE_STATUS_POLL_INTERVAL_SECONDS

    # Two pane changes in one working stretch must yield exactly one
    # ``running`` edge — the dedupe holds. A second ``running`` here would
    # mean the idle→running edge isn't being coalesced.
    capture.on_activity()
    capture.on_activity()
    await asyncio.sleep(0)  # let the call_soon_threadsafe publishes run
    assert [e.status for e in status_edges] == ["running"]

    # Quiescence → idle, and a repeat idle tick must not re-emit. A second
    # ``idle`` would mean the edge isn't deduped.
    capture.on_idle()
    capture.on_idle()
    await asyncio.sleep(0)
    assert [e.status for e in status_edges] == ["running", "idle"]

    # New pane output after idle re-arms ``running`` — this is the case
    # the hook path missed (e.g. compaction resume / typing after a turn).
    capture.on_activity()
    await asyncio.sleep(0)
    assert [e.status for e in status_edges] == ["running", "idle", "running"]
    # Every edge was published for the launching session, never a stray id.
    assert all(e.session_id == "conv_x" for e in status_edges)


@pytest.mark.asyncio
async def test_generic_terminal_does_not_drive_session_status(
    tmp_path: Path,
) -> None:
    """A non-agent terminal's pane activity must not move session status.

    A side shell (no ``CLAUDE_NATIVE_TERMINAL_ROLE``) still drives the
    terminal-activity badge, but its output must never flip the session's
    working status — otherwise typing in an unrelated terminal would show
    the agent as "running".
    """
    capture = _WatcherCapture()
    instance = _make_capturing_instance(tmp_path, capture, name="zsh", session_key="s1")
    registry = SessionResourceRegistry(terminal_registry=_LaunchReturningRegistry(instance))
    status_edges: list[_StatusEdge] = []
    activity_pulses: list[str] = []
    registry.set_terminal_activity_publisher(lambda _sid, tid: activity_pulses.append(tid))
    registry.set_session_status_publisher(
        lambda sid, status: status_edges.append(_StatusEdge(session_id=sid, status=status))
    )

    await registry.launch_auxiliary_terminal(
        session_id="conv_y",
        terminal_name="zsh",
        session_key="s1",
        spec=_claude_terminal_spec(tmp_path),
        # No resource_role → generic terminal.
    )

    # Generic terminals get the activity badge but NOT the idle→status
    # wiring, and keep the module defaults (not the claude threshold or the
    # fast poll interval).
    assert capture.started is True
    assert capture.on_activity is not None
    assert capture.on_idle is None
    assert capture.idle_threshold_s is None
    assert capture.poll_interval_s is None

    capture.on_activity()
    await asyncio.sleep(0)
    # Badge still pulses once for the side shell...
    assert len(activity_pulses) == 1
    # ...but session status is never touched by a non-agent terminal.
    assert status_edges == []


@pytest.mark.asyncio
async def test_terminal_activity_pulses_throttled_to_one_per_second(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Activity emission is throttled to one pulse per second per terminal.

    The claude-native pane watcher polls every 200ms and Claude redraws
    its busy line on nearly every poll, so ``_on_activity`` fires ~5x/sec
    while a turn runs. Without throttling that would push ~5
    ``session.terminal.activity`` events/second; the watcher must coalesce
    them to at most one per
    :data:`_TERMINAL_ACTIVITY_EMIT_MIN_INTERVAL_SECONDS`. A fresh idle
    edge resets the throttle so the next working episode pulses
    immediately rather than lagging up to a second behind the
    running-status edge.

    A fake monotonic clock makes the throttle window deterministic — the
    real watcher's 200ms poll spacing is replaced by hand-advanced time so
    we drive the exact sub-second / cross-second boundaries.

    :param tmp_path: Temporary directory for the terminal instance.
    :param monkeypatch: Patches the module-local ``_monotonic`` indirection
        so the throttle reads a controllable clock (never the wall clock).
    """
    clock = {"now": 1000.0}
    # Patch the module-local indirection, not time.monotonic itself, so the
    # fake clock can't bleed into other threads/tests (per testing rule 14).
    monkeypatch.setattr(resource_registry_mod, "_monotonic", lambda: clock["now"])

    capture = _WatcherCapture()
    instance = _make_capturing_instance(tmp_path, capture, name="claude", session_key="main")
    registry = SessionResourceRegistry(terminal_registry=_LaunchReturningRegistry(instance))
    activity_pulses: list[str] = []
    registry.set_terminal_activity_publisher(lambda _sid, tid: activity_pulses.append(tid))
    # The idle-reset behaviour is only wired for the claude-native role, so
    # the status publisher must be installed to exercise on_idle.
    registry.set_session_status_publisher(lambda _sid, _status: None)

    await registry.launch_required_terminal(
        session_id="conv_throttle",
        terminal_name="claude",
        session_key="main",
        spec=_claude_terminal_spec(tmp_path),
        resource_role=CLAUDE_NATIVE_TERMINAL_ROLE,
    )
    assert capture.on_activity is not None
    assert capture.on_idle is not None

    # Three pane changes inside one poll-spaced instant (clock frozen) →
    # exactly one pulse. Without the throttle each changed tick emits, so a
    # count of 3 here would mean the per-second coalescing was dropped.
    capture.on_activity()
    capture.on_activity()
    capture.on_activity()
    await asyncio.sleep(0)  # let the call_soon_threadsafe publish run
    assert len(activity_pulses) == 1, (
        f"Expected 1 throttled pulse for 3 same-instant ticks, got "
        f"{len(activity_pulses)}. >1 means the activity emit is not "
        f"coalesced and fires on every pane-changed tick."
    )

    # Still inside the throttle window (advance < the min interval) → no new
    # pulse. A 2nd pulse here would mean the window is too short / ignored.
    clock["now"] += _TERMINAL_ACTIVITY_EMIT_MIN_INTERVAL_SECONDS / 2
    capture.on_activity()
    await asyncio.sleep(0)
    assert len(activity_pulses) == 1, (
        "A pulse fired inside the throttle window — the min-interval guard is not being applied."
    )

    # Cross the throttle boundary → the next changed tick emits again. If
    # this stays at 1 the throttle never re-opens and the badge would go
    # dark mid-turn.
    clock["now"] += _TERMINAL_ACTIVITY_EMIT_MIN_INTERVAL_SECONDS
    capture.on_activity()
    await asyncio.sleep(0)
    assert len(activity_pulses) == 2, (
        f"Expected a 2nd pulse after crossing the throttle window, got "
        f"{len(activity_pulses)}. Still 1 means the window never re-opens."
    )

    # An idle edge resets the throttle: the next working episode pulses
    # immediately even though we're still inside the window relative to the
    # last emit. Without the reset this on_activity would be throttled and
    # the count would stay at 2.
    capture.on_idle()
    clock["now"] += _TERMINAL_ACTIVITY_EMIT_MIN_INTERVAL_SECONDS / 4
    capture.on_activity()
    await asyncio.sleep(0)
    assert len(activity_pulses) == 3, (
        f"Expected an immediate pulse after an idle reset, got "
        f"{len(activity_pulses)}. Still 2 means on_idle did not clear the "
        f"activity throttle, so a new episode lags behind the status edge."
    )
    # Every pulse carried this terminal's resource id, never a stray one.
    assert set(activity_pulses) == {"terminal_claude_main"}


@pytest.mark.asyncio
async def test_concurrent_resource_reads_share_one_session_snapshot(
    tmp_path: Path,
) -> None:
    """A startup burst of concurrent resource reads resolves the
    session's spec through one ``GET /v1/sessions/{id}`` and one bundle
    resolution, instead of each request stampeding the Omnigent server.

    Reproduces the observed runner-startup burst: dozens of paired
    ``GET /sessions/{id}`` + ``agent/contents`` requests in one second.
    The runner now funnels ``created_at`` / ``workspace`` / ``agent_id``
    through a single-flighted snapshot loader, and ``_resolve_session_
    spec_entry`` is single-flighted too, so concurrent readers share one
    resolution.

    Determinism: request A is confirmed parked inside the (blocked)
    snapshot fetch — holding the per-session spec + snapshot locks —
    before the followers are created. A holds the spec lock continuously
    from before the followers exist until after it writes the cache, so
    no follower can acquire the lock with an empty cache; each must reuse
    A's result.

    :param tmp_path: Temporary runner workspace root.
    :returns: None.
    """
    conv = "conv_burst"
    agent_id = "ag_burst"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    snapshot_count = 0
    snapshot_started = asyncio.Event()
    release = asyncio.Event()

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """
        Stub Omnigent server that blocks + counts the session snapshot fetch.

        :param request: Outbound request from the runner.
        :returns: The session snapshot (after release) for the session
            GET; benign payloads otherwise.
        """
        nonlocal snapshot_count
        if request.method == "GET" and request.url.path == f"/v1/sessions/{conv}":
            snapshot_count += 1
            snapshot_started.set()
            await release.wait()
            return httpx.Response(
                200,
                json={"id": conv, "agent_id": agent_id, "workspace": str(workspace)},
            )
        return httpx.Response(200, json={})

    resolver_count = 0

    async def _spec_resolver(resolved_agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Count bundle resolutions (the ``agent/contents`` dedup target).

        :param resolved_agent_id: Agent id read from the shared snapshot;
            MUST equal ``agent_id`` or the snapshot projection is wrong.
        :param session_id: Session id (unused).
        :returns: A minimal spec.
        """
        nonlocal resolver_count
        resolver_count += 1
        # The agent_id can only have come from the shared snapshot.
        assert resolved_agent_id == agent_id
        return AgentSpec(
            spec_version=1,
            name="burst-agent",
            executor=ExecutorSpec(type="omnigent", config={"harness": "runner-test-default"}),
        )

    server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    )
    app = create_runner_app(
        runner_workspace=workspace,
        spec_resolver=_spec_resolver,
        server_client=server_client,
    )
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
            first = asyncio.create_task(c.get(f"/v1/sessions/{conv}/resources"))
            # A is now parked inside the blocked snapshot fetch, holding
            # both per-session locks.
            await asyncio.wait_for(snapshot_started.wait(), timeout=5.0)
            followers = [
                asyncio.create_task(c.get(f"/v1/sessions/{conv}/resources")) for _ in range(8)
            ]
            release.set()
            responses = await asyncio.gather(first, *followers)
    finally:
        await server_client.aclose()

    # Every concurrent read succeeded — the shared resolution is correct
    # under contention, not merely rare.
    assert all(r.status_code == 200 for r in responses)
    # The 9 concurrent reads hit the server snapshot exactly once. Without
    # single-flight on the snapshot loader, each request that missed the
    # empty cache would issue its own GET (the observed burst → 9 here).
    assert snapshot_count == 1, (
        f"expected one shared snapshot GET, got {snapshot_count}; >1 means "
        f"concurrent readers stampeded the cache instead of sharing one fetch"
    )
    # The bundle resolved once too. >1 means _resolve_session_spec_entry is
    # not single-flighted and each caller re-fetched agent/contents.
    assert resolver_count == 1, (
        f"expected one shared bundle resolution, got {resolver_count}; >1 "
        f"means the spec stampede (the agent/contents half) is not deduped"
    )


@pytest.mark.asyncio
async def test_failed_session_snapshot_is_not_cached_and_retries(
    tmp_path: Path,
) -> None:
    """A transient non-200 snapshot is not memoized: a later read
    refetches and resolves once the session is reachable.

    Guards the retry-until-bound path — the agent may bind to the session
    after the runner first looks. If the failed snapshot were negatively
    cached, spec resolution would raise forever and the session could
    never recover.

    :param tmp_path: Temporary runner workspace root.
    :returns: None.
    """
    conv = "conv_retry"
    agent_id = "ag_retry"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    snapshot_count = 0
    resolver_count = 0

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """
        Stub Omnigent server: fail the first snapshot, succeed afterward.

        :param request: Outbound request from the runner.
        :returns: 503 on the first session GET, then the real snapshot.
        """
        nonlocal snapshot_count
        if request.method == "GET" and request.url.path == f"/v1/sessions/{conv}":
            snapshot_count += 1
            if snapshot_count == 1:
                return httpx.Response(503, json={})
            return httpx.Response(
                200,
                json={"id": conv, "agent_id": agent_id, "workspace": str(workspace)},
            )
        return httpx.Response(200, json={})

    async def _spec_resolver(resolved_agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Resolve a minimal spec, counting how often it runs.

        :param resolved_agent_id: Agent id from the snapshot (unused
            beyond the count).
        :param session_id: Session id (unused).
        :returns: A minimal spec.
        """
        nonlocal resolver_count
        resolver_count += 1
        return AgentSpec(
            spec_version=1,
            name="retry-agent",
            executor=ExecutorSpec(type="omnigent", config={"harness": "runner-test-default"}),
        )

    server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    )
    app = create_runner_app(
        runner_workspace=workspace,
        spec_resolver=_spec_resolver,
        server_client=server_client,
    )
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
            first = await c.get(f"/v1/sessions/{conv}/resources")
            second = await c.get(f"/v1/sessions/{conv}/resources")
    finally:
        await server_client.aclose()

    # First read hit the 503 snapshot → spec resolution raised
    # OmnigentError → the endpoint returned a non-200 error response.
    assert first.status_code != 200
    # Second read refetched the (now 200) snapshot and resolved → 200.
    assert second.status_code == 200
    # snapshot_count == 2 proves the 503 was NOT negatively cached; 1 would
    # mean the failed snapshot stuck and the session could never recover.
    assert snapshot_count == 2, (
        f"expected a refetch after the transient 503, got {snapshot_count} snapshot fetch(es)"
    )
    # The bundle resolved only on the successful attempt.
    assert resolver_count == 1, (
        f"expected one bundle resolution (success only), got {resolver_count}"
    )


@pytest.mark.asyncio
async def test_unbound_agent_snapshot_is_not_cached_and_retries(
    tmp_path: Path,
) -> None:
    """A 200 snapshot whose ``agent_id`` is still null is not memoized:
    a later read refetches and resolves once the agent binds.

    This is the harder retry-until-bound case than the non-200 one — the
    session GET succeeds (HTTP 200) before the agent binds, so the
    snapshot is "ok" but incomplete. If it were cached on ``ok`` alone,
    the stale ``agent_id=None`` would latch and spec resolution would
    raise ``NOT_FOUND`` forever, because the snapshot cache never
    refreshes on server-side binding.

    :param tmp_path: Temporary runner workspace root.
    :returns: None.
    """
    conv = "conv_unbound"
    agent_id = "ag_late_bind"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    snapshot_count = 0
    resolver_count = 0

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """
        Stub Omnigent server: 200 with no agent_id first, then the bound one.

        :param request: Outbound request from the runner.
        :returns: A 200 snapshot lacking ``agent_id`` on the first
            session GET, then one carrying ``agent_id``.
        """
        nonlocal snapshot_count
        if request.method == "GET" and request.url.path == f"/v1/sessions/{conv}":
            snapshot_count += 1
            if snapshot_count == 1:
                # Session exists, agent not bound yet (agent_id null).
                return httpx.Response(200, json={"id": conv, "workspace": str(workspace)})
            return httpx.Response(
                200,
                json={"id": conv, "agent_id": agent_id, "workspace": str(workspace)},
            )
        return httpx.Response(200, json={})

    async def _spec_resolver(resolved_agent_id: str, session_id: str | None = None) -> AgentSpec:
        """
        Resolve a minimal spec, counting how often it runs.

        :param resolved_agent_id: Agent id from the snapshot; only the
            bound value can reach here.
        :param session_id: Session id (unused).
        :returns: A minimal spec.
        """
        nonlocal resolver_count
        resolver_count += 1
        assert resolved_agent_id == agent_id
        return AgentSpec(
            spec_version=1,
            name="late-bind-agent",
            executor=ExecutorSpec(type="omnigent", config={"harness": "runner-test-default"}),
        )

    server_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    )
    app = create_runner_app(
        runner_workspace=workspace,
        spec_resolver=_spec_resolver,
        server_client=server_client,
    )
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
            first = await c.get(f"/v1/sessions/{conv}/resources")
            second = await c.get(f"/v1/sessions/{conv}/resources")
    finally:
        await server_client.aclose()

    # First read saw the unbound (agent_id null) snapshot → spec
    # resolution raised NOT_FOUND → non-200 error response.
    assert first.status_code != 200
    # Second read refetched the now-bound snapshot and resolved → 200.
    assert second.status_code == 200
    # snapshot_count == 2 proves the unbound 200 was NOT cached; 1 would
    # mean agent_id=None latched and the session could never bind.
    assert snapshot_count == 2, (
        f"expected a refetch after the unbound snapshot, got {snapshot_count} "
        f"snapshot fetch(es); 1 means the agent_id=None snapshot was cached "
        f"and retry-until-bound is broken"
    )
    # The bundle resolved only once the agent had bound.
    assert resolver_count == 1, (
        f"expected one bundle resolution (after binding), got {resolver_count}"
    )


@pytest.mark.asyncio
async def test_claude_terminal_ensure_concurrent_calls_create_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent ensure_native_terminal requests create the Claude terminal exactly once.

    The ``_ensure_native_terminal_ready`` path from the AP server fires
    ``POST /v1/sessions/{id}/resources/terminals?ensure_native_terminal=True``
    every time a user message arrives on a cold claude-native session.  A
    concurrent ``_on_runner_connect`` callback fires a ``POST /v1/sessions``
    that also calls ``_auto_create_claude_terminal`` under
    ``_claude_terminal_ensure_locks``.  Without the per-session lock on the
    terminals endpoint, both requests pass the "no terminal yet" check
    simultaneously and both call ``_auto_create_claude_terminal`` — spawning
    two forwarders that double-persist every transcript item (the
    double-bubble bug).  The fix wraps the terminals-endpoint check-and-create
    in the same ``_claude_terminal_ensure_locks`` key as the sessions path,
    so the second request waits, then finds the terminal already registered
    and returns early without a second create.

    Proof strategy:

    * The fake ``_auto_create_claude_terminal`` blocks on an asyncio.Event
      while holding the lock, giving a deterministic window for the second
      request to arrive.
    * Both requests fire concurrently; the first holds the lock while the
      second waits.
    * After the first completes and registers the fake terminal view,
      the second acquires the lock, finds the terminal, and returns early.
    * ``create_count == 1`` proves the second request took the early-return
      path; ``2`` would mean the lock is absent and both requests created a
      terminal (the pre-fix bug).

    :param tmp_path: Temp directory (not used by the terminal stub but
        required by the fixture signature).
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    session_id = f"conv_{uuid.uuid4().hex[:12]}"

    # Fake terminal view returned by _auto_create_claude_terminal and later
    # by get_terminal_resource once the first create completes.
    fake_view = SessionResourceView(
        id=terminal_resource_id("claude", "main"),
        type="terminal",
        session_id=session_id,
        name="Claude",
    )

    # Concurrency gate: the fake create blocks until released, keeping the
    # per-session lock held so the second request has time to arrive.
    first_call_started = asyncio.Event()
    first_call_released = asyncio.Event()
    create_count = 0
    terminal_ready = False  # set by fake create so get_terminal_resource sees it

    async def fake_auto_create(
        s_id: str,
        reg: Any,
        pub: Any,
        **kwargs: Any,
    ) -> SessionResourceView:
        """Stub that signals entry, blocks until released, then marks the terminal ready.

        :param s_id: Session id from the caller.
        :param reg: Resource registry (unused in stub).
        :param pub: Publish-event callback (unused in stub).
        :param kwargs: Extra keyword arguments (ignored).
        :returns: The fake terminal view.
        """
        nonlocal create_count, terminal_ready
        create_count += 1
        first_call_started.set()
        await first_call_released.wait()
        terminal_ready = True
        return fake_view

    monkeypatch.setattr("omnigent.runner.app._auto_create_claude_terminal", fake_auto_create)

    async def fake_get_terminal_resource(
        self: Any,
        s_id: str,
        t_id: str,
    ) -> SessionResourceView | None:
        """Return the fake view once the first create has completed, else None.

        :param self: SessionResourceRegistry instance (unused).
        :param s_id: Session id (unused).
        :param t_id: Terminal resource id (unused).
        :returns: The fake view when ``terminal_ready`` is True, else ``None``.
        """
        del self, s_id, t_id
        if terminal_ready:
            return fake_view
        return None

    monkeypatch.setattr(
        SessionResourceRegistry,
        "get_terminal_resource",
        fake_get_terminal_resource,
    )

    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        ensure_body = {
            "terminal": "claude",
            "session_key": "main",
            "ensure_native_terminal": True,
        }

        # Start request 1: enters the lock, calls fake_auto_create (blocks on
        # first_call_released), which holds the lock.
        task1 = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json=ensure_body,
            )
        )

        # Yield to the event loop so task1 runs until it blocks inside the lock.
        await first_call_started.wait()

        # Start request 2: tries to acquire the same per-session lock, blocks
        # because task1 holds it.
        task2 = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json=ensure_body,
            )
        )

        # Give task2 a chance to reach the lock and suspend there.
        await asyncio.sleep(0.02)

        # Release task1: it sets terminal_ready=True and exits the lock.  Task2
        # then acquires the lock, sees get_terminal_resource return the fake view,
        # and returns the existing terminal without calling _auto_create_claude_terminal.
        first_call_released.set()

        resp1, resp2 = await asyncio.gather(task1, task2)

    assert resp1.status_code == 200, f"Request 1 failed: {resp1.status_code} {resp1.text}"
    assert resp2.status_code == 200, f"Request 2 failed: {resp2.status_code} {resp2.text}"

    # _auto_create_claude_terminal must be called exactly once.
    # If 2: the per-session lock on the terminals endpoint is absent — both
    # concurrent requests passed the "no terminal" check and both spawned a
    # Claude terminal, reproducing the double-forwarder / double-bubble bug.
    assert create_count == 1, (
        f"Expected _auto_create_claude_terminal to be called once, got {create_count}. "
        "A value of 2 means both concurrent ensure requests spawned a Claude terminal "
        "(the pre-fix double-forwarder race)."
    )
