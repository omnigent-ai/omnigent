"""E2E: a deny-capable guardrails policy must not break sub-agent dispatch.

A parent agent that declares a ``guardrails`` CEL policy **whose expression is
capable of returning DENY** used to lose sub-agent dispatch: the parent's
``sys_session_send`` tool call failed with

    Error: sys_session_send requires parent session inbox

even though the policy ALLOWS that exact call (the ``sys_session_send`` tool
name is on the policy's allowlist, so the CEL verdict for the dispatch event is
ALLOW). What mattered was only whether the expression's *terminal branch* could
return DENY at all: the runner probes tool_call policies with a synthetic
``sys_agent_start`` call at session init, the allowlist policy denied that
unknown name, session init aborted, and the parent session ran without the
inbox that sub-agent dispatch requires.

Reproduction shape (mirrors the reporter's minimal two-bundle reproducer):

* One parent+child directory bundle whose parent declares a deny-capable CEL
  guardrails policy (allowlist of tool names, terminal ``{"result": "DENY"}``).
* A byte-identical control bundle differing only in that terminal word
  (``DENY`` -> ``ALLOW``), i.e. an all-ALLOW / not-deny-capable policy.

Both bundles are driven through the identical journey: create a session, send a
user message, and let the (mock) LLM emit one ``sys_session_send(agent="child",
...)`` dispatch call. The runner dispatches ``sys_session_send`` and the tool
result lands in the session as a ``function_call_output`` item.

Both tests assert the same correct behavior — the dispatch returns a launching
handle (``"status": "launching"``) with no inbox error:

* ``test_deny_capable_policy_allows_subagent_dispatch``: the deny-capable
  bundle. Before the fix this failed with ``requires parent session inbox``.
* ``test_allow_only_policy_allows_subagent_dispatch``: the all-ALLOW control,
  which always worked; it pins the baseline the deny-capable case must match.

Topology mirrors tests/e2e/test_coder_subagent.py and
tests/e2e/test_subagent_tool_limit_e2e.py: real server + real runner, mock LLM
scripted per-agent (parent and child each route to their own mock model queue
via a per-agent ``executor.auth.base_url``).
"""

from __future__ import annotations

import io
import json
import tarfile
import time
import uuid
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    OMNIGENT_INTERNAL_WS_ORIGIN,
    configure_mock_llm,
    create_runner_bound_session,
    reset_mock_llm,
    send_user_message_to_session,
)

# Per-child mock-LLM routing (each agent on its own mock model + auth
# base_url) requires a server >= 0.3.0 -- same constraint as
# tests/e2e/test_coder_subagent.py and test_subagent_tool_limit_e2e.py.
pytestmark = [
    pytest.mark.min_server_version("0.3.0"),
    # A parent dispatch turn plus polling for the async dispatch result; allow
    # headroom under the signal-based timeout.
    pytest.mark.timeout(420, method="signal"),
]

# The exact runner error string raised at runner/tool_dispatch.py when
# ``_execute_subagent_tool`` cannot find the parent session's inbox.
_INBOX_ERROR = "requires parent session inbox"

# The deny-capable CEL expression from the reporter's minimal reproducer: it
# ALLOWs the dispatch tool names but has a terminal ``DENY`` branch. The only
# difference between the two bundles is that terminal verdict word.
_ALLOWLIST = '"^(ToolSearch|sys_session_send|sys_read_inbox)$"'


def _cel_expression(*, terminal: str) -> str:
    """Build the allowlist CEL expression with a chosen terminal verdict.

    :param terminal: Terminal-branch verdict, ``"DENY"`` (deny-capable) or
        ``"ALLOW"`` (all-ALLOW control).
    :returns: The CEL expression string.
    """
    return (
        'event.type != "tool_call"\n'
        '  ? {"result": "ALLOW"}\n'
        "  : has(event.data.name)\n"
        "    && type(event.data.name) == string\n"
        f"    && event.data.name.matches({_ALLOWLIST})\n"
        '    ? {"result": "ALLOW"}\n'
        f'    : {{"result": "{terminal}"}}\n'
    )


