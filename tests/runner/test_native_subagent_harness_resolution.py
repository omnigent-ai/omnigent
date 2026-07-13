"""Regression repro: native sub-agent harness must survive a cache refill.

Reproduces the production failure where polly (``claude-sdk``) spawns a
``claude_code`` sub-agent (``claude-native``) and the web UI then shows
*"Bridge closed: terminal resource not found or not running"*.

Root cause (see investigation notes): the child's harness is resolved by
swapping the parent spec to the named sub-agent's sub-spec
(``_find_spec_by_name``). That swap is gated on the in-memory dict
``_session_sub_agent_names``, populated ONLY in ``POST /v1/sessions`` from
``body["sub_agent_name"]``. The streaming dispatch path
(``_stream_message_to_harness`` -> ``_resolve_harness_config``) skips the
swap entirely: it derives the harness from the session's bound ``agent_id``
(the *parent* polly agent) and so resolves ``claude-sdk``. After a tunnel
reconnect / spec-cache eviction (when the in-memory map is empty), a turn
that takes this path asks ``HarnessProcessManager.get_client`` for
``claude-sdk``; the manager sees the harness change
``claude-native -> claude-sdk`` and respawns, tearing down the live
claude-native tmux terminal. The UI's bridge to ``terminal_claude_main``
then fails with the observed error.

The server already persists and returns ``sub_agent_name`` on
``GET /v1/sessions/{id}`` (``SessionResponse.sub_agent_name``); the runner
just drops it. The invariant under test: **a turn dispatched for a
sub-agent session asks the process manager to spawn the CHILD's harness
(``claude-native``) even when the in-memory sub-agent-name map is empty**
(the post-reconnect state), because the identity is recoverable from the
server snapshot.

This test FAILS on the buggy commit (the runner asks for ``claude-sdk``)
and PASSES once the streaming dispatch path is made sub-agent-aware.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec

# Reuse the proven harness/process-manager/client stubs from the sessions-native
# suite so this repro drives the exact same dispatch path the runner uses.
from tests.runner.test_app_sessions_native import (
    NullServerClient,
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)

PARENT_AGENT_ID = "ag_polly"
CHILD_SESSION_ID = "conv_child_claude_code"
SUB_AGENT_NAME = "claude_code"


def _polly_spec_tree() -> AgentSpec:
    """Parent polly (claude-sdk) with a claude_code (claude-native) child.

    Mirrors ``examples/polly/config.yaml`` + its ``claude_code`` sub-agent.
    """
    child = AgentSpec(
        spec_version=1,
        name=SUB_AGENT_NAME,
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )
    return AgentSpec(
        spec_version=1,
        name="polly",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        sub_agents=[child],
    )


async def _parent_spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
    """Resolve any agent_id to the parent polly tree (as the live server does).

    A sub-agent session is bound to its *parent* agent_id server-side, so the
    runner's spec_resolver returns the parent tree; only the sub-agent-name
    swap turns it into the child spec.
    """
    del agent_id, session_id
    return _polly_spec_tree()


class _SubAgentSnapshotServer(NullServerClient):
    """Server client whose ``GET /v1/sessions/{child}`` carries sub_agent_name.

    Mirrors ``SessionResponse.sub_agent_name`` (server routes/sessions.py): the
    authoritative source the runner can use to recover the sub-agent identity
    after the in-memory ``_session_sub_agent_names`` map is lost (e.g. after a
    tunnel reconnect). All other endpoints fall through to the empty-200 base.
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
        # The bare session GET carries the snapshot the runner needs.
        if url.rstrip("/").endswith(CHILD_SESSION_ID):
            return self._Resp(
                {
                    "agent_id": PARENT_AGENT_ID,
                    "sub_agent_name": SUB_AGENT_NAME,
                    "parent_session_id": "conv_parent_polly",
                    "created_at": 0,
                    "workspace": None,
                }
            )
        if url.rstrip("/").endswith("/items"):
            return self._Resp({"data": [], "has_more": False})
        return super()._Response()


