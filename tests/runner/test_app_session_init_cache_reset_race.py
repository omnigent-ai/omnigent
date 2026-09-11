"""Session init must not reinstate a spec entry retired by a concurrent reset.

``POST /v1/sessions`` resolves the agent spec early and memoizes it into the
session-keyed spec cache only at the very end of init. An agent-cache reset
landing in between (``POST /v1/sessions/{id}/agent-cache/reset`` — issued by
the server when the user edits the session's agent or its MCP servers) pops
the entry and bumps the session's cache generation; init must not write the
superseded entry back, or every later spec read on the session serves the
pre-reset bundle (instructions, MCP servers, local tools, bundle workdir)
until the next invalidation.

Drives the runner app over real HTTP (ASGI): init is held open inside a slow
agent-spec resolution (the way a slow bundle download holds it open), the
reset is acknowledged mid-init, then a spec-derived read
(``GET /v1/sessions/{id}/skills``) must reflect the post-reset spec.

Usage::

    pytest tests/runner/test_app_session_init_cache_reset_race.py -v
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.runner import create_runner_app
from omnigent.runner.app import get_session_agent_id
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.spec.types import AgentSpec, SkillSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient

_SESSION_ID = "cacheinit_5f0f70bd0e9f4d92a51d8bd4a3a1c9d7"
_AGENT_ID = "agentinit_1febd0be51c94b309a6f9f19c1f6f8aa"


class _SessionSnapshotServerClient(NullServerClient):
    """Server stub whose ``GET /v1/sessions/{id}`` names the bound agent.

    The post-reset spec re-resolution reads the session snapshot to find the
    agent id; the null parent's empty body would abort that read before the
    resolver is ever consulted.
    """

    class _SnapshotResponse(NullServerClient._Response):
        """Stub 200 carrying the session→agent binding."""

        def json(self) -> dict[str, Any]:
            """Return the minimal session snapshot body."""
            return {
                "id": _SESSION_ID,
                "agent_id": _AGENT_ID,
                "created_at": 1234,
                "workspace": None,
            }

    async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        """Serve the session snapshot; defer everything else to the null parent.

        :param url: Request path, e.g. ``"/v1/sessions/<id>"``.
        :param kwargs: Extra keyword arguments (forwarded to the parent).
        :returns: Snapshot response for the session URL, empty 200 otherwise.
        """
        if url.split("?", 1)[0] == f"/v1/sessions/{_SESSION_ID}":
            return self._SnapshotResponse()
        return await super().get(url, **kwargs)


def _spec(version: str) -> AgentSpec:
    """Build an agent spec carrying a version-marked, user-invocable skill.

    ``skills_filter="none"`` suppresses host-skill discovery so the skills
    read is exactly the bundled set — hermetic, independent of the dev's
    real ``~/.claude/skills/``.

    :param version: Marker distinguishing the pre-reset ("v1") from the
        post-reset ("v2") spec.
    :returns: The stub agent spec.
    """
    return AgentSpec(
        spec_version=1,
        name=f"cache-reset-race-{version}",
        instructions=f"instructions {version}",
        skills=[
            SkillSpec(
                name=f"marker-{version}",
                description=f"sentinel skill from the {version} bundle",
                content="noop",
            )
        ],
        skills_filter="none",
    )


@pytest.mark.asyncio
async def test_session_init_memoizes_spec_when_no_reset_intervenes() -> None:
    """Absent a reset, init's spec-cache write must stand (no over-fencing).

    Guards the fence added for the reset race: a session that saw no
    invalidation during init must serve later spec reads from the entry init
    memoized, without re-consulting the resolver.
    """
    resolver_calls = 0

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        nonlocal resolver_calls
        del agent_id, session_id
        resolver_calls += 1
        return _spec("v1" if resolver_calls == 1 else "v2")

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_SessionSnapshotServerClient(),  # type: ignore[arg-type]
        resource_registry=SessionResourceRegistry(terminal_registry=None),
    )

    async with _runner_client(app) as client:
        init_resp = await client.post(
            "/v1/sessions",
            json={"session_id": _SESSION_ID, "agent_id": _AGENT_ID},
        )
        assert init_resp.status_code == 201, init_resp.text
        skills_resp = await client.get(f"/v1/sessions/{_SESSION_ID}/skills")

    assert skills_resp.status_code == 200, skills_resp.text
    names = {skill["name"] for skill in skills_resp.json()["skills"]}
    assert resolver_calls == 1, (
        f"spec read after an uninterrupted init re-consulted the resolver "
        f"({resolver_calls} calls): init's memoization was wrongly suppressed"
    )
    assert names == {"marker-v1"}, f"init's resolved spec not served; skills = {sorted(names)}"


@pytest.mark.asyncio
async def test_session_init_does_not_reinstate_spec_superseded_by_reset() -> None:
    """A reset acknowledged during init wins over init's spec-cache write.

    Steps (all over the runner's HTTP API):

    1. ``POST /v1/sessions`` — init starts; agent-spec resolution is slow
       (the resolver blocks, holding init open).
    2. ``POST /v1/sessions/{id}/agent-cache/reset`` — the invalidation the
       server issues when the user edits the session's agent mid-init.
       Returns 200 with the entry dropped and the generation bumped.
    3. Resolution completes with the pre-reset ("v1") spec; init finishes.
    4. ``GET /v1/sessions/{id}/skills`` — a spec-derived read. The reset
       retired the v1 entry, so the read must re-resolve and serve the
       post-reset ("v2") spec, not the superseded entry init memoized.
    """
    resolver_entered = asyncio.Event()
    release_resolver = asyncio.Event()
    resolver_calls = 0

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        nonlocal resolver_calls
        del agent_id, session_id
        resolver_calls += 1
        if resolver_calls == 1:
            resolver_entered.set()
            await release_resolver.wait()
            return _spec("v1")
        return _spec("v2")

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_SessionSnapshotServerClient(),  # type: ignore[arg-type]
        resource_registry=SessionResourceRegistry(terminal_registry=None),
    )

    async with _runner_client(app) as client:
        init_task = asyncio.create_task(
            client.post(
                "/v1/sessions",
                json={"session_id": _SESSION_ID, "agent_id": _AGENT_ID},
            )
        )
        await asyncio.wait_for(resolver_entered.wait(), timeout=10)

        reset_resp = await client.post(
            f"/v1/sessions/{_SESSION_ID}/agent-cache/reset",
            json={"agent_id": _AGENT_ID},
        )
        assert reset_resp.status_code == 200
        assert reset_resp.json()["reset"] is True

        release_resolver.set()
        init_resp = await asyncio.wait_for(init_task, timeout=30)
        assert init_resp.status_code == 201, init_resp.text

        skills_resp = await client.get(f"/v1/sessions/{_SESSION_ID}/skills")

    assert skills_resp.status_code == 200, skills_resp.text
    names = {skill["name"] for skill in skills_resp.json()["skills"]}

    # The acknowledged reset retired the v1 entry: the next spec read must
    # consult the resolver again rather than be served init's stale write.
    assert resolver_calls == 2, (
        "spec read after an acknowledged agent-cache reset was served from the "
        "session spec cache: session init reinstated the superseded entry "
        f"(resolver consulted {resolver_calls} time(s), expected 2)"
    )
    assert "marker-v2" in names, f"post-reset spec not served; skills = {sorted(names)}"
    assert "marker-v1" not in names, (
        f"superseded pre-reset spec still served after reset; skills = {sorted(names)}"
    )


_NEW_AGENT_ID = "agentswitch_9c2c1f5f0e5a4a76a1d2b3c4d5e6f708"


class _SwitchableSnapshotServerClient(NullServerClient):
    """Server stub whose session snapshot names a switchable agent.

    Mutating :attr:`agent_id` mid-test mimics the server-side agent edit that
    accompanies a mid-init agent-cache reset, so the runner's snapshot
    fallback observably disagrees with the binding init resolved.
    """

    class _SnapshotResponse(NullServerClient._Response):
        """Stub 200 carrying a caller-supplied session snapshot body."""

        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def json(self) -> dict[str, Any]:
            """Return the session snapshot body supplied at construction."""
            return self._payload

    def __init__(self, session_id: str, agent_id: str) -> None:
        self._session_id = session_id
        self.agent_id = agent_id

    async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        """Serve the current snapshot; defer everything else to the null parent.

        :param url: Request path, e.g. ``"/v1/sessions/<id>"``.
        :param kwargs: Extra keyword arguments (forwarded to the parent).
        :returns: Snapshot response for the session URL, empty 200 otherwise.
        """
        if url.split("?", 1)[0] == f"/v1/sessions/{self._session_id}":
            return self._SnapshotResponse(
                {
                    "id": self._session_id,
                    "agent_id": self.agent_id,
                    "created_at": 1234,
                    "workspace": None,
                }
            )
        return await super().get(url, **kwargs)


@pytest.mark.asyncio
async def test_session_init_does_not_reinstate_agent_binding_superseded_by_reset() -> None:
    """A reset acknowledged during init also wins over the agent-id binding.

    ``_clear_session_agent_caches`` retires ``_session_agent_ids`` alongside
    the spec entry, and two reset paths later read that binding to pick which
    agent's shared spec-cache entry to drop (``reset_session_agent_cache``'s
    no-body fallback and ``reset_session_state``). An init that reinstated a
    superseded binding would misdirect them at the old agent, so the write is
    fenced by the same generation guard as the spec-cache memoization.

    Steps (over the runner's HTTP API, plus the module-level binding
    accessor):

    1. ``POST /v1/sessions`` with the old agent — init starts; agent-spec
       resolution blocks, holding init open.
    2. The server-side agent switch lands: the snapshot now names the new
       agent, and the accompanying ``POST /v1/sessions/{id}/agent-cache/reset``
       retires the old agent's caches mid-init.
    3. Resolution completes; init finishes on the old agent's spec.
    4. The binding must not have been reinstated, and a body-less reset must
       fall back to the fresh snapshot's (new) agent rather than pop the old
       agent's shared entry.
    """
    session_id = "cachebindrace_7a41c3d0b52e4f8fa6c1de92b30a54e1"
    resolver_entered = asyncio.Event()
    release_resolver = asyncio.Event()

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        resolver_entered.set()
        await release_resolver.wait()
        return _spec("v1")

    server_client = _SwitchableSnapshotServerClient(session_id, _AGENT_ID)
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
        resource_registry=SessionResourceRegistry(terminal_registry=None),
    )

    async with _runner_client(app) as client:
        init_task = asyncio.create_task(
            client.post(
                "/v1/sessions",
                json={"session_id": session_id, "agent_id": _AGENT_ID},
            )
        )
        await asyncio.wait_for(resolver_entered.wait(), timeout=10)

        # The user's agent edit lands mid-init: the server now binds the new
        # agent and invalidates the old agent's runner-side caches.
        server_client.agent_id = _NEW_AGENT_ID
        reset_resp = await client.post(
            f"/v1/sessions/{session_id}/agent-cache/reset",
            json={"agent_id": _AGENT_ID},
        )
        assert reset_resp.status_code == 200
        assert reset_resp.json()["reset"] is True

        release_resolver.set()
        init_resp = await asyncio.wait_for(init_task, timeout=30)
        assert init_resp.status_code == 201, init_resp.text

        assert get_session_agent_id(session_id) is None, (
            "init reinstated the agent-id binding retired by the mid-init "
            f"reset (bound {get_session_agent_id(session_id)!r})"
        )

        # The read path must not 500 while the binding is fenced out: the
        # session is live, so the runner GET serves the authoritative
        # binding from the (post-switch) server snapshot instead.
        get_resp = await client.get(f"/v1/sessions/{session_id}")
        assert get_resp.status_code == 200, get_resp.text
        assert get_resp.json()["agent_id"] == _NEW_AGENT_ID, (
            "runner GET served neither the snapshot fallback nor the fresh "
            f"agent after a raced init (got {get_resp.json()['agent_id']!r})"
        )

        fallback_resp = await client.post(
            f"/v1/sessions/{session_id}/agent-cache/reset",
            json={},
        )

    assert fallback_resp.status_code == 200
    assert fallback_resp.json()["agent_id"] == _NEW_AGENT_ID, (
        "body-less reset was misdirected at the superseded agent binding "
        f"instead of the fresh snapshot's agent (got "
        f"{fallback_resp.json()['agent_id']!r})"
    )


@pytest.mark.asyncio
async def test_session_init_memoizes_agent_binding_when_no_reset_intervenes() -> None:
    """Absent a reset, init's agent-id binding must stand (no over-fencing).

    Guards the fence added for the binding: a session that saw no
    invalidation during init must keep the binding init wrote, and a later
    body-less reset must be served from it rather than the session snapshot.
    """
    session_id = "cachebindkeep_2e95abf04c47d5a08b3c6d19e7f2ab41"

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return _spec("v1")

    server_client = _SwitchableSnapshotServerClient(session_id, _AGENT_ID)
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
        resource_registry=SessionResourceRegistry(terminal_registry=None),
    )

    async with _runner_client(app) as client:
        init_resp = await client.post(
            "/v1/sessions",
            json={"session_id": session_id, "agent_id": _AGENT_ID},
        )
        assert init_resp.status_code == 201, init_resp.text
        assert get_session_agent_id(session_id) == _AGENT_ID, (
            "uninterrupted init failed to memoize the agent-id binding: "
            "the fence was wrongly applied without a reset"
        )

        # Even if the snapshot were to disagree, the reset must be served
        # from the binding init memoized.
        server_client.agent_id = _NEW_AGENT_ID
        reset_resp = await client.post(
            f"/v1/sessions/{session_id}/agent-cache/reset",
            json={},
        )

    assert reset_resp.status_code == 200
    assert reset_resp.json()["agent_id"] == _AGENT_ID, (
        "body-less reset ignored the binding memoized by an uninterrupted "
        f"init (got {reset_resp.json()['agent_id']!r})"
    )


class _BlockingVersionServerClient(_SwitchableSnapshotServerClient):
    """Snapshot stub whose ``GET /api/version`` probe blocks until released.

    The legacy (no-envelope) init path awaits the server-version probe before
    the agent spec is ever resolved; holding that probe open exposes the
    earliest await of init to a concurrent reset.
    """

    def __init__(self, session_id: str, agent_id: str) -> None:
        super().__init__(session_id, agent_id)
        self.version_probe_entered = asyncio.Event()
        self.release_version_probe = asyncio.Event()

    async def get(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        """Block the version probe; defer everything else to the parent.

        :param url: Request path, e.g. ``"/api/version"``.
        :param kwargs: Extra keyword arguments (forwarded to the parent).
        :returns: Empty 200 for the version probe (parsed as an unknown
            version), snapshot/empty responses otherwise.
        """
        if url.split("?", 1)[0] == "/api/version":
            self.version_probe_entered.set()
            await self.release_version_probe.wait()
        return await super().get(url, **kwargs)


@pytest.mark.asyncio
async def test_session_init_fences_reset_during_legacy_context_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reset landing during the legacy-init version probe is also fenced.

    Init's first await on the no-envelope path is ``_get_server_version``'s
    ``GET /api/version`` network probe, which runs *before* the agent spec is
    resolved. The cache generation must therefore be captured before that
    load: captured any later, a reset acknowledged during the probe bumps the
    generation before the capture, the fence sees a "current" generation, and
    init reinstates the superseded spec entry and agent binding after all.

    Steps (over the runner's HTTP API):

    1. ``POST /v1/sessions`` with no init envelope — init blocks inside the
       ``/api/version`` probe, before the resolver is consulted.
    2. ``POST /v1/sessions/{id}/agent-cache/reset`` — the invalidation lands
       during that earliest await.
    3. The probe releases; init resolves the pre-reset ("v1") spec and
       finishes.
    4. A spec-derived read must re-resolve and serve the post-reset ("v2")
       spec, and the agent-id binding must not have been reinstated.
    """
    session_id = "cachelegacy_8d3f2a614b7c4e0f9a25c6d1e8b74f03"
    resolver_calls = 0

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        nonlocal resolver_calls
        del agent_id, session_id
        resolver_calls += 1
        return _spec("v1" if resolver_calls == 1 else "v2")

    server_client = _BlockingVersionServerClient(session_id, _AGENT_ID)
    # The version probe is module-memoized once it succeeds; force the miss so
    # the legacy context load genuinely awaits the (blocked) network probe.
    monkeypatch.setattr("omnigent.runner.app._server_version", None)

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
        resource_registry=SessionResourceRegistry(terminal_registry=None),
    )

    async with _runner_client(app) as client:
        init_task = asyncio.create_task(
            client.post(
                "/v1/sessions",
                json={"session_id": session_id, "agent_id": _AGENT_ID},
            )
        )
        await asyncio.wait_for(server_client.version_probe_entered.wait(), timeout=10)

        reset_resp = await client.post(
            f"/v1/sessions/{session_id}/agent-cache/reset",
            json={"agent_id": _AGENT_ID},
        )
        assert reset_resp.status_code == 200
        assert reset_resp.json()["reset"] is True

        server_client.release_version_probe.set()
        init_resp = await asyncio.wait_for(init_task, timeout=30)
        assert init_resp.status_code == 201, init_resp.text

        assert get_session_agent_id(session_id) is None, (
            "init reinstated the agent-id binding retired by a reset during "
            "the legacy context load"
        )

        skills_resp = await client.get(f"/v1/sessions/{session_id}/skills")

    assert skills_resp.status_code == 200, skills_resp.text
    names = {skill["name"] for skill in skills_resp.json()["skills"]}
    assert resolver_calls == 2, (
        "spec read after a reset acknowledged during the legacy context load "
        "was served from the session spec cache: the generation was captured "
        f"after init's first await (resolver consulted {resolver_calls} "
        "time(s), expected 2)"
    )
    assert "marker-v2" in names, f"post-reset spec not served; skills = {sorted(names)}"
    assert "marker-v1" not in names, (
        f"superseded pre-reset spec still served after reset; skills = {sorted(names)}"
    )