def _register_bundle(
    client: httpx.Client,
    *,
    name: str,
    parent_model: str,
    child_model: str,
    mock_llm_base_url: str,
    terminal_verdict: str,
) -> str:
    """Upload a parent+child directory bundle whose parent guardrails policy
    uses a CEL expression with the given terminal verdict.

    Mirrors the reporter's two-bundle minimal reproducer: parent config.yaml
    declares ``tools.agents: [child]``, ``async: true`` and a
    ``guardrails.policies`` CEL policy on ``tool_call``; agents/child holds the
    child. Both agents route to their own mock model + auth base_url.

    :param client: HTTP client pointed at the live server.
    :param name: Unique parent agent name for this registration.
    :param parent_model: Mock model key for the parent's response queue.
    :param child_model: Mock model key for the child's response queue.
    :param mock_llm_base_url: Mock server base URL including ``/v1``.
    :param terminal_verdict: ``"DENY"`` (deny-capable) or ``"ALLOW"`` (control).
    :returns: The registered parent agent name.
    """
    auth = {"type": "api_key", "api_key": "mock-key", "base_url": mock_llm_base_url}

    parent_cfg: dict[str, Any] = {
        "spec_version": 1,
        "name": name,
        "description": "Deny-capable-policy dispatch reproducer.",
        "executor": {
            "type": "omnigent",
            "model": parent_model,
            "auth": auth,
            "config": {"harness": "openai-agents"},
        },
        "prompt": (
            "Diagnostic probe. Do exactly what you are asked and quote tool results verbatim."
        ),
        "async": True,
        "tools": {"timeout": 300, "agents": ["child"]},
        "guardrails": {
            "policies": {
                "allowlist_then_deny": {
                    "type": "function",
                    "on": ["tool_call"],
                    "function": {
                        "path": "omnigent.policies.builtins.cel.cel_policy",
                        "arguments": {"expression": _cel_expression(terminal=terminal_verdict)},
                    },
                }
            }
        },
        "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
    }

    child_cfg: dict[str, Any] = {
        "spec_version": 1,
        "name": "child",
        "description": "Child of the deny-capable-policy reproducer.",
        "executor": {
            "type": "omnigent",
            "model": child_model,
            "auth": auth,
            "config": {"harness": "openai-agents"},
        },
        "prompt": "Answer briefly and literally.",
        "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
    }

    import yaml

    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:

            def _add_yaml(arcname: str, config: dict[str, Any]) -> None:
                data = yaml.dump(config).encode()
                info = tarfile.TarInfo(arcname)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

            _add_yaml("config.yaml", parent_cfg)
            _add_yaml("agents/child/config.yaml", child_cfg)
        bundle = buf.getvalue()

    resp = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    if resp.status_code not in (200, 201, 409):
        raise RuntimeError(f"bundle register failed: {resp.status_code} {resp.text[:500]}")
    return name


def _configure_mocks(
    mock_llm_server_url: str,
    *,
    parent_model: str,
    child_model: str,
    child_marker: str,
) -> None:
    """Script the parent to dispatch the child once, then acknowledge; script
    the child to reply with a sentinel marker.

    :param mock_llm_server_url: Mock server base URL.
    :param parent_model: Parent queue key.
    :param child_model: Child queue key.
    :param child_marker: Sentinel the child mock replies with.
    """
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_dispatch_child",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "child",
                                "title": "ping",
                                "args": {"input": "reply exactly PING"},
                            }
                        ),
                    },
                ],
            },
            {"text": "Dispatched child, waiting for result."},
            {"text": f"The child returned: {child_marker}."},
        ],
        key=parent_model,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": child_marker}],
        key=child_model,
    )


def _dispatch_result_output(items: list[dict[str, Any]]) -> str | None:
    """Return the ``sys_session_send`` dispatch result text from session items.

    Scans conversation items for the ``function_call_output`` that answers the
    ``sys_session_send`` call (by its call_id) and returns its output string.
    Falls back to any output item mentioning the inbox error or a launching
    handle so the assertion is robust to minor snapshot-shape differences.

    :param items: Conversation items from the session snapshot.
    :returns: The dispatch tool-result output string, or ``None`` if not found.
    """
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "function_call_output":
            continue
        # The output item nests its fields under ``data`` in the session
        # snapshot (``{"type": "function_call_output", "data": {"call_id":
        # ..., "output": ...}}``); tolerate a flat shape too.
        data = item.get("data") if isinstance(item.get("data"), dict) else item
        if data.get("call_id") != "call_dispatch_child":
            continue
        out = data.get("output")
        if isinstance(out, str):
            return out
    # Fallback: if the inbox error surfaced anywhere in the snapshot (e.g. an
    # error item shaped differently), treat it as the dispatch outcome.
    if _INBOX_ERROR in json.dumps(items):
        return _INBOX_ERROR
    return None