@pytest.mark.asyncio
async def test_subagent_turn_spawns_child_native_harness_without_prior_post() -> None:
    """A turn for a sub-agent session must spawn the CHILD (claude-native) harness.

    Drives ``POST /v1/sessions/{child}/events?stream=true`` with NO prior
    ``POST /v1/sessions`` — exactly the post-reconnect state where the runner's
    in-memory ``_session_sub_agent_names`` map is empty. The runner must still
    ask the process manager for ``claude-native`` (the child's harness),
    recovering ``sub_agent_name`` from the server snapshot.

    Buggy commit: the runner derives the harness from the bound parent
    ``agent_id`` and asks for ``claude-sdk`` -> the manager respawns the
    process, killing the native terminal -> "Bridge closed".
    """
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "r1"}}),
            _sse({"type": "response.completed", "response": {"id": "r1"}}),
        ]
    )
    pm = _FakeProcessManager(hc)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_parent_spec_resolver,
        server_client=_SubAgentSnapshotServer(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{CHILD_SESSION_ID}/events",
            params={"stream": "true"},
            json={
                "type": "message",
                "role": "user",
                "agent_id": PARENT_AGENT_ID,
                "content": [{"type": "input_text", "text": "hi"}],
            },
        )
        assert resp.status_code == 200, f"{resp.status_code} {resp.text}"
        # Drain the SSE body so the streaming turn fully runs get_client.
        _ = resp.text

    harnesses = [h for (_conv, h, _env) in pm.get_client_calls]
    assert harnesses, "the turn never asked the process manager for a harness"
    assert all(h == "claude-native" for h in harnesses), (
        f"runner asked the process manager to spawn {harnesses!r} for the "
        "sub-agent session; expected only 'claude-native'. A 'claude-sdk' "
        "spawn is the bug: it respawns the harness and tears down the live "
        "claude-native terminal ('Bridge closed: terminal resource not found')."
    )


@pytest.mark.asyncio
async def test_subagent_background_turn_resolves_child_native_harness() -> None:
    """The fire-and-forget turn path must also resolve the CHILD harness.

    ``POST /v1/sessions/{child}/events`` with ``stream=false`` runs the
    PRIMARY turn path (``_run_turn_bg`` -> ``_run_turn_bg_setup_and_stream``),
    which derives the harness from the (swapped) cached spec and bakes it
    into the ``TurnDispatch``. That path reads the sub-agent name to perform
    the swap; with the in-memory map empty (post-reconnect) it must recover
    the name from the server snapshot, otherwise it bakes the PARENT
    ``claude-sdk`` harness and the process manager respawns the child's
    ``claude-native`` terminal away ("Bridge closed").

    This covers the gap the streaming test above does not: the background
    path computes the harness itself rather than deferring to
    ``_resolve_harness_config``.
    """
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "r1"}}),
            _sse({"type": "response.completed", "response": {"id": "r1"}}),
        ]
    )
    pm = _FakeProcessManager(hc)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_parent_spec_resolver,
        server_client=_SubAgentSnapshotServer(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{CHILD_SESSION_ID}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": PARENT_AGENT_ID,
                "content": [{"type": "input_text", "text": "hi"}],
            },
        )
        # Fire-and-forget returns 202; the background turn runs get_client.
        assert resp.status_code == 202, f"{resp.status_code} {resp.text}"

    # Let the background turn task reach get_client.
    for _ in range(200):
        if pm.get_client_calls:
            break
        await asyncio.sleep(0.01)

    harnesses = [h for (_conv, h, _env) in pm.get_client_calls]
    assert harnesses, "the background turn never asked the process manager for a harness"
    assert all(h == "claude-native" for h in harnesses), (
        f"background turn asked the process manager to spawn {harnesses!r} for "
        "the sub-agent session; expected only 'claude-native'. A 'claude-sdk' "
        "spawn is the bug: it respawns the harness and tears down the live "
        "claude-native terminal ('Bridge closed: terminal resource not found')."
    )


class _CatchUpServer(_SubAgentSnapshotServer):
    """Like the snapshot server, but ``/items`` returns one fresh user message.

    The catch-up scan only starts a turn when new user items arrived after the
    last seen cursor — this provides exactly one so the scan dispatches.
    """

    async def get(self, url: str, **kwargs: Any) -> Any:
        del kwargs
        u = url.rstrip("/")
        if u.endswith(f"{CHILD_SESSION_ID}/items"):
            return self._Resp(
                {
                    "data": [
                        {
                            "id": "item_new1",
                            "type": "message",
                            "data": {
                                "role": "user",
                                "content": [{"type": "input_text", "text": "continue"}],
                            },
                        }
                    ],
                    "has_more": False,
                }
            )
        if u.endswith(CHILD_SESSION_ID):
            return self._Resp(
                {
                    "agent_id": PARENT_AGENT_ID,
                    "sub_agent_name": SUB_AGENT_NAME,
                    "parent_session_id": "conv_parent_polly",
                    "created_at": 0,
                    "workspace": None,
                }
            )
        if u.endswith("/items"):
            return self._Resp({"data": [], "has_more": False})
        return super(_SubAgentSnapshotServer, self)._Response()


