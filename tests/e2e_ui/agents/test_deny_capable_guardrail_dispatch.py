"""An allowlisted dispatch must spawn a worker despite a deny-capable guardrail."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import (
    configure_mock_llm,
    open_right_rail,
    set_fallback_mock_llm,
)

_PARENT_TURN_DONE = "PARENT_DISPATCH_TURN_DONE"
_PARENT_YAML = """\
spec_version: 1
name: {parent_model}
prompt: Dispatch the worker once with sys_session_send, then finish.
executor:
  model: {parent_model}
  config:
    harness: openai-agents
tools:
  agents:
    - worker
guardrails:
  policies:
    allowlist_then_deny:
      type: function
      "on": [tool_call]
      function:
        path: omnigent.policies.builtins.cel.cel_policy
        arguments:
          expression: >
            event.type != "tool_call"
              ? {{"result": "ALLOW"}}
              : has(event.data.name)
                && type(event.data.name) == string
                && event.data.name.matches("^(ToolSearch|sys_session_send|sys_read_inbox)$")
                ? {{"result": "ALLOW"}}
                : {{"result": "DENY"}}
os_env:
  type: caller_process
  cwd: .
"""
_WORKER_YAML = """\
spec_version: 1
name: worker
prompt: Acknowledge the task and finish.
executor:
  model: {child_model}
  config:
    harness: openai-agents
os_env:
  type: caller_process
  cwd: .
"""


@pytest.mark.timeout(600)
def test_deny_capable_guardrail_allows_subagent_dispatch(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    runner_id: str,
    _recover_shared_runner: Callable[[], None],
) -> None:
    uid = uuid.uuid4().hex[:8]
    parent_model = f"denycap-parent-{uid}"
    child_model = f"denycap-child-{uid}"
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_dispatch_worker",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "worker",
                                "title": "deny-capable-dispatch",
                                "args": "Acknowledge the task and finish.",
                            }
                        ),
                    }
                ]
            },
            {"text": _PARENT_TURN_DONE},
        ],
        key=parent_model,
    )
    # The child's reply can wake the parent after its scripted turn ends.
    set_fallback_mock_llm(mock_llm_server_url, parent_model, "PARENT_WAKE_DONE")
    set_fallback_mock_llm(mock_llm_server_url, child_model, "WORKER_ACK_DONE")
    _recover_shared_runner()

    # config.yaml selects the strict parser that honors guardrails.
    bundle = bundle_files(
        {
            "config.yaml": _PARENT_YAML.format(parent_model=parent_model).encode(),
            "agents/worker/config.yaml": _WORKER_YAML.format(child_model=child_model).encode(),
        }
    )
    created = post_session_bundle(httpx.post, f"{live_server}/v1/sessions", bundle, timeout=30.0)
    created.raise_for_status()
    session_id = created.json()["session_id"]
    request.addfinalizer(
        lambda: httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
    )
    bind_session_runner(httpx.patch, live_server, session_id, runner_id, timeout=10.0)
    # Start recording after setup so the video opens on the session.
    page = request.getfixturevalue("page")
    assert isinstance(page, Page)
    page.goto(f"{live_server}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Please dispatch the worker sub-agent now, then finish.")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(
        page.locator(
            '[data-testid="message-bubble"][data-role="assistant"]',
            has_text=_PARENT_TURN_DONE,
        ).first
    ).to_be_visible(timeout=180_000)

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    # A denied synthetic start probe leaves no inbox, so dispatch creates no row.
    expect(rail.get_by_test_id("subagent-row").first).to_be_visible(timeout=60_000)
