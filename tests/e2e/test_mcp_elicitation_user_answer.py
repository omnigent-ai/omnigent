"""E2E regression test: MCP elicitation delivers the user's
selected answer to the MCP server, not the schema's first option.

Guards against a bug where an MCP server sends ``elicitation/create``
with an enum schema (e.g. ``["dev", "staging", "prod"]``), the user picks
the *third* option ("prod"), but the runner resolves with the *first*
option ("dev") because ``pending_approvals`` only carries a boolean and
``_build_accept_content`` auto-fills from the schema.

Concrete failure path (the observed defect)::

    1. runner receives approval event {action: "accept", content: {answer: "prod"}, …}
    2. runner's approval handler calls
       pending_approvals.resolve(elicitation_id, approved=True)   # content dropped
    3. mcp_manager._elicit resumes with approved=True, calls
       _build_accept_content(params)                              # → "dev" (first enum)
    4. MCP server receives "dev", not "prod"

Test journey::

    1. Create an agent with the ``elicitation_enum_mcp_server`` fixture
       (exposes a ``deploy`` tool that calls ``ctx.elicit`` with a 3-option enum).
    2. Mock LLM returns a function_call for ``deploy``.
    3. In a background thread, poll for the pending elicitation then POST an
       approval with ``content: {"answer": "prod"}`` (the third, non-default
       option — the one that differs from the auto-filled "dev").
    4. Turn completes; assert the ``deploy`` tool's return value contains
       ``"elicit_answer:prod"``, not ``"elicit_answer:dev"``.

When the bug is present the assert fails because the runner ignores
``content`` and auto-fills "dev".  After the fix the assert passes.

Usage::

    pytest tests/e2e/test_mcp_elicitation_user_answer.py -v
"""

from __future__ import annotations

import io
import json as _json
import sys
import tarfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    reset_mock_llm,
    send_user_message_to_session,
)

# ── Constants ────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The elicitation fixture server.
_ELICITATION_MCP_SERVER = (
    _REPO_ROOT / "tests" / "tools" / "fixtures" / "elicitation_enum_mcp_server.py"
)

# Enum values in the fixture schema — must match the server.
_ENV_FIRST = "dev"  # first / auto-fill default (the wrong answer the bug produces)
_ENV_TARGET = "prod"  # third value — the one the test user "selects"

# Marker the deploy tool embeds in its return value so we can find it in output.
_SUCCESS_MARKER = f"elicit_answer:{_ENV_TARGET}"
_FAILURE_MARKER = f"elicit_answer:{_ENV_FIRST}"

_ELICITATION_POLL_TIMEOUT_S = 60.0
_ELICITATION_POLL_INTERVAL_S = 0.5


# ── Helpers ──────────────────────────────────────────────────────────────────


def _build_elicitation_agent_bundle(
    *,
    name: str,
    model: str,
    mock_llm_base_url: str,
) -> bytes:
    """Build a tar.gz agent bundle with the elicitation MCP fixture."""
    assert _ELICITATION_MCP_SERVER.is_file(), (
        f"Expected elicitation MCP fixture at {_ELICITATION_MCP_SERVER}; "
        "update _ELICITATION_MCP_SERVER if the file moved."
    )

    config: dict[str, object] = {
        "name": name,
        "prompt": (
            "You have exactly one tool available: ``deploy``. "
            "When the user asks you to deploy, call the ``deploy`` tool "
            "with no arguments and reply to the user with the tool's exact "
            "return value verbatim."
        ),
        "executor": {
            "harness": "openai-agents",
            "model": model,
            "profile": "",
            "auth": {
                "type": "api_key",
                "api_key": "mock-key",
                "base_url": mock_llm_base_url,
            },
        },
        "tools": {
            "elicitation_mcp": {
                "type": "mcp",
                "command": sys.executable,
                "args": [str(_ELICITATION_MCP_SERVER)],
            },
        },
    }

    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            yaml_bytes = yaml.dump(config).encode()
            info = tarfile.TarInfo(f"{name}.yaml")
            info.size = len(yaml_bytes)
            tar.addfile(info, io.BytesIO(yaml_bytes))
        return buf.getvalue()


