"""E2E: an explicit ``sys_session_rename`` must work after the first title lands.

``sys_session_rename`` used to dispatch to ``POST /v1/sessions/{id}/auto-title``,
a one-shot compare-and-swap that only succeeds while the session title still
equals the deterministic first-message seed. The background titler consumes
that slot on turn one, so every subsequent explicit rename returned
``{"renamed": false, "title": null, "reason": "title_changed"}`` forever, even
with no concurrent writer.

Journey exercised (all through the live server + runner + mock LLM):

1. Create a runner-bound session; the first user message seeds the
   deterministic title.
2. Consume the one-shot auto-title slot exactly the way the background titler
   does — a successful ``POST /auto-title`` replacing seed with a generated
   title.
3. The agent (mock LLM) calls ``sys_session_rename``; the runner dispatches it.
   Broken build: the tool result is ``renamed=false / reason="title_changed"``.
   Fixed build: ``renamed=true`` and the title is persisted.
4. A second ``sys_session_rename`` in a later turn must also succeed — the
   rename is repeatable, not another one-shot.

Runs against the mock LLM server — no real API key needed::

    pytest tests/e2e/test_session_rename_repeatable.py -v
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    send_user_message_to_session,
)
from tests.e2e.helpers import POLL_INTERVAL_S


def _get_session_title(client: httpx.Client, session_id: str) -> str | None:
    """Return the current title of *session_id* via ``GET /v1/sessions/{id}``.

    :param client: HTTP client pointed at the live server.
    :param session_id: Session id, e.g. ``"conv_abc123"``.
    :returns: The title string, or ``None`` when the session has no title yet.
    """
    resp = client.get(f"/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("title")


def _wait_for_seed_title(
    client: httpx.Client,
    session_id: str,
    *,
    timeout: float = 30.0,
) -> str:
    """Poll until the server seeds a title from the first message.

    :param client: HTTP client pointed at the live server.
    :param session_id: Session id.
    :param timeout: Maximum seconds to wait.
    :returns: The seed title.
    :raises AssertionError: If no title appears within *timeout* seconds.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        title = _get_session_title(client, session_id)
        if title is not None:
            return title
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"Session {session_id!r} never received a seed title after the first "
        "message. The title-seeding step may have changed."
    )