@pytest.mark.asyncio
async def test_reconnect_catch_up_scan_keeps_child_native_harness() -> None:
    """The reconnect catch-up scan must not flip a sub-agent off claude-native.

    This drives the REAL ``app.state.catch_up_scan`` (the runner's
    ``on_reconnect`` callback) — the exact path that fired in production after
    a Databricks Apps ingress WebSocket recycle. With the sub-agent session
    known to the runner (it has history) but its spec cache holding the PARENT
    spec, ``_is_native_harness`` returns False on the buggy code, so the scan
    does NOT skip it, runs a catch-up turn, and resolves the parent
    ``claude-sdk`` harness — asking ``get_client`` for ``claude-sdk`` while the
    live subprocess is ``claude-native`` (the respawn that kills the terminal).

    The fix recovers ``sub_agent_name`` from the server snapshot so the turn
    resolves ``claude-native`` instead.
    """
    from omnigent.runner.app import _session_histories_ref

    pm = _FakeProcessManager(
        _ScriptedHarnessClient(
            [
                _sse({"type": "response.created", "response": {"id": "r1"}}),
                _sse({"type": "response.completed", "response": {"id": "r1"}}),
            ]
        )
    )
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_parent_spec_resolver,
        server_client=_CatchUpServer(),  # type: ignore[arg-type]
    )

    async with _runner_client(app):
        # Field state after a reconnect: the runner knows the session (it has
        # in-memory history) but never repopulated the sub-agent-name map for
        # it. Seed history so the scan iterates this session.
        _session_histories_ref[CHILD_SESSION_ID] = [
            {"role": "user", "content": [{"type": "input_text", "text": "prev"}]}
        ]
        try:
            await app.state.catch_up_scan()
            for _ in range(300):
                if pm.get_client_calls:
                    break
                await asyncio.sleep(0.01)
        finally:
            _session_histories_ref.pop(CHILD_SESSION_ID, None)

    sub_calls = [h for (conv, h, _env) in pm.get_client_calls if conv == CHILD_SESSION_ID]
    assert sub_calls, "catch-up scan did not dispatch a turn for the sub-agent session"
    assert all(h == "claude-native" for h in sub_calls), (
        f"reconnect catch-up scan asked the process manager to spawn {sub_calls!r} "
        "for the sub-agent session; expected only 'claude-native'. A 'claude-sdk' "
        "spawn is the production bug: the ingress WebSocket recycle triggers this "
        "scan, which respawns the harness and tears down the live claude-native "
        "terminal ('Bridge closed: terminal resource not found')."
    )


@pytest.mark.asyncio
async def test_resource_access_before_post_caches_child_not_parent_harness() -> None:
    """A resource request racing ahead of POST must not cache the PARENT harness.

    Root-enabler race: ``_session_spec_cache`` is the source of truth for a
    session's harness. Two paths populate it and race —

    * ``POST /v1/sessions`` caches the swapped CHILD spec (claude-native);
    * ``_resolve_session_spec_entry`` (hit by resource endpoints like
      ``GET /resources``, filesystem, terminal create) caches the spec the
      bound ``agent_id`` resolves to — the PARENT (claude-sdk).

    The web UI opens resource panels as it creates a sub-agent, so a resource
    GET frequently lands BEFORE the runner's POST. ``_resolve_session_spec_entry``
    early-returns once the key is cached, so whoever wins sticks. If the
    resource path wins on buggy code it caches the parent, ``_is_native_harness``
    goes False, and the next turn / reconnect flips the harness — tearing down
    the native terminal.

    This drives that exact ordering: a resource GET first (no prior POST), then
    a turn. The turn must spawn ``claude-native``. On buggy code the cached
    parent spec yields ``claude-sdk``.
    """
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "r1"}}),
            _sse({"type": "response.completed", "response": {"id": "r1"}}),
        ]
    )
    pm = _FakeProcessManager(hc)
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_parent_spec_resolver,
        server_client=_SubAgentSnapshotServer(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        # Resource access BEFORE any POST /v1/sessions — populates the spec
        # cache via _resolve_session_spec_entry (the racing path).
        rsrc = await client.get(f"/v1/sessions/{CHILD_SESSION_ID}/resources")
        assert rsrc.status_code == 200, f"{rsrc.status_code} {rsrc.text}"

        # Now a turn dispatches; it must resolve the CHILD harness despite the
        # resource path having cached first.
        resp = await client.post(
            f"/v1/sessions/{CHILD_SESSION_ID}/events",
            params={"stream": "true"},
            json={
                "type": "message",
                "role": "user",
                "agent_id": PARENT_AGENT_ID,
                "content": [{"type": "input_text", "text": "hi"}],
            },
        )
        assert resp.status_code == 200, f"{resp.status_code} {resp.text}"
        _ = resp.text

    harnesses = [h for (conv, h, _env) in pm.get_client_calls if conv == CHILD_SESSION_ID]
    assert harnesses, "the turn never asked the process manager for a harness"
    assert all(h == "claude-native" for h in harnesses), (
        f"after a resource GET raced ahead of POST, the turn spawned {harnesses!r} "
        "for the sub-agent session; expected only 'claude-native'. A 'claude-sdk' "
        "spawn means the resource path cached the parent spec and it stuck — the "
        "race that ultimately tears down the native terminal ('Bridge closed')."
    )
