"""
Integration test: ``POST /policies/evaluate`` keeps synchronous store work off the event loop.

Native harnesses call this endpoint once per tool call. The agent lookup, bundle load and
policy-engine build all make synchronous store reads, so running them on the event loop
stalls every other request the server is handling.

Uses the shared ``auth_client`` / ``db_uri`` fixtures from ``tests/server/conftest.py``.
"""

from __future__ import annotations

import threading
from typing import Any

import httpx
import pytest

from omnigent.runtime import get_caps
from omnigent.runtime.caps import RuntimeCaps
from omnigent.server.routes.sessions import routes_hooks
from omnigent.spec.types import FunctionPolicySpec, FunctionRef
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

OWNER = "owner@example.com"

_ASK_THEN_ALLOW_CALLS: list[None] = []


def _ask_then_allow(event: dict[str, Any]) -> dict[str, Any]:
    """
    Policy that ASKs on its first evaluation and ALLOWs afterwards.

    The first ASK makes the endpoint rebuild the engine inside the approval-gate lock;
    the rebuilt engine ALLOWs, so both build call sites run without a human.

    :param event: V0 event dict.
    :returns: ASK the first time, then ALLOW.
    """
    _ASK_THEN_ALLOW_CALLS.append(None)
    if len(_ASK_THEN_ALLOW_CALLS) == 1:
        return {"result": "ASK", "reason": "Approval required"}
    return {"result": "ALLOW"}


async def test_policy_engine_builds_run_off_the_event_loop(
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both engine builds (first and approval-gate rebuild) run on worker threads."""
    _ASK_THEN_ALLOW_CALLS.clear()
    policy = FunctionPolicySpec(
        name="admin__ask_then_allow",
        on=None,
        function=FunctionRef(path=f"{__name__}._ask_then_allow"),
    )
    original_caps = get_caps()
    patched_caps = RuntimeCaps(
        execution_timeout=original_caps.execution_timeout,
        default_policies=[policy],
    )
    monkeypatch.setattr("omnigent.server.routes.sessions.get_caps", lambda: patched_caps)

    agent = await create_test_agent(auth_client, user=OWNER)
    resp = await auth_client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"]},
        headers={"X-Forwarded-Email": OWNER},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]

    build_threads: list[threading.Thread] = []
    real_build = routes_hooks.build_policy_engine

    def recording_build(*args: Any, **kwargs: Any) -> Any:
        build_threads.append(threading.current_thread())
        return real_build(*args, **kwargs)

    monkeypatch.setattr(routes_hooks, "build_policy_engine", recording_build)

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json={
            "event": {
                "type": "PHASE_TOOL_CALL",
                "target": "",
                "data": {"name": "Bash", "arguments": {}},
                "context": {},
            },
        },
        headers={"X-Forwarded-Email": OWNER},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == "POLICY_ACTION_ALLOW"
    assert len(build_threads) == 2
    # The ASGI app runs on this test's event loop, i.e. this thread.
    assert threading.current_thread() not in build_threads
