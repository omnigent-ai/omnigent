"""UI journey: a spawn_bounds-guarded orchestrator must not fan out past its cap.

The session's agent bundle declares a function-type ``spawn_bounds``
guardrail with ``max_dispatches_per_turn: 1``. The user asks the
orchestrator to run three tasks; its scripted turn attempts THREE
``sys_session_send`` worker dispatches in that single turn. Dispatches
beyond the cap must be denied, so after the turn settles the Agents rail
may list at most ONE worker sub-agent row.

The defect this guards against: ``spawn_bounds`` counts dispatches in
in-memory per-turn state, but every enforcement point that saw a
``sys_session_send`` rebuilt the policy per call, so the counter never
passed 1 and the cap was decorative. On the buggy build all three
dispatches succeed and the rail shows three worker rows — unbounded
fan-out with the guardrail configured — and this test FAILS.

Registration and mock scripting mirror ``approval_session`` in the parent
conftest (strict ``config.yaml`` bundle parser — the one that honors
``guardrails``) plus the two-file parent+child bundle shape of
``tests/e2e/test_spawn_bounds_subagent_dispatch_e2e.py``.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import tarfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    open_right_rail,
    set_fallback_mock_llm,
)

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_SUBAGENT_ROW = '[data-testid="subagent-row"]'

# Number of workers the scripted parent tries to dispatch in ONE turn,
# and the per-turn cap its spawn_bounds guardrail allows.
_DISPATCH_ATTEMPTS = 3
_DISPATCH_CAP = 1

# Sentinel ending the parent's dispatch turn so the test can wait on it.
_PARENT_TURN_DONE = "PARENT_DISPATCH_TURN_DONE"

# The dispatch turn runs three spawn rounds plus child turns and parent
# auto-wakes over the mock LLM, so give the bubble a generous budget.
_TURN_TIMEOUT_MS = 240_000

_PARENT_YAML = """\
spec_version: 1
name: {name}
prompt: |
  You are an orchestrator. When asked to run, dispatch the worker
  sub-agent with sys_session_send once per requested task.

executor:
  model: {parent_model}
  config:
    harness: openai-agents

tools:
  agents:
    - worker

guardrails:
  policies:
    spawn_bounds:
      type: function
      function:
        path: omnigent.policies.builtins.orchestration.spawn_bounds
        arguments:
          max_dispatches_per_turn: {cap}

os_env:
  type: caller_process
  cwd: .
"""

_WORKER_YAML = """\
spec_version: 1
name: worker
prompt: |
  You are a worker. Acknowledge the task you were given and finish.

executor:
  model: {child_model}
  config:
    harness: openai-agents

os_env:
  type: caller_process
  cwd: .
"""


@dataclass(frozen=True)
class SpawnBoundsSession:
    """Handle for the spawn_bounds-guarded orchestrator session fixture.

    :param base_url: Spawned server base URL, e.g. ``"http://127.0.0.1:51234"``.
    :param session_id: The runner-bound parent session id.
    """

    base_url: str
    session_id: str


@pytest.fixture
def spawn_bounds_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[SpawnBoundsSession]:
    """Create a runner-bound session for the spawn_bounds orchestrator.

    Scripts the parent queue to attempt three ``sys_session_send``
    dispatches in one turn, then end the turn; worker children draw a
    canned acknowledgement. Unique per-run model keys isolate the queues.

    :param live_server: Spawned server fixture from the parent conftest.
    :param mock_llm_server_url: Mock LLM server used by credential-free runs.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: A :class:`SpawnBoundsSession` handle.
    """
    uid = uuid.uuid4().hex[:8]
    agent_name = f"spawn_bounds_probe_{uid}"
    parent_model = f"spawnbounds-parent-{uid}"
    child_model = f"spawnbounds-child-{uid}"

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call_dispatch_worker_{i}",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "worker",
                                "title": f"fanout-{i}",
                                "args": f"Acknowledge task {i} and finish.",
                            }
                        ),
                    },
                ],
            }
            for i in range(1, _DISPATCH_ATTEMPTS + 1)
        ]
        + [{"text": _PARENT_TURN_DONE}],
        key=parent_model,
    )
    # Absorb the parent's inbox auto-wake continuations (one per child that
    # actually ran) so queue exhaustion can't fail a wake turn.
    set_fallback_mock_llm(mock_llm_server_url, parent_model, "PARENT_WAKE_DONE")
    set_fallback_mock_llm(mock_llm_server_url, child_model, "WORKER_SPAWN_TEST_DONE")

    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])

    parent_yaml = _PARENT_YAML.format(
        name=agent_name, parent_model=parent_model, cap=_DISPATCH_CAP
    ).encode()
    worker_yaml = _WORKER_YAML.format(child_model=child_model).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # Strict path: arcname config.yaml keeps it on the spec_version:1
        # parser, which is the one that honors `guardrails`.
        for arcname, data in (
            ("config.yaml", parent_yaml),
            ("agents/worker/config.yaml", worker_yaml),
        ):
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    patch_resp = httpx.patch(
        f"{live_server}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()

    try:
        yield SpawnBoundsSession(base_url=live_server, session_id=session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)


@pytest.mark.timeout(600)
def test_spawn_bounds_caps_worker_fanout_in_agents_rail(
    page: Page,
    spawn_bounds_session: SpawnBoundsSession,
) -> None:
    """Three attempted dispatches under a cap of 1 leave at most one worker row."""
    chat = spawn_bounds_session
    page.goto(f"{chat.base_url}/c/{chat.session_id}")

    # Ask the orchestrator to fan out; its scripted turn attempts three
    # worker dispatches in this single turn.
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("RUN: dispatch the worker sub-agent for three tasks.")
    page.get_by_role("button", name="Send", exact=True).click()

    # The dispatch turn finished (scripted terminal sentinel rendered).
    expect(page.locator(_ASSISTANT, has_text=_PARENT_TURN_DONE).first).to_be_visible(
        timeout=_TURN_TIMEOUT_MS
    )

    # The Agents rail lists the workers that actually spawned. Lookups are
    # scoped to the desktop "Workspace" rail so they don't match the hidden
    # mobile drawer that mirrors the same testids.
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    agents_tab = rail.get_by_role("tab", name=re.compile("^Agents"))
    agents_tab.click()
    rows = rail.locator(_SUBAGENT_ROW)

    # The allowed dispatch materializes as a worker row.
    expect(rows.first).to_be_visible(timeout=60_000)
    # Denied dispatches must not have spawned children: give any stragglers
    # a moment to render, then count.
    page.wait_for_timeout(5_000)
    row_count = rows.count()
    assert row_count <= _DISPATCH_CAP, (
        f"{row_count} worker sub-agents spawned in one turn but the bundle's "
        f"spawn_bounds guardrail caps dispatches at {_DISPATCH_CAP} — the "
        f"guardrail was not enforced on sys_session_send"
    )
