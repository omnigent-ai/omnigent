"""Runner ``GET /v1/sessions/{id}`` must report an active native-pane turn.

A native-pane turn (a Claude/Codex session driven through its own terminal)
streams through that terminal, not the app-level ``_active_turns`` map or the
process manager's in-flight guard. The runner status read a server status probe
trusts therefore has to consult the native pane status too; otherwise it reports
a false ``idle`` for a session whose native turn is still running, and the server
would settle a genuinely busy row -- stopping the sidebar's working indicator
mid-turn. A non-native harness is unaffected: its forwarded status edge updates
the exit-classification memo but must not make the status read claim a turn.

Drives the runner app over real HTTP (ASGI): a clean init, then a forwarder-style
``external_session_status`` edge marks the turn running and later idle, asserting
the status read tracks it even while the app-level and process-manager turn
trackers stay empty.

Usage::

    pytest tests/runner/test_app_session_status_native_turn.py -v
"""

from __future__ import annotations

import pytest

from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _spec_resolver_returning,
)
from tests.runner.helpers import NullServerClient

_SESSION_ID = "nativeturn_7c2b1f904e8d4a6ab0f3d5c81e2a9f30"
_AGENT_ID = "agentnative_1a2b3c4d5e6f70819a2b3c4d5e6f7081"


async def _native_turn_app(harness: str) -> object:
    """Build a runner app whose session spec declares *harness*."""
    spec = AgentSpec(
        spec_version=1,
        name="native-turn-status",
        instructions="noop",
        skills=[],
        skills_filter="none",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
    )
    return create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=await _spec_resolver_returning(spec),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )


async def _post_status(client: object, status: str) -> None:
    """POST one forwarder-style ``external_session_status`` edge for the session."""
    resp = await client.post(  # type: ignore[attr-defined]
        f"/v1/sessions/{_SESSION_ID}/events",
        json={"type": "external_session_status", "data": {"status": status}},
    )
    assert resp.status_code == 204, resp.text


async def _get_status(client: object) -> str:
    """Return the runner's status read for the session."""
    resp = await client.get(f"/v1/sessions/{_SESSION_ID}")  # type: ignore[attr-defined]
    assert resp.status_code == 200, resp.text
    return resp.json()["status"]


@pytest.mark.asyncio
async def test_get_session_reports_active_native_turn() -> None:
    """The status read follows a native-pane turn no other tracker holds.

    With no app-level ``_active_turns`` entry and no process-manager in-flight
    turn, a native turn signalled by the terminal's forwarded ``running`` edge
    must still read ``running``; once the terminal forwards ``idle``, the read
    settles.
    """
    app = await _native_turn_app("claude-native")
    async with _runner_client(app) as client:
        init_resp = await client.post(
            "/v1/sessions",
            json={"session_id": _SESSION_ID, "agent_id": _AGENT_ID},
        )
        assert init_resp.status_code == 201, init_resp.text

        await _post_status(client, "running")
        assert _SESSION_ID not in app.state.active_turns
        assert await _get_status(client) == "running", (
            "the status read ignored the native-pane turn and reported a false idle"
        )

        await _post_status(client, "idle")
        assert await _get_status(client) == "idle"


@pytest.mark.asyncio
async def test_get_session_ignores_non_native_forwarded_status() -> None:
    """A non-native harness's forwarded ``running`` edge is not a tracked turn.

    The forwarded edge updates the exit-classification memo for any harness, so
    the status read must gate the native-turn branch on the harness actually
    being native. Otherwise a stuck ``running`` row that lost its ``idle`` edge
    could never be settled: the probe would echo back the memo as ``running``.
    """
    app = await _native_turn_app("openai-agents")
    async with _runner_client(app) as client:
        init_resp = await client.post(
            "/v1/sessions",
            json={"session_id": _SESSION_ID, "agent_id": _AGENT_ID},
        )
        assert init_resp.status_code == 201, init_resp.text

        await _post_status(client, "running")
        assert _SESSION_ID not in app.state.active_turns
        assert await _get_status(client) == "idle", (
            "a non-native forwarded status must not make the read claim a turn"
        )