def _pending_elicitations(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    """Return outstanding elicitation prompts from the session snapshot."""
    resp = client.get(f"/v1/sessions/{session_id}")
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _wait_for_pending_elicitation(
    client: httpx.Client,
    session_id: str,
    *,
    timeout: float = _ELICITATION_POLL_TIMEOUT_S,
) -> list[dict[str, Any]]:
    """Block until at least one elicitation is parked for *session_id*.

    :raises AssertionError: If no elicitation appears within *timeout* seconds.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = _pending_elicitations(client, session_id)
        if pending:
            return pending
        time.sleep(_ELICITATION_POLL_INTERVAL_S)
    raise AssertionError(
        f"No pending elicitation appeared for session {session_id!r} within {timeout}s."
    )


def _resolve_elicitation_with_content(
    client: httpx.Client,
    session_id: str,
    elicitation_id: str,
    answer: str,
) -> None:
    """POST an approval verdict carrying the user's selected *answer*.

    Uses the ``type: "approval"`` event path — the same path the web UI
    takes when the user clicks an option button on the ApprovalCard.

    :param client: HTTP client pointed at the live server.
    :param session_id: Session that owns the elicitation.
    :param elicitation_id: Correlation id from the pending elicitation.
    :param answer: The enum value the "user" selected (e.g. "prod").
    """
    resp = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "approval",
            "data": {
                "elicitation_id": elicitation_id,
                "action": "accept",
                "content": {"answer": answer},
            },
        },
        timeout=10.0,
    )
    resp.raise_for_status()


def _collect_all_tool_outputs(client: httpx.Client, session_id: str) -> list[str]:
    """Return all function_call_output strings from the session."""
    resp = client.get(f"/v1/sessions/{session_id}/items?limit=200")
    resp.raise_for_status()
    items = resp.json().get("data", [])

    outputs: list[str] = []
    for item in items:
        data = item.get("data") or {}
        itype = item.get("type") or data.get("type")
        output = item.get("output") or data.get("output")
        if itype == "function_call_output" and output is not None:
            outputs.append(str(output))
    return outputs


# ── Test ─────────────────────────────────────────────────────────────────────


def test_mcp_elicitation_delivers_user_selected_answer_not_schema_default(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """MCP elicitation: user picks the 3rd enum option; server receives it.

    With the bug present, ``pending_approvals``
    drops ``content`` (carries only a bool), and ``_build_accept_content``
    auto-fills the first enum value ("dev").  After the fix, ``content``
    flows all the way to the MCP server and the tool's return value
    contains ``"elicit_answer:prod"``.

    Steps:

    1. Register an agent with the elicitation MCP fixture server.
    2. Mock LLM emits a ``function_call`` for ``elicitation_mcp__deploy``
       (namespaced as ``<config_key>__<tool_name>``).
    3. Background thread waits for the pending elicitation then resolves it
       with ``content: {answer: "prod"}`` (the 3rd, non-default option).
    4. Poll until the turn is terminal.
    5. Assert the deploy tool's output contains ``"elicit_answer:prod"``
       and does NOT contain the first-option fallback ``"elicit_answer:dev"``.
    """
    model = f"mock-elicit-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)

    agent_name = f"elicit-test-{uuid.uuid4().hex[:6]}"
    bundle = _build_elicitation_agent_bundle(
        name=agent_name,
        model=model,
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )

    from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN

    resp = http_client.post(
        "/v1/sessions",
        data={"metadata": _json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    if resp.status_code not in (200, 201, 409):
        pytest.fail(f"Agent registration failed: {resp.status_code} {resp.text[:500]}")

    # The tool name Omnigent uses in the function_call is
    # "<config_key>__<tool_name>" where <config_key> is the key under
    # ``tools:`` in the YAML config (``"elicitation_mcp"`` above).
    # The FastMCP server's own name ("elicitation-enum-test") is NOT used
    # in the namespace — the YAML config key is.
    tool_call_name = "elicitation_mcp__deploy"

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "type": "function_call",
                        "name": tool_call_name,
                        "arguments": "{}",
                    }
                ],
            },
            # Second turn: LLM acknowledges the tool result.
            {"text": "Deployment initiated."},
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )

    # ── Background resolver ───────────────────────────────────────────────
    # Runs concurrently with the turn: waits for the elicitation to appear
    # in the session snapshot, then resolves it with the "prod" answer.
    resolver_error: list[Exception] = []

    def _resolve_in_background() -> None:
        try:
            pending = _wait_for_pending_elicitation(http_client, session_id)
            elicitation_id = pending[0]["elicitation_id"]
            _resolve_elicitation_with_content(
                http_client,
                session_id,
                elicitation_id,
                answer=_ENV_TARGET,  # "prod" — the non-default 3rd option
            )
        except Exception as exc:
            resolver_error.append(exc)

    resolver_thread = threading.Thread(target=_resolve_in_background, daemon=True)
    resolver_thread.start()

    # ── Send user message ─────────────────────────────────────────────────
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Please deploy.",
    )

    # ── Poll until terminal ───────────────────────────────────────────────
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=120,
    )

    resolver_thread.join(timeout=10)
    if resolver_error:
        raise AssertionError(
            f"Background elicitation resolver failed: {resolver_error[0]}"
        ) from resolver_error[0]

    assert body["status"] == "completed", (
        f"Turn did not complete successfully: status={body['status']}, error={body.get('error')}"
    )

    # ── Assert the MCP server received the correct answer ─────────────────
    # The deploy tool returns "elicit_answer:<answer>" so we can observe
    # what value the MCP server got.  This surfaces in function_call_output.
    tool_outputs = _collect_all_tool_outputs(http_client, session_id)
    combined_output = " ".join(tool_outputs)

    # BUG: with the content-dropping bug present, combined_output contains
    # "elicit_answer:dev" (the first enum value), not "elicit_answer:prod".
    assert _SUCCESS_MARKER in combined_output, (
        f"MCP server should have received the user-selected answer "
        f"'{_ENV_TARGET}' (marker {_SUCCESS_MARKER!r}), but did not.\n"
        f"  Tool outputs: {tool_outputs}\n"
        f"  This is the content-dropping bug: runner's pending_approvals drops "
        f"content, _build_accept_content auto-fills the first enum value."
    )

    # Belt-and-suspenders: confirm the wrong (auto-filled) value was not used.
    assert _FAILURE_MARKER not in combined_output, (
        f"MCP server received the schema-default first option "
        f"'{_ENV_FIRST}' (marker {_FAILURE_MARKER!r}) instead of the "
        f"user-selected '{_ENV_TARGET}'.  The content-dropping bug is still present.\n"
        f"  Tool outputs: {tool_outputs}"
    )
