"""Regression: a session reset must fence a terminal that is still starting.

``POST /v1/sessions/{id}/reset-state`` closes the terminals the registry holds at
that moment, but a creator already past its spec resolution keeps going -- the
registry takes the slot only once the start completes. A terminal built from the
spec the reset retired could therefore register itself after the reset finished
and be handed straight back to the caller.

The fence makes the in-flight launch notice the reset, drop what it built, and
report a conflict instead of attaching a terminal from the previous agent.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.entities.session_resources import SessionResourceView, terminal_resource_id
from omnigent.runner import create_runner_app
from omnigent.runner.native import orchestration
from omnigent.spec.types import AgentSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient

_CONV = "7c41f0a2b9de4c8fa1e05b6d3c2f8a91"
_TERMINAL = "goose"


@pytest.mark.asyncio
async def test_reset_during_terminal_launch_refuses_the_stale_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal that only finishes starting after a reset must not be attached."""
    entered_adapter = asyncio.Event()
    release_adapter = asyncio.Event()

    async def _adapter(ctx: Any) -> SessionResourceView:
        """Stand in for the harness auto-create hook, pausing mid-start."""
        entered_adapter.set()
        await release_adapter.wait()
        return SessionResourceView(
            id=terminal_resource_id(_TERMINAL, "main"),
            type="terminal",
            session_id=ctx.session_id,
            name=_TERMINAL,
        )

    real_resolve_hook = orchestration.resolve_hook

    def _resolve_hook(provider: Any, name: str) -> Any:
        """Swap in the pausing adapter, leaving every other hook alone."""
        if name == "auto_create_terminal":
            return _adapter
        return real_resolve_hook(provider, name)

    monkeypatch.setattr(orchestration, "resolve_hook", _resolve_hook)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="t")

    harness_client = _ScriptedHarnessClient(
        [_sse({"type": "response.created", "response": {"id": "resp_1"}})]
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(harness_client),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        ensure = asyncio.create_task(
            client.post(
                f"/v1/sessions/{_CONV}/resources/terminals",
                json={
                    "terminal": _TERMINAL,
                    "session_key": "main",
                    "ensure_native_terminal": True,
                },
            )
        )
        await asyncio.wait_for(entered_adapter.wait(), timeout=5)

        reset = await client.post(f"/v1/sessions/{_CONV}/reset-state")
        assert reset.status_code == 200

        # The launch completes only now, after the reset has already torn down
        # everything it could see.
        release_adapter.set()
        response = await asyncio.wait_for(ensure, timeout=5)

    assert response.status_code == 409, (
        "the terminal that finished starting after the reset was attached anyway "
        f"(status {response.status_code})"
    )
    assert response.json()["error"]["code"] == "session_reset_during_launch"
