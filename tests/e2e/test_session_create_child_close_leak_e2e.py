"""E2E: an idle ``sys_session_create`` child stays closable and listable.

A ``sys_session_create`` child keeps its verbatim, possibly colonless
``title``, while several ``sys_session_*`` paths assumed the framework's
``"<agent>:<title>"`` naming:

* ``sys_session_close(<child>)`` must succeed for a genuine sub-agent
  (populated ``parent_session_id``) instead of refusing with
  ``session_not_a_sub_agent`` when the title-parse finds no agent.

* ``sys_session_list`` must surface the child (the ``sub_agents`` view
  skipped colonless titles and the global ``sessions`` view listed only
  top-level sessions), so an orchestrator that lost the create-time
  ``conversation_id`` can recover the handle.

A child that can be neither closed nor listed has no supported teardown,
so a native-harness child's ``omnigent-terminal-*`` tmux server + bridge
process would leak. This suite drives that environment-independent root
cause on the mock-LLM ``openai-agents`` harness, which allocates no
tmux/bridge of its own.

Topology mirrors tests/e2e/test_spawn_bounds_subagent_dispatch_e2e.py:
real server + real runner, mock LLM scripted per-agent.
"""

from __future__ import annotations

import io
import json
import tarfile
import uuid
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    OMNIGENT_INTERNAL_WS_ORIGIN,
    configure_mock_llm,
    create_runner_bound_session,
    lookup_agent_id,
    poll_session_until_terminal,
    reset_mock_llm,
    send_user_message_to_session,
    set_fallback_mock_llm,
)

pytestmark = [
    pytest.mark.min_server_version("0.3.0"),
    pytest.mark.timeout(600, method="signal"),
]

_CHILD_TITLE = "LeakRepro"
_PARENT_TURN_DONE = "PARENT_TURN_DONE"

# spawn: true registers sys_session_create + sys_session_close; the child
# is a trivial leaf agent that never runs (spawned idle, no message).
_PARENT_YAML = """\
spec_version: 1
name: {name}
prompt: |
  You are an orchestrator.
spawn: true
executor:
  type: omnigent
  model: {model}
  auth:
    type: api_key
    api_key: mock-key
    base_url: {base_url}
  config:
    harness: openai-agents
os_env:
  type: caller_process
  cwd: .
"""

_CHILD_YAML = """\
spec_version: 1
name: {name}
prompt: |
  You are an idle worker.
executor:
  type: omnigent
  model: {model}
  auth:
    type: api_key
    api_key: mock-key
    base_url: {base_url}
  config:
    harness: openai-agents
os_env:
  type: caller_process
  cwd: .
"""


def _register_agent(client: httpx.Client, *, name: str, yaml_text: str) -> str:
    """Upload a single-agent bundle and return its registered name."""
    data = yaml_text.encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="config.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    resp = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    if resp.status_code not in (200, 201, 409):
        raise RuntimeError(f"bundle register failed: {resp.status_code} {resp.text[:500]}")
    return name


def _tool_output(items: list[dict[str, Any]], tool_name: str) -> str:
    """Return the first ``function_call_output`` for *tool_name* in *items*."""
    flat: list[dict[str, Any]] = []
    for item in items:
        data = item.get("data") or {}
        flat.append({"type": item.get("type"), **data})
    call_ids = {
        p.get("call_id")
        for p in flat
        if p.get("type") == "function_call" and p.get("name") == tool_name
    }
    for p in flat:
        if p.get("type") == "function_call_output" and p.get("call_id") in call_ids:
            return str(p.get("output", ""))
    raise AssertionError(
        f"no {tool_name} output found in session items; got types "
        f"{[p.get('type') for p in flat]!r}"
    )


def _spawn_idle_child(
    http_client: httpx.Client,
    *,
    live_runner_id: str,
    mock_llm_server_url: str,
    parent_name: str,
    parent_model: str,
    child_agent_id: str,
) -> tuple[str, str]:
    """Drive the parent through one ``sys_session_create`` turn.

    :returns: ``(parent_session_id, child_conversation_id)``.
    """
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_create_child",
                        "name": "sys_session_create",
                        "arguments": json.dumps(
                            {"agent_id": child_agent_id, "title": _CHILD_TITLE}
                        ),
                    }
                ]
            },
            {"text": _PARENT_TURN_DONE},
        ],
        key=parent_model,
    )
    set_fallback_mock_llm(mock_llm_server_url, parent_model, _PARENT_TURN_DONE)

    parent_session_id = create_runner_bound_session(
        http_client, agent_name=parent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=parent_session_id,
        content="Create one idle child sub-agent.",
    )
    result = poll_session_until_terminal(
        http_client, session_id=parent_session_id, response_id=response_id, timeout=240
    )
    assert result["status"] == "completed", (
        f"parent create turn did not complete: {result.get('error')!r}"
    )

    snap = http_client.get(f"/v1/sessions/{parent_session_id}")
    snap.raise_for_status()
    create_out = _tool_output(snap.json().get("items", []), "sys_session_create")
    payload = json.loads(create_out)
    child_id = payload.get("conversation_id")
    assert isinstance(child_id, str) and child_id, (
        f"sys_session_create did not return a child conversation_id: {create_out!r}"
    )

    # Precondition: the child IS a genuine sub-agent -- it has a parent --
    # yet its title is the verbatim create-time label with no
    # "<agent>:<title>" colon, which is what the buggy guards keyed off.
    child_snap = http_client.get(f"/v1/sessions/{child_id}")
    child_snap.raise_for_status()
    child_body = child_snap.json()
    assert child_body.get("parent_session_id") == parent_session_id, (
        f"child {child_id} is not parented to {parent_session_id}: "
        f"parent_session_id={child_body.get('parent_session_id')!r}"
    )
    assert child_body.get("title") == _CHILD_TITLE and ":" not in _CHILD_TITLE

    return parent_session_id, child_id


