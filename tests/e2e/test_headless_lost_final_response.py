"""Headless async agents can lose a saved final response after a missed
completion event.

``omnigent run <agent> -p "<prompt>"`` drives ``_query_sessions_once``. For an
async orchestrator (dispatch a sub-agent, park, get auto-woken to synthesize)
the sequence is:

1. Turn 1 dispatches a sub-agent and completes with no assistant text; the
   session parks (``waiting``) while the worker runs.
2. The worker finishes, the parent is auto-woken, and a later turn produces and
   persists the completed synthesis.
3. The drain loop's live subscription for that later turn misses its terminal
   completion event (the reported subscribe-after-post race) and collects no
   text.
4. With no collected text, ``_query_sessions_once`` checks only for a persisted
   *error* -- never reconciling the persisted *text* -- and returns ``None``.
   Headless ``-p`` then prints nothing and exits 0, silently dropping the
   completed final response.

This test drives the real ``_query_sessions_once`` CLI helper against a live
server + runner running a real sub-agent orchestrator, so the dispatch, the
auto-wake, and the persisted synthesis are all genuine. Only the reported
trigger is injected: the drain-loop subscription is made to miss the final
completion event (``await_turn`` returns empty, as it does when a subscription
sees only the idle status). The completed synthesis remains durably persisted
server-side, so a correct helper would reconcile and return it.

Run locally (mock LLM, no real API key needed)::

    pytest tests/e2e/test_headless_lost_final_response.py -v
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
import uuid
from typing import Any

import httpx
import pytest
import yaml

from tests.e2e.conftest import (
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)

# Per-agent mock-LLM routing (parent and worker each on their own model queue)
# requires a server >= 0.3.0, matching tests/e2e/test_spawn_bounds_subagent_dispatch_e2e.py.
pytestmark = [
    pytest.mark.min_server_version("0.3.0"),
    pytest.mark.timeout(300, method="signal"),
]


def _build_orchestrator_bundle(
    *,
    name: str,
    parent_model: str,
    child_model: str,
    mock_llm_base_url: str,
) -> bytes:
    """Package a parent orchestrator + a trivial worker sub-agent as a bundle.

    The parent dispatches ``worker`` via ``sys_session_send`` (async), so it
    parks to ``waiting`` and is auto-woken when the worker completes -- the
    async-orchestrator shape ``_query_sessions_once``'s drain loop exists for.
    """
    auth = {"type": "api_key", "api_key": "mock-key", "base_url": mock_llm_base_url}
    parent_cfg: dict[str, Any] = {
        "spec_version": 1,
        "name": name,
        "executor": {
            "type": "omnigent",
            "model": parent_model,
            "config": {"harness": "openai-agents"},
            "auth": auth,
        },
        "prompt": (
            "You are an orchestrator. Dispatch the worker sub-agent for the "
            "requested task, then report its result."
        ),
        "tools": {"agents": ["worker"]},
        "os_env": {"type": "caller_process", "cwd": "."},
    }
    child_cfg: dict[str, Any] = {
        "spec_version": 1,
        "name": "worker",
        "executor": {
            "type": "omnigent",
            "model": child_model,
            "config": {"harness": "openai-agents"},
            "auth": auth,
        },
        "prompt": "You are a worker. Acknowledge the task you were given and finish.",
        "os_env": {"type": "caller_process", "cwd": "."},
    }
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:

            def _add(arcname: str, cfg: dict[str, Any]) -> None:
                data = yaml.dump(cfg).encode()
                info = tarfile.TarInfo(arcname)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

            _add("config.yaml", parent_cfg)
            _add("agents/worker/config.yaml", child_cfg)
        return buf.getvalue()


@pytest.mark.parametrize("worker_delay", [0.0, 12.0], ids=["immediate-worker", "slow-worker"])
def test_headless_prompt_reconciles_saved_final_response_after_missed_completion(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
    monkeypatch: pytest.MonkeyPatch,
    worker_delay: float,
) -> None:
    """Headless ``-p`` must not drop a saved final response on a missed event.

    Fails on the buggy build: ``_query_sessions_once`` returns ``None`` even
    though the completed synthesis is persisted server-side.
    """
    from omnigent_client import OmnigentClient, QueryResult
    from omnigent_client._sessions_chat import SessionsChat

    from omnigent.chat import _persisted_turn_text, _query_sessions_once

    assert mock_llm_server_url is not None, "reproduction requires the mock LLM server"

    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-lost-final-parent-{uid}"
    child_model = f"mock-lost-final-child-{uid}"
    marker = f"SYNTH_FINAL_{uid}"

    reset_mock_llm(mock_llm_server_url)
    # Hold synthesis until the drain loop starts, so the initial transcript
    # check cannot recover it before the missed-completion path is exercised.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_dispatch_worker",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {"agent": "worker", "title": "job", "args": "Acknowledge and finish."}
                        ),
                    }
                ]
            },
            {"text": ""},
            {"text": marker, "block": True},
        ],
        key=parent_model,
    )
    set_fallback_mock_llm(mock_llm_server_url, parent_model, marker)
    configure_mock_llm(
        mock_llm_server_url, [{"text": "WORKER_DONE", "delay": worker_delay}], key=child_model
    )
    set_fallback_mock_llm(mock_llm_server_url, child_model, "WORKER_DONE")

    bundle = _build_orchestrator_bundle(
        name=f"lost-final-{uid}",
        parent_model=parent_model,
        child_model=child_model,
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )

    gate_released = False

    # Miss the completion event while still waiting for the real session to
    # finish. Returning every poll would exhaust the CLI's 30-turn guard.
    async def _await_turn_missing_completion(
        self: SessionsChat, *, timeout: float | None = 1200.0
    ) -> QueryResult:
        nonlocal gate_released
        try:
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(base_url=mock_llm_server_url) as gate_client:
                    while True:
                        if not gate_released:
                            pending = await gate_client.get("/gate/pending")
                            pending.raise_for_status()
                            if pending.json()["pending"]:
                                released = await gate_client.post("/gate/release")
                                released.raise_for_status()
                                assert released.json()["released"]
                                gate_released = True
                        await self.refresh()
                        assert self.status != "failed", "orchestrator failed before synthesis"
                        if gate_released and self.status == "idle":
                            break
                        await asyncio.sleep(0.2)
        except TimeoutError:
            pass  # Match await_turn's empty result when its wait budget expires.
        return QueryResult(text="", files=[])

    monkeypatch.setattr(SessionsChat, "await_turn", _await_turn_missing_completion)

    captured: dict[str, str] = {}

    async def _one_shot() -> str | None:
        async with OmnigentClient(base_url=str(http_client.base_url)) as client:
            return await asyncio.wait_for(
                _query_sessions_once(
                    client=client,
                    agent_name=f"lost-final-{uid}",
                    tool_handler=None,
                    prompt="Dispatch the worker for one job, then report its result.",
                    session_bundle=bundle,
                    session_bundle_filename="agent.tar.gz",
                    runner_id=live_runner_id,
                    on_session_ready=lambda sid: captured.__setitem__("id", sid),
                ),
                timeout=180,
            )

    result = asyncio.run(_one_shot())
    assert gate_released, "the drain loop never reached the gated synthesis"

    session_id = captured.get("id")
    assert session_id, "headless run never reported a session id"

    async def _persisted() -> str | None:
        async with OmnigentClient(base_url=str(http_client.base_url)) as client:
            return await _persisted_turn_text(client, session_id)

    persisted = asyncio.run(_persisted())
    assert persisted is not None and marker in persisted, (
        f"precondition: the completed synthesis {marker!r} must be persisted "
        f"server-side; got {persisted!r}"
    )

    assert result is not None and marker in result, (
        "headless -p dropped the saved final response: _query_sessions_once "
        f"returned {result!r} even though the completed synthesis {marker!r} "
        f"was persisted server-side (session {session_id})"
    )