def _rename_tool_result(
    body: dict[str, Any],
    call_id: str,
) -> dict[str, Any]:
    """Extract and parse the ``sys_session_rename`` tool result for *call_id*.

    :param body: Terminal response body from
        :func:`poll_session_until_terminal`.
    :param call_id: The mock-issued tool call id, e.g. ``"call_rename_1"``.
    :returns: The parsed JSON payload the runner returned to the LLM.
    :raises AssertionError: If no tool result for *call_id* is present.
    """
    for item in body.get("output", []):
        if item.get("type") == "function_call_output" and item.get("call_id") == call_id:
            output = item.get("output")
            assert isinstance(output, str), (
                f"tool output for {call_id!r} is not a string: {item!r}"
            )
            return json.loads(output)
    raise AssertionError(
        f"No function_call_output for {call_id!r} in session output: "
        f"{[i.get('type') for i in body.get('output', [])]}"
    )


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_sys_session_rename_succeeds_after_first_title_landed(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
) -> None:
    """An agent rename works — repeatedly — once the seed title was replaced.

    **Failure mode this guards:** the rename tool and the background titler
    shared the one-shot ``/auto-title`` CAS slot. After the titler's first
    write (seed → generated title), every ``sys_session_rename`` returned
    ``{"renamed": false, "title": null, "reason": "title_changed"}`` even
    with no concurrent writer — the tool was permanently dead.

    **What fixed looks like:** the tool routes through the repeatable session
    rename path, so it returns ``{"renamed": true, "title": <requested>}``
    and the title is persisted, on the first call and on later calls alike.

    :param http_client: HTTP client pointed at the live server.
    :param live_runner_id: Runner id registered with the live server.
    :param mock_llm_server_url: Mock LLM server URL (``None`` when using a
        real LLM, in which case the mock-llm configure calls are no-ops).
    """
    # Unique per-run queue key — no shared-state reset needed (and a reset
    # would clear other tests' queues on the session-scoped mock server).
    model = f"mock-rename-{uuid.uuid4().hex[:8]}"

    first_rename_title = "Investigate flaky auth timeout"
    second_rename_title = "Verify auth timeout fix"

    # Turn 1: plain ack (lets the first message complete and seed the title).
    # Turn 2: the agent calls sys_session_rename, then acks the result.
    # Turn 3: a second sys_session_rename, then acks — proves repeatability.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": "Acknowledged the task."},
            {
                "tool_calls": [
                    {
                        "call_id": "call_rename_1",
                        "name": "sys_session_rename",
                        "arguments": json.dumps({"title": first_rename_title}),
                    }
                ]
            },
            {"text": "Renamed the session once."},
            {
                "tool_calls": [
                    {
                        "call_id": "call_rename_2",
                        "name": "sys_session_rename",
                        "arguments": json.dumps({"title": second_rename_title}),
                    }
                ]
            },
            {"text": "Renamed the session again."},
        ],
        key=model,
    )

    agent_name = register_inline_agent(
        http_client,
        name=f"rename-fix-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a terse assistant.",
        mock_llm_base_url=(f"{mock_llm_server_url}/v1" if mock_llm_server_url else None),
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )

    # Step 1 — first user message; the server seeds the deterministic title.
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="investigate the authentication timeout in the production database",
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=120
    )
    assert body["status"] == "completed", (
        f"first turn did not complete: status={body.get('status')!r}, error={body.get('error')!r}"
    )
    seed_title = _wait_for_seed_title(http_client, session_id)

    # Step 2 — consume the one-shot auto-title slot exactly the way the
    # background titler does: a successful seed → generated-title CAS. After
    # this, the session title no longer matches the seed, which is the state
    # every real session reaches after its first turn.
    generated_title = "Debug production database access"
    auto_resp = http_client.post(
        f"/v1/sessions/{session_id}/auto-title",
        json={"title": generated_title},
        timeout=10.0,
    )
    assert auto_resp.status_code == 200, (
        f"POST /auto-title returned {auto_resp.status_code}: {auto_resp.text}"
    )
    auto_body = auto_resp.json()
    assert auto_body.get("renamed") is True, (
        f"The first (titler-style) auto-title write should succeed while the "
        f"seed {seed_title!r} is intact, got: {auto_body!r}"
    )

    # Step 3 — the agent renames the session. On the broken build the tool
    # result is renamed=false/reason="title_changed" because the one-shot
    # slot is gone; on a fixed build it succeeds.
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Rename this session to reflect the investigation.",
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=120
    )
    assert body["status"] == "completed", (
        f"rename turn did not complete: status={body.get('status')!r}, error={body.get('error')!r}"
    )
    result = _rename_tool_result(body, "call_rename_1")
    assert result.get("renamed") is True, (
        f"sys_session_rename failed after the first title landed: {result!r}\n"
        f"Expected renamed=true — an explicit rename must not share the "
        f"background titler's one-shot seed slot."
    )
    assert _get_session_title(http_client, session_id) == first_rename_title, (
        "The rename reported success but the title was not persisted."
    )

    # Step 4 — a second rename must also succeed: the fix makes the rename
    # repeatable, not merely a second one-shot.
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Rename the session again, the work moved on.",
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=120
    )
    assert body["status"] == "completed", (
        f"second rename turn did not complete: status={body.get('status')!r}, "
        f"error={body.get('error')!r}"
    )
    result = _rename_tool_result(body, "call_rename_2")
    assert result.get("renamed") is True, (
        f"The second sys_session_rename failed: {result!r}\n"
        f"Renames must be repeatable as the session's work evolves."
    )
    assert _get_session_title(http_client, session_id) == second_rename_title, (
        "The second rename reported success but the title was not persisted."
    )