@pytest.fixture(scope="module")
def leak_repro_agents(
    http_client: httpx.Client, mock_llm_server_url: str | None
) -> dict[str, str]:
    """Register the spawn:true parent + idle leaf child; return their handles."""
    assert mock_llm_server_url is not None
    uid = uuid.uuid4().hex[:6]
    base_url = f"{mock_llm_server_url}/v1"
    parent_name = f"leak-parent-{uid}"
    parent_model = f"mock-leak-parent-{uid}"
    child_name = f"leak-child-{uid}"
    child_model = f"mock-leak-child-{uid}"

    _register_agent(
        http_client,
        name=parent_name,
        yaml_text=_PARENT_YAML.format(name=parent_name, model=parent_model, base_url=base_url),
    )
    _register_agent(
        http_client,
        name=child_name,
        yaml_text=_CHILD_YAML.format(name=child_name, model=child_model, base_url=base_url),
    )
    # Idle child never runs, but keep its queue answerable defensively.
    set_fallback_mock_llm(mock_llm_server_url, child_model, "IDLE")
    child_agent_id = lookup_agent_id(http_client, child_name)
    return {
        "parent_name": parent_name,
        "parent_model": parent_model,
        "child_agent_id": child_agent_id,
    }


def test_sys_session_close_accepts_sys_session_create_child(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
    leak_repro_agents: dict[str, str],
) -> None:
    """Closing a genuine ``sys_session_create`` child must succeed.

    Guards against ``sys_session_close`` refusing the child with
    ``session_not_a_sub_agent`` because its verbatim, colonless title
    fails the ``"<agent>:<title>"`` parse.
    """
    assert mock_llm_server_url is not None
    parent_model = leak_repro_agents["parent_model"]
    reset_mock_llm(mock_llm_server_url)
    parent_session_id, child_id = _spawn_idle_child(
        http_client,
        live_runner_id=live_runner_id,
        mock_llm_server_url=mock_llm_server_url,
        parent_name=leak_repro_agents["parent_name"],
        parent_model=parent_model,
        child_agent_id=leak_repro_agents["child_agent_id"],
    )

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_close_child",
                        "name": "sys_session_close",
                        "arguments": json.dumps({"conversation_id": child_id}),
                    }
                ]
            },
            {"text": _PARENT_TURN_DONE},
        ],
        key=parent_model,
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=parent_session_id,
        content="Close the idle child sub-agent now.",
    )
    result = poll_session_until_terminal(
        http_client, session_id=parent_session_id, response_id=response_id, timeout=240
    )
    assert result["status"] == "completed", (
        f"parent close turn did not complete: {result.get('error')!r}"
    )

    snap = http_client.get(f"/v1/sessions/{parent_session_id}")
    snap.raise_for_status()
    close_out = _tool_output(snap.json().get("items", []), "sys_session_close")
    close_payload = json.loads(close_out)

    assert close_payload.get("error") != "session_not_a_sub_agent", (
        f"sys_session_close refused a genuine sub-agent (has parent "
        f"{parent_session_id}) with session_not_a_sub_agent -- the child "
        f"created via sys_session_create has no supported teardown: {close_out!r}"
    )
    assert close_payload.get("closed") is True, (
        f"sys_session_close should tombstone the child; got: {close_out!r}"
    )


def test_sys_session_create_child_is_listable(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
    leak_repro_agents: dict[str, str],
) -> None:
    """The child must be reachable through ``sys_session_list``.

    Guards against the child being absent from BOTH the ``sub_agents``
    view (colonless title skipped) and the global ``sessions`` view
    (only top-level sessions listed).
    """
    assert mock_llm_server_url is not None
    parent_model = leak_repro_agents["parent_model"]
    reset_mock_llm(mock_llm_server_url)
    parent_session_id, child_id = _spawn_idle_child(
        http_client,
        live_runner_id=live_runner_id,
        mock_llm_server_url=mock_llm_server_url,
        parent_name=leak_repro_agents["parent_name"],
        parent_model=parent_model,
        child_agent_id=leak_repro_agents["child_agent_id"],
    )

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_list_sessions",
                        "name": "sys_session_list",
                        "arguments": json.dumps({}),
                    }
                ]
            },
            {"text": _PARENT_TURN_DONE},
        ],
        key=parent_model,
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=parent_session_id,
        content="List your sub-agent sessions.",
    )
    result = poll_session_until_terminal(
        http_client, session_id=parent_session_id, response_id=response_id, timeout=240
    )
    assert result["status"] == "completed", (
        f"parent list turn did not complete: {result.get('error')!r}"
    )

    snap = http_client.get(f"/v1/sessions/{parent_session_id}")
    snap.raise_for_status()
    list_out = _tool_output(snap.json().get("items", []), "sys_session_list")

    assert child_id in list_out, (
        f"the idle sys_session_create child {child_id} (parented to "
        f"{parent_session_id}) is absent from sys_session_list -- neither the "
        f"sub_agents view nor the global sessions list surfaces it, so an "
        f"orchestrator without the create-time id loses the handle: {list_out!r}"
    )