def _wait_for_dispatch_result(
    http_client: httpx.Client,
    session_id: str,
    *,
    timeout_s: float = 180.0,
) -> str:
    """Poll the session snapshot until the ``sys_session_send`` dispatch result
    (its ``function_call_output``) is present, and return its output string.

    :param http_client: HTTP client pointed at the live server.
    :param session_id: Session/conversation id to poll.
    :param timeout_s: Max seconds to wait.
    :returns: The dispatch tool-result output string.
    """
    deadline = time.monotonic() + timeout_s
    last_blob = ""
    while time.monotonic() < deadline:
        resp = http_client.get(f"/v1/sessions/{session_id}")
        resp.raise_for_status()
        items = resp.json().get("items", [])
        last_blob = json.dumps(items)
        result = _dispatch_result_output(items)
        if result is not None:
            return result
        time.sleep(0.5)
    raise AssertionError(
        f"sys_session_send dispatch result not found in session {session_id} "
        f"within {timeout_s:.0f}s. Last items: {last_blob[:2000]}"
    )


def test_deny_capable_policy_allows_subagent_dispatch(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """A parent whose guardrails CEL policy CAN return DENY (terminal branch)
    must still dispatch its child: ``sys_session_send`` returns a launching
    handle, not the ``requires parent session inbox`` runner error.

    Before the fix the synthetic ``sys_agent_start`` init probe hit the
    policy's terminal DENY, session init aborted, and this exact journey
    failed with the inbox error even though the policy ALLOWs the call.
    """
    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-deny-parent-{uid}"
    child_model = f"mock-deny-child-{uid}"
    child_marker = f"PING_DENY_{uid}"

    reset_mock_llm(mock_llm_server_url)

    parent_name = _register_bundle(
        http_client,
        name=f"deny-parent-{uid}",
        parent_model=parent_model,
        child_model=child_model,
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        terminal_verdict="DENY",
    )
    _configure_mocks(
        mock_llm_server_url,
        parent_model=parent_model,
        child_model=child_model,
        child_marker=child_marker,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=parent_name, runner_id=live_runner_id
    )
    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Dispatch the child sub-agent with sys_session_send.",
    )

    result = _wait_for_dispatch_result(http_client, session_id)
    assert _INBOX_ERROR not in result, (
        "Deny-capable-policy dispatch must NOT hit the inbox error (the "
        f"policy ALLOWs sys_session_send); got: {result!r}"
    )
    assert "launching" in result or "task_id" in result or "kind" in result, (
        "Expected a launching sub-agent handle from the deny-capable-policy "
        f"dispatch; got: {result!r}"
    )


def test_allow_only_policy_allows_subagent_dispatch(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """Control: the same bundle whose CEL terminal branch is ``ALLOW`` (not
    deny-capable) dispatches the child successfully -- ``sys_session_send``
    returns a launching handle with no inbox error.

    Byte-identical to the deny-capable bundle apart from that one word; it
    pins the baseline behavior the deny-capable case must match.
    """
    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-allow-parent-{uid}"
    child_model = f"mock-allow-child-{uid}"
    child_marker = f"PING_ALLOW_{uid}"

    reset_mock_llm(mock_llm_server_url)

    parent_name = _register_bundle(
        http_client,
        name=f"allow-parent-{uid}",
        parent_model=parent_model,
        child_model=child_model,
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        terminal_verdict="ALLOW",
    )
    _configure_mocks(
        mock_llm_server_url,
        parent_model=parent_model,
        child_model=child_model,
        child_marker=child_marker,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=parent_name, runner_id=live_runner_id
    )
    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Dispatch the child sub-agent with sys_session_send.",
    )

    result = _wait_for_dispatch_result(http_client, session_id)
    assert _INBOX_ERROR not in result, (
        f"Control (all-ALLOW policy) dispatch must NOT hit the inbox error; got: {result!r}"
    )
    assert "launching" in result or "task_id" in result or "kind" in result, (
        f"Expected a launching sub-agent handle from the control dispatch; got: {result!r}"
    )
