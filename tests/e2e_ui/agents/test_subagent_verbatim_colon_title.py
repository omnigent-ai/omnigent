"""UI journey: a ``sys_session_create`` child keeps its verbatim colon title.

The orchestrator's scripted turn creates a child titled ``"research:pricing"``
from a local ``config_path`` and then calls ``sys_session_list``; the parent's
Agents rail and the list result must show the full title under the child's
real agent, not ``research`` / ``pricing``. Fixture details, including why the
parent pins an absolute ``os_env.cwd``, live on ``verbatim_title_session``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    _create_bundled_session,
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    open_right_rail,
    set_fallback_mock_llm,
)

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_SUBAGENT_ROW = '[data-testid="subagent-row"]'

_CHILD_AGENT_NAME = "pricing_probe_child"
_CHILD_CONFIG_DIR = "pricing_probe_child"
_VERBATIM_TITLE = "research:pricing"
_TURN_DONE = "VERBATIM_TITLE_LIST_TURN_DONE"
_TURN_TIMEOUT_MS = 240_000

_PARENT_YAML = """\
spec_version: 1
name: {name}
prompt: |
  You are an orchestrator. When asked, create a research child session with
  sys_session_create and then list your sessions with sys_session_list.

executor:
  model: {parent_model}
  config:
    harness: openai-agents

spawn: true

os_env:
  type: caller_process
  cwd: {cwd}
"""

_CHILD_YAML = """\
spec_version: 1
name: {name}
prompt: |
  You are a research worker. Acknowledge the task you were given and finish.

executor:
  model: {child_model}
  config:
    harness: openai-agents

os_env:
  type: caller_process
  cwd: .
"""


@pytest.fixture
def browser_context_args(browser_context_args: dict) -> dict:
    """Record ``--video on`` at the viewport size so the rail label stays legible."""
    return {**browser_context_args, "record_video_size": {"width": 1280, "height": 720}}


@dataclass(frozen=True)
class VerbatimTitleSession:
    """Handle for the spawning orchestrator session.

    :param base_url: Server base URL, e.g. ``"http://127.0.0.1:51234"``.
    :param session_id: The runner-bound parent session id.
    """

    base_url: str
    session_id: str


@pytest.fixture
def verbatim_title_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[VerbatimTitleSession]:
    """Create a ``spawn: true`` parent whose turn creates a ``research:pricing`` child.

    The child agent config lives under a per-run directory inside the repo
    (the runner may not share this process's ``/tmp``), and that directory is
    the parent's absolute ``os_env.cwd`` so ``config_path`` resolves against it;
    a relative cwd would be replaced by a per-conversation tmpdir because
    neither the e2e runner nor the prepared repro runner sets
    ``OMNIGENT_RUNNER_WORKSPACE``.

    :param live_server: Server fixture from the parent conftest.
    :param mock_llm_server_url: Mock LLM server used by credential-free runs.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: A :class:`VerbatimTitleSession` handle.
    """
    uid = uuid.uuid4().hex[:8]
    parent_model = f"verbatim-title-parent-{uid}"
    child_model = f"verbatim-title-child-{uid}"
    work_dir = _REPO_ROOT / ".omnigent" / "e2e-verbatim-title" / uid
    (work_dir / _CHILD_CONFIG_DIR).mkdir(parents=True)
    session_id: str | None = None
    respawned_runner: subprocess.Popen[bytes] | None = None
    try:
        (work_dir / _CHILD_CONFIG_DIR / "config.yaml").write_text(
            _CHILD_YAML.format(name=_CHILD_AGENT_NAME, child_model=child_model)
        )
        configure_mock_llm(
            mock_llm_server_url,
            [
                {
                    "tool_calls": [
                        {
                            "call_id": "call_create_research_child",
                            "name": "sys_session_create",
                            "arguments": json.dumps(
                                {
                                    "config_path": _CHILD_CONFIG_DIR,
                                    "title": _VERBATIM_TITLE,
                                    "message": "Research the pricing page and summarize it.",
                                }
                            ),
                        }
                    ]
                },
                {
                    "tool_calls": [
                        {
                            "call_id": "call_list_children",
                            "name": "sys_session_list",
                            "arguments": "{}",
                        }
                    ]
                },
                {"text": _TURN_DONE},
            ],
            key=parent_model,
            required_tools=["sys_session_create"],
        )
        set_fallback_mock_llm(mock_llm_server_url, parent_model, "PARENT_WAKE_DONE")
        set_fallback_mock_llm(mock_llm_server_url, child_model, "CHILD_RESEARCH_DONE")

        respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
        runner_id = str(_server_state["runner_id"])
        session_id = _create_bundled_session(
            live_server,
            runner_id,
            _PARENT_YAML.format(
                name=f"verbatim_title_probe_{uid}", parent_model=parent_model, cwd=work_dir
            ),
        )
        yield VerbatimTitleSession(base_url=live_server, session_id=session_id)
    finally:
        if session_id is not None:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        shutil.rmtree(work_dir, ignore_errors=True)
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)


def _session_list_output(base_url: str, session_id: str) -> dict:
    """Return the parsed ``sys_session_list`` tool result from the parent transcript."""
    items = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items", params={"limit": 100}, timeout=30.0
    ).json()["data"]
    outputs = [
        item
        for item in items
        if item.get("type") == "function_call_output"
        and item.get("call_id") == "call_list_children"
    ]
    assert outputs, "sys_session_list tool result missing from the parent transcript"
    return json.loads(outputs[-1]["output"])


@pytest.mark.timeout(600)
def test_sys_session_create_child_keeps_verbatim_colon_title(
    page: Page,
    verbatim_title_session: VerbatimTitleSession,
) -> None:
    """The child is listed as its real agent with the full ``research:pricing`` title."""
    chat = verbatim_title_session
    page.goto(f"{chat.base_url}/c/{chat.session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(
        "Create a research child session titled research:pricing, then list your sessions."
    )
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT, has_text=_TURN_DONE).first).to_be_visible(
        timeout=_TURN_TIMEOUT_MS
    )

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    rows = rail.locator(_SUBAGENT_ROW)
    expect(rows.first).to_contain_text(_VERBATIM_TITLE, timeout=60_000)
    rail_label = rows.first.inner_text().splitlines()[0]

    listed = _session_list_output(chat.base_url, chat.session_id)["sub_agents"]
    assert len(listed) == 1, f"expected exactly one listed child, got {listed!r}"
    entry = listed[0]

    assert rail_label == _VERBATIM_TITLE, (
        f"Agents rail labels the child {rail_label!r}; expected the verbatim title "
        f"{_VERBATIM_TITLE!r}"
    )
    assert (entry["agent"], entry["title"]) == (_CHILD_AGENT_NAME, _VERBATIM_TITLE), (
        f"sys_session_list reported agent={entry['agent']!r} title={entry['title']!r}; "
        f"expected the bound agent {_CHILD_AGENT_NAME!r} with title {_VERBATIM_TITLE!r}"
    )
