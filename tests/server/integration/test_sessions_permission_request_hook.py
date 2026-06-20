"""
Integration tests for ``POST /v1/sessions/{id}/hooks/permission-request``.

The endpoint receives Claude Code's ``PermissionRequest`` HTTP hook
payload (when ``omnigent claude`` wraps the native TUI), parks
the call on the same in-memory elicitation registry the claude-sdk
path uses, emits an SSE event for the web UI's :file:`ApprovalCard`,
and returns Claude's expected ``hookSpecificOutput`` decision JSON
once the UI verdict arrives.

Tests cover three round-trips:

- Allow: the UI resolves the elicitation via a session
  ``approval`` event → endpoint returns
  ``decision.behavior == "allow"``.
- Deny: same path but the UI declines → ``decision.behavior == "deny"``.
- Timeout: nobody resolves the elicitation → endpoint returns
  ``200`` with empty body so Claude defers to its TUI prompt
  (fail-ask).

Uses the shared ``client`` fixture from ``tests/server/conftest.py``
(real stores + mock LLM) so the tests exercise the real route →
``_harness_elicitation_registry`` → SSE-publish pipeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import httpx
import pytest

from omnigent.codex_native_elicitation import codex_elicitation_id
from omnigent.runtime import session_stream
from omnigent.server._elicitation_registry import (
    _harness_pre_resolved_elicitations,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.routes import sessions as sessions_route
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def _create_session(client: httpx.AsyncClient, agent_id: str) -> str:
    """
    Create a minimal session and return its id.

    :param client: Test HTTP client.
    :param agent_id: Agent to bind.
    :returns: New session id.
    """
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id})
    assert resp.status_code == 201, f"create failed: {resp.status_code} {resp.text}"
    return resp.json()["id"]


async def _drain_until_elicitation(
    session_id: str,
    *,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    """
    Block on the session SSE stream until a
    ``response.elicitation_request`` event arrives, then return the
    full event dict.

    The endpoint publishes the SSE event before parking on the
    elicitation future, so subscribing here is the simplest way to
    learn the id without monkey-patching ``uuid``. Returning the
    whole event lets callers also inspect the params block for the
    Claude-native extras (``tool_use_id``, ``cwd``, ``permission_mode``).

    :param session_id: Session to subscribe to.
    :param timeout_s: Maximum seconds to wait for the elicitation
        event before failing the test.
    :returns: The captured elicitation_request event dict.
    """
    async with asyncio.timeout(timeout_s):
        async for event in session_stream.subscribe(session_id):
            if event.get("type") == "response.elicitation_request":
                elicitation_id = event.get("elicitation_id")
                assert isinstance(elicitation_id, str) and elicitation_id, (
                    f"elicitation event missing id: {event!r}"
                )
                return event
    raise AssertionError("subscribe loop ended without an elicitation event")


async def _post_approval(
    client: httpx.AsyncClient,
    session_id: str,
    elicitation_id: str,
    action: str,
    content: dict[str, Any] | None = None,
) -> httpx.Response:
    """
    Resolve a published elicitation through the session event API.

    :param client: Test HTTP client.
    :param session_id: Session that emitted the elicitation.
    :param elicitation_id: Elicitation id from the stream event,
        e.g. ``"elicit_abc123"``.
    :param action: MCP ``ElicitResult.action`` literal,
        e.g. ``"accept"`` or ``"decline"``.
    :param content: Optional MCP ``ElicitResult.content`` payload.
    :returns: The HTTP response from the session event route.
    """
    data: dict[str, Any] = {"elicitation_id": elicitation_id, "action": action}
    if content is not None:
        data["content"] = content
    return await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "approval",
            "data": data,
        },
    )


async def _claude_permission_payload(tool_name: str = "Bash") -> dict[str, Any]:
    """
    Build a realistic Claude PermissionRequest hook body.

    :param tool_name: Tool Claude wants to call.
    :returns: JSON-serializable payload mirroring Claude Code's
        published wire shape for the ``PermissionRequest`` event.
    """
    return {
        "session_id": "claude_sess_abc",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/tmp/cwd",
        "permission_mode": "default",
        "hook_event_name": "PermissionRequest",
        "tool_name": tool_name,
        "tool_input": {"command": "ls -la"},
        "tool_use_id": "tool_use_xyz",
    }


async def test_permission_request_hook_allow_round_trip(
    client: httpx.AsyncClient,
) -> None:
    """
    UI approves Claude's permission request → endpoint returns
    ``decision.behavior == "allow"`` in the Claude hookSpecificOutput
    shape.

    Failure modes this catches: the SSE event is never emitted (UI
    never sees the request, can't approve); the elicitation registry
    doesn't park the future (verdict comes in but the endpoint never
    wakes); the verdict→decision mapping returns the wrong literal.
    """
    agent = await create_test_agent(client, "test-permission-allow")
    session_id = await _create_session(client, agent["id"])
    payload = await _claude_permission_payload()

    # Fire the hook POST and the verdict event concurrently — the hook
    # parks on the registry, the verdict resolves it. Subscribing to
    # the session stream is how we learn the elicitation id.
    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    # Give the subscriber a moment to register before the publisher
    # fires (publish is broadcast-to-current-subscribers — pre-subscribe
    # events are lost).
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/permission-request",
            json=payload,
        )
    )

    event = await drain_task
    verdict = await _post_approval(client, session_id, event["elicitation_id"], "accept")
    assert verdict.status_code == 202, verdict.text

    resp = await hook_task
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }


async def test_top_level_elicitations_route_is_not_mounted(
    client: httpx.AsyncClient,
) -> None:
    """
    Omnigent no longer exposes ``POST /v1/elicitations/{id}``.

    Approval verdicts must travel through the session event route so
    they share normal session scoping and authorization. If the AP
    router gets mounted again, this test turns red immediately.
    """
    resp = await client.post(
        "/v1/elicitations/elicit_removed",
        json={"action": "accept"},
    )
    assert resp.status_code == 404, resp.text


async def test_permission_request_hook_deny_round_trip(
    client: httpx.AsyncClient,
) -> None:
    """
    UI declines Claude's permission request → endpoint returns
    ``decision.behavior == "deny"``.

    Mirrors the allow test but the verdict carries
    ``action: "decline"``. If this regresses, Claude proceeds with
    tools the user explicitly rejected.
    """
    agent = await create_test_agent(client, "test-permission-deny")
    session_id = await _create_session(client, agent["id"])
    payload = await _claude_permission_payload(tool_name="Edit")

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/permission-request",
            json=payload,
        )
    )

    event = await drain_task
    verdict = await _post_approval(client, session_id, event["elicitation_id"], "decline")
    assert verdict.status_code == 202, verdict.text

    resp = await hook_task
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["hookSpecificOutput"]["decision"]["behavior"] == "deny"


async def test_permission_request_hook_allow_all_edits_round_trip(
    client: httpx.AsyncClient,
) -> None:
    """
    Edit-tool prompt: the endpoint stamps ``allow_all_edits`` on the
    published params, and resolving with ``content.allow_all_edits``
    makes the decision carry a ``setMode`` permission update that
    switches the session into ``acceptEdits`` mode.

    This is the web equivalent of Claude Code's native shift+tab
    "auto-accept edits" toggle. Failure modes this catches: the gate
    doesn't recognize ``Edit`` as an edit tool (button never offered);
    the verdict→decision mapping drops ``updatedPermissions`` (the
    session never switches mode, so the button silently degrades to a
    plain Approve); the ``setMode`` entry has the wrong field
    names/values (Claude Code ignores it).
    """
    agent = await create_test_agent(client, "test-permission-allow-all-edits")
    session_id = await _create_session(client, agent["id"])
    # ``permission_mode: "default"`` (set by _claude_permission_payload)
    # is the mode that still prompts for edits, so the hint applies.
    payload = await _claude_permission_payload(tool_name="Edit")

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/permission-request",
            json=payload,
        )
    )

    event = await drain_task
    # The UI hint is stamped only for edit tools — it drives the
    # "Accept & allow all edits" button.
    assert event["params"]["allow_all_edits"] is True

    verdict = await _post_approval(
        client,
        session_id,
        event["elicitation_id"],
        "accept",
        content={"allow_all_edits": True},
    )
    assert verdict.status_code == 202, verdict.text

    resp = await hook_task
    assert resp.status_code == 200, resp.text
    decision = resp.json()["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "allow"
    # The setMode permission update is what actually switches the
    # session to acceptEdits. Shape matches the Agent SDK's
    # PermissionUpdate ``setMode`` variant; "session" scopes it to this
    # session so it resets on the next one.
    assert decision["updatedPermissions"] == [
        {"type": "setMode", "mode": "acceptEdits", "destination": "session"}
    ]


async def test_permission_request_hook_no_allow_all_edits_for_non_edit_tool(
    client: httpx.AsyncClient,
) -> None:
    """
    Non-edit tool (Bash): the endpoint does NOT stamp
    ``allow_all_edits``, and a plain accept produces a decision with
    no ``updatedPermissions``.

    Guards the user's core concern — the option must never surface
    where switching to ``acceptEdits`` would be a no-op (acceptEdits
    only auto-approves Edit/Write/MultiEdit/NotebookEdit, not Bash).
    """
    agent = await create_test_agent(client, "test-permission-no-edits-bash")
    session_id = await _create_session(client, agent["id"])
    payload = await _claude_permission_payload(tool_name="Bash")

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/permission-request",
            json=payload,
        )
    )

    event = await drain_task
    # No hint for Bash — the button is gated off client-side.
    assert "allow_all_edits" not in event["params"]

    verdict = await _post_approval(client, session_id, event["elicitation_id"], "accept")
    assert verdict.status_code == 202, verdict.text

    resp = await hook_task
    assert resp.status_code == 200, resp.text
    decision = resp.json()["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "allow"
    # A plain Approve must not mint a mode switch.
    assert "updatedPermissions" not in decision


async def test_permission_request_hook_spoofed_allow_all_edits_on_non_edit_tool_ignored(
    client: httpx.AsyncClient,
) -> None:
    """
    A client that sends ``content.allow_all_edits: true`` on a NON-edit
    tool (Bash) must NOT receive a ``setMode`` decision.

    The server re-derives eligibility from the gated tool/mode at the
    verdict site instead of trusting the client's content flag. Without
    that re-check, a crafted approval payload could flip the session
    into ``acceptEdits`` on a prompt the affordance was never offered
    for — the exact gating bypass this guards. (The UI never sends this;
    the test forges the payload directly.)
    """
    agent = await create_test_agent(client, "test-permission-spoofed-edits")
    session_id = await _create_session(client, agent["id"])
    payload = await _claude_permission_payload(tool_name="Bash")

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/permission-request",
            json=payload,
        )
    )

    event = await drain_task
    # Forge the flag the UI would never attach to a Bash prompt.
    verdict = await _post_approval(
        client,
        session_id,
        event["elicitation_id"],
        "accept",
        content={"allow_all_edits": True},
    )
    assert verdict.status_code == 202, verdict.text

    resp = await hook_task
    assert resp.status_code == 200, resp.text
    decision = resp.json()["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "allow"
    # Server ignores the spoofed flag — no mode switch for a non-edit tool.
    assert "updatedPermissions" not in decision


async def test_permission_request_hook_edit_plain_accept_has_no_mode_switch(
    client: httpx.AsyncClient,
) -> None:
    """
    Edit-tool prompt accepted via plain Approve (no
    ``allow_all_edits`` flag) → decision is a plain ``allow`` with no
    ``updatedPermissions``.

    Regression guard: the ``setMode`` update must be opt-in per click.
    If the endpoint emitted it for every edit-tool accept, clicking
    plain Approve would silently switch the whole session into
    auto-accept-edits — exactly the surprise this gating avoids.
    """
    agent = await create_test_agent(client, "test-permission-edit-plain")
    session_id = await _create_session(client, agent["id"])
    payload = await _claude_permission_payload(tool_name="Write")

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/permission-request",
            json=payload,
        )
    )

    event = await drain_task
    # The hint is present (Write is an edit tool)…
    assert event["params"]["allow_all_edits"] is True
    # …but a plain accept (no content flag) must not carry it through.
    verdict = await _post_approval(client, session_id, event["elicitation_id"], "accept")
    assert verdict.status_code == 202, verdict.text

    resp = await hook_task
    assert resp.status_code == 200, resp.text
    decision = resp.json()["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "allow"
    assert "updatedPermissions" not in decision


async def test_permission_request_hook_forwards_cwd_and_permission_mode(
    client: httpx.AsyncClient,
) -> None:
    """
    Claude's ``cwd`` and ``permission_mode`` make it onto the
    published elicitation request params as extras under MCP's
    ``extra="allow"`` policy.

    ``cwd`` rides along for UI cues like "Bash in /etc";
    ``permission_mode`` lets the UI badge the card with the mode
    Claude is in (``"default"`` / ``"acceptEdits"`` / ``"plan"``).

    Note: ``tool_use_id`` is intentionally not asserted here.
    Claude Code's PermissionRequest payload doesn't carry one
    (the id is only minted when the tool call is emitted, AFTER
    the permission check). The UI's auto-clear falls back to a
    "first pending" heuristic instead — see
    :file:`ap-web/src/store/chatStore.ts`'s ``tool_call`` case.
    """
    agent = await create_test_agent(client, "test-permission-extras")
    session_id = await _create_session(client, agent["id"])
    payload = await _claude_permission_payload()
    payload["cwd"] = "/home/user/project"
    payload["permission_mode"] = "default"

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/permission-request",
            json=payload,
        )
    )

    event = await drain_task
    params = event["params"]
    assert params["cwd"] == "/home/user/project"
    assert params["permission_mode"] == "default"

    # Resolve so the hook task doesn't time out and stall pytest.
    verdict = await _post_approval(client, session_id, event["elicitation_id"], "accept")
    assert verdict.status_code == 202, verdict.text
    resp = await hook_task
    assert resp.status_code == 200, resp.text


async def test_permission_request_hook_surfaces_ask_user_question_structured(
    client: httpx.AsyncClient,
) -> None:
    """
    When the gated tool is AskUserQuestion, the endpoint stamps
    a structured ``ask_user_question`` payload onto the
    elicitation params (under MCP ``extra="allow"``) so the web
    UI can render an interactive form WITHOUT having to parse the
    truncated ``content_preview`` JSON string.

    Critical: ``content_preview`` is hard-capped at 1024 chars,
    which is enough for Bash/Edit but the AskUserQuestion payload
    blows past it (the live sample the user pasted truncated
    mid-string in the third question). Surfacing the full
    questions + options structurally is the only way to render
    the form reliably.
    """
    agent = await create_test_agent(client, "test-permission-aqu-structured")
    session_id = await _create_session(client, agent["id"])
    payload = await _claude_permission_payload(tool_name="AskUserQuestion")
    payload["tool_input"] = {
        "questions": [
            {
                "question": "Which programming language?",
                "header": "Language",
                "options": [
                    {"label": "Python", "description": "Dynamic, ML-heavy"},
                    {"label": "Go", "description": "Static, fast"},
                    {"label": "Rust", "description": "Memory-safe"},
                ],
                "multiSelect": False,
            },
            {
                "question": "Which dev tools? (multi)",
                "header": "Tools",
                "options": [
                    {"label": "Claude Code"},
                    {"label": "VS Code"},
                ],
                "multiSelect": True,
            },
        ],
    }

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/permission-request",
            json=payload,
        )
    )

    event = await drain_task
    params = event["params"]
    assert "ask_user_question" in params, params
    aqu = params["ask_user_question"]
    assert len(aqu["questions"]) == 2
    first = aqu["questions"][0]
    assert first["question"] == "Which programming language?"
    assert first["header"] == "Language"
    assert first["multiSelect"] is False
    assert first["options"][0] == {"label": "Python", "description": "Dynamic, ML-heavy"}
    second = aqu["questions"][1]
    assert second["multiSelect"] is True
    # Options without descriptions ride through with just the label.
    assert second["options"][0] == {"label": "Claude Code"}

    # Resolve through the supported session-scoped event route so the
    # hook task doesn't time out and stall pytest.
    verdict = await _post_approval(client, session_id, event["elicitation_id"], "accept")
    assert verdict.status_code == 202, verdict.text
    resp = await hook_task
    assert resp.status_code == 200, resp.text


async def test_permission_request_hook_substitutes_ask_user_question_answers(
    client: httpx.AsyncClient,
) -> None:
    """
    When the gated tool is AskUserQuestion and the user accepts
    with selections in ``content``, the hook's response decision
    carries ``updatedInput`` mirroring the original ``tool_input``
    plus an ``answers`` field populated from the verdict content.

    Without this, the form selection is recorded server-side but
    Claude never sees it — the LLM keeps blocking on the TUI
    picker even though the user already answered via the web UI.
    """
    agent = await create_test_agent(client, "test-permission-aqu-substitute")
    session_id = await _create_session(client, agent["id"])
    payload = await _claude_permission_payload(tool_name="AskUserQuestion")
    payload["tool_input"] = {
        "questions": [
            {
                "question": "Which framework should we use?",
                "header": "Framework",
                "options": [
                    {"label": "React", "description": "JS UI library"},
                    {"label": "Vue", "description": "Progressive framework"},
                ],
                "multiSelect": False,
            },
            {
                "question": "Pick snacks",
                "header": "Snacks",
                "options": [
                    {"label": "Popcorn", "description": "Classic"},
                    {"label": "Pretzels", "description": "Salty"},
                ],
                "multiSelect": True,
            },
        ],
    }

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/permission-request",
            json=payload,
        )
    )

    event = await drain_task
    verdict = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "approval",
            "data": {
                "elicitation_id": event["elicitation_id"],
                "action": "accept",
                # Flat MCP ``ElicitResult.content`` shape — each question
                # text is one top-level field; single-select is a string,
                # multi-select is a ``list[str]``.
                "content": {
                    "Which framework should we use?": "Vue",
                    "Pick snacks": ["Popcorn", "Pretzels"],
                },
            },
        },
    )
    assert verdict.status_code == 202, verdict.text

    resp = await hook_task
    assert resp.status_code == 200, resp.text
    body = resp.json()
    decision = body["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "allow"
    assert decision["updatedInput"]["questions"] == payload["tool_input"]["questions"]
    assert decision["updatedInput"]["answers"] == {
        "Which framework should we use?": "Vue",
        "Pick snacks": ["Popcorn", "Pretzels"],
    }


async def test_permission_request_hook_no_updated_input_without_answers(
    client: httpx.AsyncClient,
) -> None:
    """
    Approving an AskUserQuestion elicitation WITHOUT content (e.g.
    a bare ``{"action": "accept"}`` verdict) must NOT add an empty
    ``updatedInput`` to the decision — Claude would interpret that
    as "the user picked nothing" and the LLM would see an empty
    answer set. Bare-accept should fall through to the TUI picker.
    """
    agent = await create_test_agent(client, "test-permission-aqu-bare-accept")
    session_id = await _create_session(client, agent["id"])
    payload = await _claude_permission_payload(tool_name="AskUserQuestion")
    payload["tool_input"] = {
        "questions": [
            {
                "question": "Q",
                "header": "H",
                "options": [{"label": "A", "description": "a"}],
                "multiSelect": False,
            },
        ],
    }

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/permission-request",
            json=payload,
        )
    )

    event = await drain_task
    verdict = await _post_approval(client, session_id, event["elicitation_id"], "accept")
    assert verdict.status_code == 202
    resp = await hook_task
    decision = resp.json()["hookSpecificOutput"]["decision"]
    assert decision == {"behavior": "allow"}


async def test_permission_request_hook_surfaces_option_preview(
    client: httpx.AsyncClient,
) -> None:
    """
    When an AskUserQuestion option carries a ``preview`` field,
    the server's structured extraction surfaces it verbatim on
    the published elicitation params. Without this passthrough
    the UI can't render the <pre> preview block — the
    structured-extra helper was dropping ``preview`` even though
    Claude included it on the wire.
    """
    agent = await create_test_agent(client, "test-permission-aqu-preview")
    session_id = await _create_session(client, agent["id"])
    payload = await _claude_permission_payload(tool_name="AskUserQuestion")
    payload["tool_input"] = {
        "questions": [
            {
                "question": "Which layout?",
                "header": "Layout",
                "options": [
                    {
                        "label": "Two-pane",
                        "description": "Side-by-side",
                        "preview": "[editor] | [output]",
                    },
                    {
                        "label": "Single",
                        "description": "One pane",
                        # No preview on this option — must NOT appear
                        # in the structured payload.
                    },
                ],
                "multiSelect": False,
            },
        ],
    }

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/permission-request",
            json=payload,
        )
    )

    event = await drain_task
    aqu = event["params"]["ask_user_question"]
    options = aqu["questions"][0]["options"]
    assert options[0]["preview"] == "[editor] | [output]"
    assert "preview" not in options[1]

    verdict = await _post_approval(client, session_id, event["elicitation_id"], "accept")
    assert verdict.status_code == 202
    await hook_task


async def test_permission_request_hook_surfaces_exit_plan_mode_input(
    client: httpx.AsyncClient,
) -> None:
    """
    When the gated tool is ExitPlanMode, the endpoint stamps the
    FULL ``tool_input`` verbatim onto the elicitation params as an
    ``exit_plan_mode`` extra (under MCP ``extra="allow"``) so the
    web UI can render a dedicated plan-review card.

    Critical: ``content_preview`` is hard-capped at 1024 chars and
    real plans blow well past it — the structured extra is the only
    untruncated source. The input shape also varies across Claude
    Code builds (``plan`` markdown, ``allowedPrompts``, future
    fields), so the endpoint must pass every field through without
    filtering.
    """
    agent = await create_test_agent(client, "test-permission-epm-structured")
    session_id = await _create_session(client, agent["id"])
    payload = await _claude_permission_payload(tool_name="ExitPlanMode")
    # ExitPlanMode fires while Claude is in plan mode.
    payload["permission_mode"] = "plan"
    # Plan markdown longer than the 1024-char content_preview cap —
    # proves the structured extra is NOT subject to the truncation.
    long_plan = "# Migration plan\n\n" + ("- step: do the thing\n" * 100)
    assert len(long_plan) > 1024
    payload["tool_input"] = {
        "plan": long_plan,
        # ``allowedPrompts`` is the newer-build field requesting
        # prompt-based permissions alongside the plan. An unknown
        # extra field rides along to prove no-filtering.
        "allowedPrompts": [
            {"tool": "Bash", "prompt": "run tests"},
            {"tool": "Bash", "prompt": "install dependencies"},
        ],
        "futureField": {"nested": True},
    }

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/permission-request",
            json=payload,
        )
    )

    event = await drain_task
    params = event["params"]
    # The whole native tool_input — plan, allowedPrompts, and the
    # unknown field — must ride through verbatim. A missing key or a
    # truncated plan means the endpoint filtered/capped the extra.
    assert params["exit_plan_mode"] == payload["tool_input"], params
    # Plan cards are eligible for the "auto-accept edits" affordance
    # (Claude's native "Yes, and auto-accept edits" dialog option), so
    # the UI-button hint must be stamped alongside the plan.
    assert params["allow_all_edits"] is True

    # Resolve so the hook task doesn't time out and stall pytest.
    verdict = await _post_approval(client, session_id, event["elicitation_id"], "accept")
    assert verdict.status_code == 202, verdict.text
    resp = await hook_task
    assert resp.status_code == 200, resp.text
    # A plain accept ("Yes, manually approve edits") pins the session
    # to the prompting ``default`` mode rather than trusting whatever
    # mode Claude's plan-exit restores — every subsequent edit must
    # prompt. ``auto`` here would mean the auto-mode branch fired
    # without the allow_all_edits flag.
    assert resp.json()["hookSpecificOutput"]["decision"] == {
        "behavior": "allow",
        "updatedPermissions": [{"type": "setMode", "mode": "default", "destination": "session"}],
    }


async def test_permission_request_hook_exit_plan_mode_auto_accept_round_trip(
    client: httpx.AsyncClient,
) -> None:
    """
    Accepting an ExitPlanMode prompt with ``allow_all_edits: true``
    in the verdict content returns ``behavior: allow`` PLUS a
    ``setMode → auto`` permission update — the plan card's "Yes, and
    use auto mode" option: exit plan mode and continue in Claude's
    ``auto`` mode (NOT the narrower ``acceptEdits`` the edit-tool
    affordance uses).

    Without the eligibility extension this verdict would come back
    as a plain allow and the user would be re-prompted for every
    edit the plan goes on to make.
    """
    agent = await create_test_agent(client, "test-permission-epm-auto-accept")
    session_id = await _create_session(client, agent["id"])
    payload = await _claude_permission_payload(tool_name="ExitPlanMode")
    payload["permission_mode"] = "plan"
    payload["tool_input"] = {"plan": "# Plan\n\n- do the thing"}

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/permission-request",
            json=payload,
        )
    )

    event = await drain_task
    verdict = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "approval",
            "data": {
                "elicitation_id": event["elicitation_id"],
                "action": "accept",
                # The UI's "auto-accept edits" button sends the same
                # flag the edit-tool affordance uses.
                "content": {"allow_all_edits": True},
            },
        },
    )
    assert verdict.status_code == 202, verdict.text

    resp = await hook_task
    assert resp.status_code == 200, resp.text
    decision = resp.json()["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "allow"
    # The setMode echo is what flips Claude into auto mode after it
    # exits plan mode; ``destination: session`` scopes it so the mode
    # resets on the next session rather than persisting to settings.
    # ``acceptEdits`` here would mean the edit-tool branch leaked onto
    # the plan path.
    assert decision["updatedPermissions"] == [
        {"type": "setMode", "mode": "auto", "destination": "session"}
    ]


async def test_permission_request_hook_decline_forwards_feedback_message(
    client: httpx.AsyncClient,
) -> None:
    """
    Declining with ``content.feedback`` (the plan card's "Reject with
    feedback" flow) returns ``behavior: deny`` plus the feedback as
    ``decision.message`` — Claude Code surfaces it as the denial
    reason, so the model stays in plan mode and revises toward the
    feedback instead of guessing why the plan was refused.

    (The bare-decline shape — no ``message`` key — is covered by
    ``test_permission_request_hook_deny_round_trip``.)
    """
    agent = await create_test_agent(client, "test-permission-epm-feedback")
    session_id = await _create_session(client, agent["id"])
    payload = await _claude_permission_payload(tool_name="ExitPlanMode")
    payload["permission_mode"] = "plan"
    payload["tool_input"] = {"plan": "# Plan\n\n- rewrite everything"}

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/permission-request",
            json=payload,
        )
    )

    event = await drain_task
    verdict = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "approval",
            "data": {
                "elicitation_id": event["elicitation_id"],
                "action": "decline",
                "content": {"feedback": "Too risky — split it into two phases."},
            },
        },
    )
    assert verdict.status_code == 202, verdict.text

    resp = await hook_task
    assert resp.status_code == 200, resp.text
    decision = resp.json()["hookSpecificOutput"]["decision"]
    # Feedback must ride through verbatim; a missing ``message`` means
    # the user's revision guidance never reached Claude.
    assert decision == {
        "behavior": "deny",
        "message": "Too risky — split it into two phases.",
    }


async def test_permission_request_hook_omits_structured_extras_for_other_tools(
    client: httpx.AsyncClient,
) -> None:
    """
    Tools other than AskUserQuestion / ExitPlanMode must NOT carry
    the ``ask_user_question`` / ``exit_plan_mode`` extras on their
    elicitation params. Without this guard the UI would attempt to
    render an interactive form (or a plan-review card) for a Bash
    permission prompt — wrong shape, wrong UX.
    """
    agent = await create_test_agent(client, "test-permission-aqu-absent")
    session_id = await _create_session(client, agent["id"])

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/permission-request",
            json=await _claude_permission_payload(),  # default tool_name="Bash"
        )
    )

    event = await drain_task
    params = event["params"]
    assert "ask_user_question" not in params, params
    assert "exit_plan_mode" not in params, params

    verdict = await _post_approval(client, session_id, event["elicitation_id"], "accept")
    assert verdict.status_code == 202
    await hook_task


async def test_permission_request_hook_timeout_returns_empty_body(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When no verdict arrives within the wait budget, the endpoint
    returns ``200`` with an empty body — Claude Code's HTTP hook
    contract treats that as "defer to the TUI prompt" (fail-ask).

    Without this contract: an unattended UI would block Claude's
    tool call for the full hook timeout (Claude's own default is
    ~10 minutes), then default to allow on its end. Returning empty
    early lets Claude fall back to its terminal prompt promptly.

    Asserts the response body is literally empty, not just empty
    JSON ``{}`` (the docs explicitly distinguish the two; only the
    empty-body form triggers TUI fallback cleanly).
    """
    monkeypatch.setattr(
        sessions_route,
        "_CLAUDE_NATIVE_PERMISSION_HOOK_TIMEOUT_S",
        0.1,
    )
    agent = await create_test_agent(client, "test-permission-timeout")
    session_id = await _create_session(client, agent["id"])
    payload = await _claude_permission_payload()

    resp = await client.post(
        f"/v1/sessions/{session_id}/hooks/permission-request",
        json=payload,
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == b"", f"expected empty body on timeout, got {resp.content!r}"


async def test_permission_request_hook_timeout_clears_pending_index(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The pending-elicitations index is decremented when the hook
    times out — Claude's fail-ask fallback (user answers in the
    TUI instead of the web UI) is the ONLY signal the Omnigent server
    gets that the prompt is done.

    Without this, the sidebar badge stays stuck forever: the
    increment ran at SSE-publish time, but no
    ``approval`` POST ever arrives to clear it because the user
    answered out-of-band through Claude's TUI prompt.

    Asserts on the index state directly because the path the user
    reported is exactly "I answered in Claude, the badge never
    cleared" — i.e. the visible artifact is the index value
    flowing through ``GET /v1/sessions``.
    """
    from omnigent.runtime import pending_elicitations, session_stream

    monkeypatch.setattr(
        sessions_route,
        "_CLAUDE_NATIVE_PERMISSION_HOOK_TIMEOUT_S",
        0.1,
    )
    # Timeout defers the index clear by the re-park grace; shrink it
    # so the test observes the clear quickly.
    monkeypatch.setattr(
        sessions_route,
        "_HARNESS_ELICITATION_REPARK_GRACE_S",
        0.05,
    )
    pending_elicitations.reset_for_tests()
    agent = await create_test_agent(client, "test-permission-timeout-index")
    session_id = await _create_session(client, agent["id"])
    payload = await _claude_permission_payload()

    async def _drain_until_resolved() -> None:
        """
        Wait for the deferred ``response.elicitation_resolved`` publish.

        :returns: None.
        """
        async with asyncio.timeout(3.0):
            async for event in session_stream.subscribe(session_id):
                if event.get("type") == "response.elicitation_resolved":
                    return

    drain_task = asyncio.create_task(_drain_until_resolved())
    # Give the subscriber a moment to register before the publisher
    # fires (publish is broadcast-to-current-subscribers).
    await asyncio.sleep(0.05)

    resp = await client.post(
        f"/v1/sessions/{session_id}/hooks/permission-request",
        json=payload,
    )
    # Sanity: the hook still returns the fail-ask shape (200 empty
    # body) so Claude proceeds to its TUI prompt. If this changes,
    # something else broke and the index assertion below is
    # secondary.
    assert resp.status_code == 200, resp.text
    assert resp.content == b"", f"expected empty body on timeout, got {resp.content!r}"
    # The deferred clear fires only after the re-park grace; waiting
    # for the resolved event (not sleeping) keeps this event-driven.
    await drain_task
    # 0 = the deferred clear decremented the index even though no UI
    # verdict arrived. If > 0, the sidebar would show a stuck badge
    # for every claude-native session whose owner answered in the
    # TUI instead of the web UI.
    assert pending_elicitations.count_for(session_id) == 0, (
        f"index should clear after the hook timeout grace, got "
        f"{pending_elicitations.count_for(session_id)!r}. The bug "
        f"is in the hook wait's deferred-clear path: it must publish "
        f"response.elicitation_resolved when no re-park arrives."
    )
    pending_elicitations.reset_for_tests()


async def test_pre_resolved_elicitation_tombstone_expires_before_hook_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Expired pre-resolved tombstones are not consumed by later hooks.

    ``external_elicitation_resolved`` can race ahead of the Codex hook
    registration, but if the hook never arrives the tombstone must not
    live forever or resolve a much later prompt that reuses the same
    deterministic id.
    """
    now = sessions_route.time.time()
    monkeypatch.setattr(
        sessions_route,
        "_HARNESS_PRE_RESOLVED_ELICITATION_TTL_S",
        10.0,
    )
    _harness_pre_resolved_elicitations.clear()
    try:
        _harness_pre_resolved_elicitations["elicit_expired"] = _PreResolvedHarnessElicitation(
            session_id="conv_123",
            created_at=now - 11.0,
        )

        assert not sessions_route._consume_pre_resolved_harness_elicitation(
            "conv_123",
            "elicit_expired",
        )
        assert _harness_pre_resolved_elicitations == {}
    finally:
        _harness_pre_resolved_elicitations.clear()


async def test_pre_resolved_elicitation_tombstones_prune_expired_and_excess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Pre-resolved tombstones are bounded even when no hook arrives.

    The old map accepted arbitrary ``external_elicitation_resolved``
    ids for valid sessions and never reaped them. Inserting a new
    tombstone now prunes expired entries and drops the oldest live
    entries beyond the configured cap.
    """
    now = sessions_route.time.time()
    monkeypatch.setattr(
        sessions_route,
        "_HARNESS_PRE_RESOLVED_ELICITATION_TTL_S",
        10.0,
    )
    monkeypatch.setattr(
        sessions_route,
        "_HARNESS_PRE_RESOLVED_ELICITATION_MAX_ENTRIES",
        2,
    )
    _harness_pre_resolved_elicitations.clear()
    try:
        _harness_pre_resolved_elicitations.update(
            {
                "elicit_expired": _PreResolvedHarnessElicitation(
                    session_id="conv_123",
                    created_at=now - 11.0,
                ),
                "elicit_oldest_live": _PreResolvedHarnessElicitation(
                    session_id="conv_123",
                    created_at=now - 3.0,
                ),
                "elicit_newer_live": _PreResolvedHarnessElicitation(
                    session_id="conv_123",
                    created_at=now - 2.0,
                ),
            }
        )

        sessions_route._signal_harness_elicitation_resolved_by_id(
            "conv_123",
            "elicit_newest_live",
        )

        assert set(_harness_pre_resolved_elicitations) == {
            "elicit_newer_live",
            "elicit_newest_live",
        }
    finally:
        _harness_pre_resolved_elicitations.clear()


async def test_permission_request_hook_clears_index_on_client_disconnect(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When the upstream client (Claude) closes its HTTP connection
    mid-park, the hook returns the fail-ask shape promptly — without
    waiting the full timeout — and the elicitation drops from the
    pending index after the re-park grace elapses with no retry.

    A severed connection is ambiguous (TUI answered vs proxy cut an
    idle long-poll), so the clear waits out the grace instead of
    wiping a card the hook's retry is about to re-park.

    The disconnect is simulated by patching the polling helper rather
    than wiring a real TCP teardown through the in-process ASGI
    transport — deterministic, no transport-layer timing dependence.
    """
    from omnigent.runtime import pending_elicitations, session_stream

    async def _disconnect_immediately(_request: Any) -> None:
        # One short yield so the future-watcher in the asyncio.wait
        # set sees the disconnect task complete after the hook has
        # published the SSE event — same ordering the real socket
        # close would produce.
        await asyncio.sleep(0.01)

    monkeypatch.setattr(
        sessions_route,
        "_poll_request_disconnect",
        _disconnect_immediately,
    )
    # Pin the timeout high so the test fails fast if the disconnect
    # race regresses — without the patch the hook would park here
    # for 300s rather than returning on the disconnect signal.
    monkeypatch.setattr(
        sessions_route,
        "_CLAUDE_NATIVE_PERMISSION_HOOK_TIMEOUT_S",
        30.0,
    )
    # Wide enough that the count==1 assertion can't race the deferred
    # clear (only a sub-ms in-process round-trip happens in between).
    monkeypatch.setattr(
        sessions_route,
        "_HARNESS_ELICITATION_REPARK_GRACE_S",
        1.0,
    )
    pending_elicitations.reset_for_tests()
    agent = await create_test_agent(client, "test-permission-disconnect")
    session_id = await _create_session(client, agent["id"])
    payload = await _claude_permission_payload()

    async def _drain_until_resolved() -> None:
        """
        Wait for the deferred ``response.elicitation_resolved`` publish.

        :returns: None.
        """
        async with asyncio.timeout(3.0):
            async for event in session_stream.subscribe(session_id):
                if event.get("type") == "response.elicitation_resolved":
                    return

    drain_task = asyncio.create_task(_drain_until_resolved())
    await asyncio.sleep(0.05)

    resp = await client.post(
        f"/v1/sessions/{session_id}/hooks/permission-request",
        json=payload,
    )
    # Fail-ask shape: 200 with empty body so Claude falls through to
    # its TUI prompt. Asserting both fields because returning 200 +
    # decision JSON would tell Claude to honor a verdict that never
    # arrived.
    assert resp.status_code == 200, resp.text
    assert resp.content == b"", f"expected empty body on disconnect, got {resp.content!r}"
    # 1 = the disconnect did NOT clear the index synchronously — the
    # prompt stays visible through the grace so a hook retry can
    # re-park it. 0 here would mean the deferred clear regressed to
    # the old immediate wipe (blocked sub-agents go invisible again).
    assert pending_elicitations.count_for(session_id) == 1, (
        f"index should survive the disconnect until the grace elapses, "
        f"got {pending_elicitations.count_for(session_id)!r}"
    )
    # After the grace passes with no re-park, the deferred clear must
    # fire so badges don't stick when the hook died for real.
    await drain_task
    assert pending_elicitations.count_for(session_id) == 0, (
        f"index should clear after the un-re-parked grace, got "
        f"{pending_elicitations.count_for(session_id)!r}"
    )
    pending_elicitations.reset_for_tests()


_REATTACH_ELICITATION_ID = f"elicit_claude_{'ab' * 16}"


async def test_permission_hook_repark_within_grace_keeps_card_pending(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A hook retry that re-parks the same ``_omnigent_elicitation_id``
    within the grace keeps the approval card pending and receives the
    eventual web verdict.

    This is the proxy-cut path that left headless sub-agents invisibly
    blocked: the old code cleared the card the moment the long-poll was
    severed, while the prompt lived on in an unattended tmux pane.
    """
    from omnigent.runtime import pending_elicitations, session_stream

    disconnect_calls = 0

    async def _disconnect_first_call_only(_request: Any) -> None:
        """
        Sever the first hook long-poll; park every later one.

        :param _request: Ignored FastAPI request.
        :returns: None when simulating the first call's disconnect.
        """
        nonlocal disconnect_calls
        disconnect_calls += 1
        if disconnect_calls == 1:
            # Yield once so the disconnect lands after the hook
            # published its SSE event, like a real socket close.
            await asyncio.sleep(0.01)
            return
        # Later calls (the re-park) stay connected; the asyncio.wait
        # race cancels this when a verdict arrives.
        await asyncio.Event().wait()

    monkeypatch.setattr(
        sessions_route,
        "_poll_request_disconnect",
        _disconnect_first_call_only,
    )
    # Wide enough that the re-park always lands before the grace expires.
    monkeypatch.setattr(
        sessions_route,
        "_HARNESS_ELICITATION_REPARK_GRACE_S",
        1.0,
    )
    pending_elicitations.reset_for_tests()
    agent = await create_test_agent(client, "test-permission-repark")
    session_id = await _create_session(client, agent["id"])
    payload = {
        **(await _claude_permission_payload()),
        "_omnigent_elicitation_id": _REATTACH_ELICITATION_ID,
    }

    resolved_events: list[dict[str, Any]] = []

    async def _capture_resolved() -> None:
        """
        Record every resolved event for this session until cancelled.

        :returns: None.
        """
        async for event in session_stream.subscribe(session_id):
            if event.get("type") == "response.elicitation_resolved":
                resolved_events.append(event)

    capture_task = asyncio.create_task(_capture_resolved())
    await asyncio.sleep(0.05)

    # First long-poll: severed by the disconnect fake → fail-ask.
    first = await client.post(
        f"/v1/sessions/{session_id}/hooks/permission-request",
        json=payload,
    )
    assert first.status_code == 200, first.text
    assert first.content == b"", f"severed poll should fail-ask, got {first.content!r}"
    # Grab the severed wait's deferred clear so we can await it, not sleep.
    deferred_tasks = set(sessions_route._deferred_elicitation_clear_tasks)
    assert len(deferred_tasks) == 1, (
        f"expected one deferred clear after the severed poll, got "
        f"{len(deferred_tasks)} — disconnect regressed to immediate wipe, or leaked tasks."
    )

    # Retry re-parks the SAME id while the grace is still running.
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/permission-request",
            json=payload,
        )
    )
    # The re-park republishes the request; wait for it so the verdict
    # below cannot race the registration.
    event = await _drain_until_elicitation(session_id)
    assert event["elicitation_id"] == _REATTACH_ELICITATION_ID

    # Run the deferred clear to completion: it must see the re-park and no-op.
    for task in deferred_tasks:
        await asyncio.wait_for(task, timeout=5.0)
    # 1 = the card survived the severed long-poll because the retry
    # re-parked inside the grace. 0 = the deferred clear ignored the
    # re-park and wiped the live prompt (the production bug).
    assert pending_elicitations.count_for(session_id) == 1, (
        f"re-parked elicitation should stay pending, got "
        f"{pending_elicitations.count_for(session_id)!r}"
    )
    assert resolved_events == [], (
        f"no resolved event may be published while the prompt is "
        f"re-parked, got {resolved_events!r}"
    )

    verdict = await _post_approval(client, session_id, _REATTACH_ELICITATION_ID, "accept")
    assert verdict.status_code == 202, verdict.text
    resp = await hook_task
    assert resp.status_code == 200, resp.text
    # The retried poll (not the severed one) carries the verdict.
    assert resp.json()["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    capture_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await capture_task
    pending_elicitations.reset_for_tests()


async def test_permission_hook_verdict_during_gap_honored_on_repark(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A web verdict that lands between a severed long-poll and its retry
    is handed to the retry via the pre-resolved tombstone.

    Without it the retry would re-publish and fail-ask later — the
    user's click silently dropped, the sub-agent still blocked.
    """
    from omnigent.runtime import pending_elicitations

    async def _disconnect_immediately(_request: Any) -> None:
        """
        Sever every hook long-poll straight away.

        :param _request: Ignored FastAPI request.
        :returns: None.
        """
        await asyncio.sleep(0.01)

    monkeypatch.setattr(
        sessions_route,
        "_poll_request_disconnect",
        _disconnect_immediately,
    )
    monkeypatch.setattr(
        sessions_route,
        "_HARNESS_ELICITATION_REPARK_GRACE_S",
        0.25,
    )
    pending_elicitations.reset_for_tests()
    agent = await create_test_agent(client, "test-permission-gap-verdict")
    session_id = await _create_session(client, agent["id"])
    payload = {
        **(await _claude_permission_payload()),
        "_omnigent_elicitation_id": _REATTACH_ELICITATION_ID,
    }

    first = await client.post(
        f"/v1/sessions/{session_id}/hooks/permission-request",
        json=payload,
    )
    assert first.status_code == 200, first.text
    assert first.content == b""

    # Verdict arrives while NO wait is parked (the gap).
    verdict = await _post_approval(client, session_id, _REATTACH_ELICITATION_ID, "accept")
    assert verdict.status_code == 202, verdict.text
    # The approval cleared the index even though nothing was parked.
    assert pending_elicitations.count_for(session_id) == 0

    # The retry consumes the tombstone and returns the verdict without
    # re-publishing the prompt (it would otherwise park again and the
    # disconnect fake would fail-ask it).
    second = await client.post(
        f"/v1/sessions/{session_id}/hooks/permission-request",
        json=payload,
    )
    assert second.status_code == 200, second.text
    body = second.json()
    # "allow" proves the gap verdict was honored; an empty body here
    # would mean the tombstone was dropped and the click lost.
    assert body["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    # Drain the severed poll's deferred clear so it doesn't outlive the
    # test's event loop (it no-ops the index either way).
    for task in set(sessions_route._deferred_elicitation_clear_tasks):
        await asyncio.wait_for(task, timeout=5.0)
    pending_elicitations.reset_for_tests()


async def test_permission_hook_rejects_malformed_reattach_id(
    client: httpx.AsyncClient,
) -> None:
    """
    A client-supplied ``_omnigent_elicitation_id`` outside the
    claude-hook namespace is rejected at the route boundary.

    The id is client-controlled; without the format gate a client
    could squat on Codex deterministic ids or server-minted ids and
    interfere with another prompt's lifecycle.
    """
    agent = await create_test_agent(client, "test-permission-bad-id")
    session_id = await _create_session(client, agent["id"])
    payload = {
        **(await _claude_permission_payload()),
        "_omnigent_elicitation_id": "elicit_codex_squat",
    }

    resp = await client.post(
        f"/v1/sessions/{session_id}/hooks/permission-request",
        json=payload,
    )
    assert resp.status_code == 400, resp.text
    assert "_omnigent_elicitation_id" in resp.text


async def test_permission_hook_rejects_reattach_id_owned_by_other_session(
    client: httpx.AsyncClient,
) -> None:
    """
    A re-attach id currently parked by another session is rejected.

    Cross-session guard: session B must not be able to adopt session
    A's live elicitation id — doing so would overwrite A's owner
    registration and let B's approval flow resolve A's prompt.
    """
    agent = await create_test_agent(client, "test-permission-id-collision")
    session_a = await _create_session(client, agent["id"])
    session_b = await _create_session(client, agent["id"])
    payload = {
        **(await _claude_permission_payload()),
        "_omnigent_elicitation_id": _REATTACH_ELICITATION_ID,
    }

    drain_task = asyncio.create_task(_drain_until_elicitation(session_a))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_a}/hooks/permission-request",
            json=payload,
        )
    )
    event = await drain_task
    assert event["elicitation_id"] == _REATTACH_ELICITATION_ID

    # Session B tries to park the id session A currently owns.
    resp = await client.post(
        f"/v1/sessions/{session_b}/hooks/permission-request",
        json=payload,
    )
    assert resp.status_code == 400, resp.text
    assert "different session" in resp.text

    # Unwind session A's parked hook so no task leaks past the test.
    verdict = await _post_approval(client, session_a, _REATTACH_ELICITATION_ID, "decline")
    assert verdict.status_code == 202, verdict.text
    resp_a = await hook_task
    assert resp_a.json()["hookSpecificOutput"]["decision"]["behavior"] == "deny"


async def test_codex_mcp_elicitation_hook_accept_round_trip(
    client: httpx.AsyncClient,
) -> None:
    """
    Codex ``mcpServer/elicitation/request`` frames are published to the
    web UI and the accepted form content is returned in Codex's
    app-server result shape.

    This is the native-Codex equivalent of the Claude permission hook
    allow path: a forwarded app-server request parks on the shared
    elicitation registry, the web ``approval`` event resolves it, and
    the hook response is exactly what the forwarder sends back over
    JSON-RPC.
    """
    agent = await create_test_agent(client, "test-codex-mcp-elicit")
    session_id = await _create_session(client, agent["id"])
    payload = {
        "id": 7,
        "method": "mcpServer/elicitation/request",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "serverName": "booking",
            "mode": "form",
            "message": "Pick a date",
            "requestedSchema": {
                "type": "object",
                "properties": {"date": {"type": "string"}},
            },
        },
    }

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/codex-elicitation-request",
            json=payload,
        )
    )

    event = await drain_task
    params = event["params"]
    assert params["message"] == "Pick a date"
    assert params["requestedSchema"] == payload["params"]["requestedSchema"]
    assert params["server_name"] == "booking"
    assert params["codex_method"] == "mcpServer/elicitation/request"
    assert params["codex_request_id"] == 7

    verdict = await _post_approval(
        client,
        session_id,
        event["elicitation_id"],
        "accept",
        content={"date": "tomorrow"},
    )
    assert verdict.status_code == 202, verdict.text

    resp = await hook_task
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "action": "accept",
        "content": {"date": "tomorrow"},
        "_meta": None,
    }


async def test_codex_pending_elicitation_survives_session_snapshot_refresh(
    client: httpx.AsyncClient,
) -> None:
    """
    A parked Codex app-server elicitation is replayed by session snapshot.

    This exercises the real hook path instead of seeding
    ``pending_elicitations`` directly: the Codex request parks in the
    shared harness elicitation registry, publishes
    ``response.elicitation_request``, and ``GET /v1/sessions/{id}``
    must include that exact event in ``pending_elicitations`` so a
    browser refresh can reconstruct the approval card.

    :param client: Test HTTP client.
    :returns: None.
    """
    from omnigent.runtime import pending_elicitations

    pending_elicitations.reset_for_tests()
    agent = await create_test_agent(client, "test-codex-pending-refresh")
    session_id = await _create_session(client, agent["id"])
    payload = {
        "id": 17,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "itemId": "item_cmd",
            "startedAtMs": 1,
            "approvalId": None,
            "reason": "test command approval",
            "command": "date",
            "cwd": "/tmp/workspace",
            "commandActions": [],
        },
    }

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/codex-elicitation-request",
            json=payload,
        )
    )

    event = await drain_task
    elicitation_id = event["elicitation_id"]
    assert pending_elicitations.count_for(session_id) == 1

    snapshot_resp = await client.get(f"/v1/sessions/{session_id}")
    assert snapshot_resp.status_code == 200, snapshot_resp.text
    pending_payloads = snapshot_resp.json()["pending_elicitations"]
    assert len(pending_payloads) == 1
    replay = pending_payloads[0]
    assert replay["type"] == "response.elicitation_request"
    assert replay["elicitation_id"] == elicitation_id
    assert replay["params"]["command"] == "date"
    assert replay["params"]["cwd"] == "/tmp/workspace"

    verdict = await _post_approval(client, session_id, elicitation_id, "accept")
    assert verdict.status_code == 202, verdict.text
    resp = await hook_task
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"decision": "accept"}
    pending_elicitations.reset_for_tests()


async def test_codex_elicitation_resolved_event_clears_hook_wait(
    client: httpx.AsyncClient,
) -> None:
    """
    Codex ``serverRequest/resolved`` propagation clears the web prompt.

    The Codex-native forwarder posts ``external_elicitation_resolved``
    when another Codex client answers the JSON-RPC request first. The
    server must resolve the parked hook wait and publish the standard
    ``response.elicitation_resolved`` SSE event for the deterministic
    Omnigent elicitation id.
    """
    agent = await create_test_agent(client, "test-codex-resolved-event")
    session_id = await _create_session(client, agent["id"])
    payload = {
        "id": 7,
        "method": "mcpServer/elicitation/request",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "serverName": "booking",
            "mode": "form",
            "message": "Pick a date",
            "requestedSchema": {
                "type": "object",
                "properties": {"date": {"type": "string"}},
            },
        },
    }
    expected_elicitation_id = codex_elicitation_id(
        session_id,
        "mcpServer/elicitation/request",
        7,
    )
    request_seen = asyncio.Event()
    captured: list[dict[str, Any]] = []

    async def _capture() -> None:
        """
        Capture the request and resolved stream events for this hook.

        :returns: None after the resolved event is observed.
        """
        async with asyncio.timeout(5.0):
            async for event in session_stream.subscribe(session_id):
                if event.get("type") in {
                    "response.elicitation_request",
                    "response.elicitation_resolved",
                }:
                    captured.append(event)
                if event.get("type") == "response.elicitation_request":
                    request_seen.set()
                if event.get("type") == "response.elicitation_resolved":
                    return

    capture_task = asyncio.create_task(_capture())
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/codex-elicitation-request",
            json=payload,
        )
    )
    await asyncio.wait_for(request_seen.wait(), timeout=5.0)

    request_event = next(e for e in captured if e.get("type") == "response.elicitation_request")
    assert request_event["elicitation_id"] == expected_elicitation_id

    resolved_resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_elicitation_resolved",
            "data": {"elicitation_id": expected_elicitation_id},
        },
    )
    assert resolved_resp.status_code == 202, resolved_resp.text

    hook_resp = await hook_task
    assert hook_resp.status_code == 200, hook_resp.text
    assert hook_resp.text == ""
    await capture_task

    resolved = [e for e in captured if e.get("type") == "response.elicitation_resolved"]
    assert len(resolved) == 1
    assert resolved[0]["elicitation_id"] == expected_elicitation_id


async def test_codex_request_user_input_hook_returns_id_keyed_answers(
    client: httpx.AsyncClient,
) -> None:
    """
    Codex ``item/tool/requestUserInput`` frames use the shared
    AskUserQuestion form payload, but return answers keyed by Codex's
    stable question id rather than by display text.

    Without this id preservation the web UI can render the question,
    but the app-server receives an empty ``answers`` object and Codex
    keeps waiting for input.
    """
    agent = await create_test_agent(client, "test-codex-request-user-input")
    session_id = await _create_session(client, agent["id"])
    payload = {
        "id": "req_9",
        "method": "item/tool/requestUserInput",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "itemId": "item_123",
            "questions": [
                {
                    "id": "framework",
                    "header": "Framework",
                    "question": "Which framework?",
                    "options": [{"label": "React", "description": "JS UI"}],
                    "isOther": False,
                    "isSecret": False,
                }
            ],
        },
    }

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/codex-elicitation-request",
            json=payload,
        )
    )

    event = await drain_task
    ask_payload = event["params"]["ask_user_question"]
    assert ask_payload["questions"][0]["id"] == "framework"
    assert ask_payload["questions"][0]["question"] == "Which framework?"
    assert ask_payload["questions"][0]["options"] == [{"label": "React", "description": "JS UI"}]

    verdict = await _post_approval(
        client,
        session_id,
        event["elicitation_id"],
        "accept",
        content={"framework": "React"},
    )
    assert verdict.status_code == 202, verdict.text

    resp = await hook_task
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"answers": {"framework": {"answers": ["React"]}}}


async def test_codex_plan_mode_final_prompt_round_trip(
    client: httpx.AsyncClient,
) -> None:
    """
    Codex plan mode's final "Implement this plan?" prompt is surfaced
    as a structured web approval form and returns the selected option
    under the stable question id.
    """
    agent = await create_test_agent(client, "test-codex-plan-final-prompt")
    session_id = await _create_session(client, agent["id"])
    payload = {
        "id": "plan_prompt",
        "method": "item/tool/requestUserInput",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "itemId": "item_plan_prompt",
            "questions": [
                {
                    "id": "plan_decision",
                    "header": "Plan",
                    "question": "Implement this plan?",
                    "options": [
                        {
                            "label": "Yes, implement this plan",
                            "description": "Switch to Default and start coding.",
                        },
                        {
                            "label": "Yes, clear context and implement",
                            "description": "Fresh thread. Context: 8% used.",
                        },
                        {
                            "label": "No, stay in Plan mode",
                            "description": "Continue planning with the model.",
                        },
                    ],
                    "isOther": False,
                    "isSecret": False,
                }
            ],
        },
    }

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/codex-elicitation-request",
            json=payload,
        )
    )

    event = await drain_task
    params = event["params"]
    assert params["message"] == "Codex needs input"
    question = params["ask_user_question"]["questions"][0]
    assert question["id"] == "plan_decision"
    assert question["question"] == "Implement this plan?"
    assert [option["label"] for option in question["options"]] == [
        "Yes, implement this plan",
        "Yes, clear context and implement",
        "No, stay in Plan mode",
    ]

    verdict = await _post_approval(
        client,
        session_id,
        event["elicitation_id"],
        "accept",
        content={"plan_decision": "Yes, implement this plan"},
    )
    assert verdict.status_code == 202, verdict.text

    resp = await hook_task
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"answers": {"plan_decision": {"answers": ["Yes, implement this plan"]}}}


async def test_codex_command_approval_hook_accept_round_trip(
    client: httpx.AsyncClient,
) -> None:
    """
    Codex command approval requests are published as binary web
    approvals and accepted web verdicts return Codex's command
    approval response shape.
    """
    agent = await create_test_agent(client, "test-codex-command-approval")
    session_id = await _create_session(client, agent["id"])
    payload = {
        "id": 13,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "itemId": "item_cmd",
            "startedAtMs": 1,
            "approvalId": None,
            "reason": "test command approval",
            "command": "date",
            "cwd": "/tmp/workspace",
            "commandActions": [],
        },
    }

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/codex-elicitation-request",
            json=payload,
        )
    )

    event = await drain_task
    params = event["params"]
    assert params["message"] == "Codex wants to run **date**"
    assert params["command"] == "date"
    assert params["cwd"] == "/tmp/workspace"
    assert params["reason"] == "test command approval"
    assert params["codex_method"] == "item/commandExecution/requestApproval"

    verdict = await _post_approval(
        client,
        session_id,
        event["elicitation_id"],
        "accept",
    )
    assert verdict.status_code == 202, verdict.text

    resp = await hook_task
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"decision": "accept"}


async def test_codex_command_approval_hook_accepts_execpolicy_amendment(
    client: httpx.AsyncClient,
) -> None:
    """
    Codex command approval requests that offer an execpolicy
    amendment can be accepted with that exact amendment, returning
    Codex's structured ``acceptWithExecpolicyAmendment`` decision.
    """
    agent = await create_test_agent(client, "test-codex-execpolicy-approval")
    session_id = await _create_session(client, agent["id"])
    amendment = [".venv/bin/python", "-m", "pytest"]
    payload = {
        "id": 131,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "itemId": "item_cmd",
            "startedAtMs": 1,
            "approvalId": None,
            "reason": "test command approval",
            "command": ".venv/bin/python -m pytest tests/test_codex_native.py -q",
            "cwd": "/tmp/workspace",
            "commandActions": [],
            "availableDecisions": [
                "accept",
                {
                    "acceptWithExecpolicyAmendment": {
                        "execpolicy_amendment": amendment,
                    },
                },
                "cancel",
            ],
        },
    }

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/codex-elicitation-request",
            json=payload,
        )
    )

    event = await drain_task
    params = event["params"]
    assert params["execpolicy_amendment"] == amendment

    verdict = await _post_approval(
        client,
        session_id,
        event["elicitation_id"],
        "accept",
        content={"execpolicy_amendment": amendment},
    )
    assert verdict.status_code == 202, verdict.text

    resp = await hook_task
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "decision": {
            "acceptWithExecpolicyAmendment": {
                "execpolicy_amendment": amendment,
            },
        },
    }


async def test_codex_command_approval_hook_rejects_malformed_execpolicy_amendment(
    client: httpx.AsyncClient,
) -> None:
    """
    Malformed remember approvals fail instead of degrading to a plain
    command accept.
    """
    agent = await create_test_agent(client, "test-codex-execpolicy-invalid")
    session_id = await _create_session(client, agent["id"])
    amendment = [".venv/bin/python", "-m", "pytest"]
    payload = {
        "id": 132,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "itemId": "item_cmd",
            "startedAtMs": 1,
            "approvalId": None,
            "reason": "test command approval",
            "command": ".venv/bin/python -m pytest tests/test_codex_native.py -q",
            "cwd": "/tmp/workspace",
            "commandActions": [],
            "availableDecisions": [
                "accept",
                {
                    "acceptWithExecpolicyAmendment": {
                        "execpolicy_amendment": amendment,
                    },
                },
                "cancel",
            ],
        },
    }

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/codex-elicitation-request",
            json=payload,
        )
    )

    event = await drain_task
    verdict = await _post_approval(
        client,
        session_id,
        event["elicitation_id"],
        "accept",
        content={"execpolicy_amendment": []},
    )
    assert verdict.status_code == 202, verdict.text

    resp = await hook_task
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_input"
    assert "execpolicy amendment" in resp.json()["error"]["message"]


async def test_codex_permissions_approval_hook_grants_requested_profile(
    client: httpx.AsyncClient,
) -> None:
    """
    Codex ``item/permissions/requestApproval`` frames are surfaced as
    web approvals and accepted verdicts return the requested permission
    profile in Codex's response shape.
    """
    agent = await create_test_agent(client, "test-codex-permissions-approval")
    session_id = await _create_session(client, agent["id"])
    payload = {
        "id": 14,
        "method": "item/permissions/requestApproval",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "itemId": "item_permissions",
            "startedAtMs": 1,
            "cwd": "/tmp/workspace",
            "reason": "need network",
            "permissions": {
                "network": {"enabled": True},
                "fileSystem": None,
            },
        },
    }

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/codex-elicitation-request",
            json=payload,
        )
    )

    event = await drain_task
    params = event["params"]
    assert params["message"] == "Codex requests additional permissions"
    assert params["cwd"] == "/tmp/workspace"
    assert params["reason"] == "need network"
    assert params["permissions"] == payload["params"]["permissions"]
    assert params["codex_method"] == "item/permissions/requestApproval"

    verdict = await _post_approval(
        client,
        session_id,
        event["elicitation_id"],
        "accept",
    )
    assert verdict.status_code == 202, verdict.text

    resp = await hook_task
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "permissions": {"network": {"enabled": True}},
        "scope": "turn",
    }


async def test_codex_legacy_exec_command_approval_uses_legacy_decision_literals(
    client: httpx.AsyncClient,
) -> None:
    """
    Older Codex ``execCommandApproval`` requests expect
    ``approved``/``denied`` review decisions rather than v2
    ``accept``/``decline`` literals.
    """
    agent = await create_test_agent(client, "test-codex-legacy-command-approval")
    session_id = await _create_session(client, agent["id"])
    payload = {
        "id": "legacy_1",
        "method": "execCommandApproval",
        "params": {
            "conversationId": "thread_123",
            "callId": "call_123",
            "approvalId": None,
            "command": ["date"],
            "cwd": "/tmp/workspace",
            "reason": "test legacy approval",
            "parsedCmd": [],
        },
    }

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/codex-elicitation-request",
            json=payload,
        )
    )

    event = await drain_task
    params = event["params"]
    assert params["command"] == "date"
    assert params["call_id"] == "call_123"

    verdict = await _post_approval(
        client,
        session_id,
        event["elicitation_id"],
        "decline",
    )
    assert verdict.status_code == 202, verdict.text

    resp = await hook_task
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"decision": "denied"}


async def test_codex_elicitation_hook_timeout_clears_pending_index(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Codex-native elicitation timeout uses the same cleanup path as the
    Claude hook: return an empty body and decrement the pending
    elicitation index once the re-park grace elapses with no retry.
    """
    from omnigent.runtime import pending_elicitations, session_stream

    monkeypatch.setattr(
        sessions_route,
        "_CODEX_NATIVE_ELICITATION_HOOK_TIMEOUT_S",
        0.1,
    )
    monkeypatch.setattr(
        sessions_route,
        "_HARNESS_ELICITATION_REPARK_GRACE_S",
        0.05,
    )
    pending_elicitations.reset_for_tests()
    agent = await create_test_agent(client, "test-codex-elicit-timeout")
    session_id = await _create_session(client, agent["id"])

    async def _drain_until_resolved() -> None:
        """
        Wait for the deferred ``response.elicitation_resolved`` publish.

        :returns: None.
        """
        async with asyncio.timeout(3.0):
            async for event in session_stream.subscribe(session_id):
                if event.get("type") == "response.elicitation_resolved":
                    return

    drain_task = asyncio.create_task(_drain_until_resolved())
    await asyncio.sleep(0.05)

    resp = await client.post(
        f"/v1/sessions/{session_id}/hooks/codex-elicitation-request",
        json={
            "id": 12,
            "method": "mcpServer/elicitation/request",
            "params": {
                "mode": "form",
                "message": "Pick a value",
                "requestedSchema": {"type": "object", "properties": {}},
            },
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.content == b""
    # Deferred clear — the index drops only after the grace passes
    # with no re-park, so wait for the resolved event first.
    await drain_task
    assert pending_elicitations.count_for(session_id) == 0
    pending_elicitations.reset_for_tests()


async def test_permission_request_hook_validates_tool_name(
    client: httpx.AsyncClient,
) -> None:
    """
    A request without ``tool_name`` is rejected at the route boundary
    with 400, not parked silently on the elicitation registry.

    Without this guard, a malformed payload from Claude Code would
    hang the hook for the full timeout and the UI would render a
    ``response.elicitation_request`` with an empty preview — neither
    useful nor diagnosable.
    """
    agent = await create_test_agent(client, "test-permission-validate")
    session_id = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session_id}/hooks/permission-request",
        json={
            "hook_event_name": "PermissionRequest",
            "tool_input": {"command": "ls"},
        },
    )
    assert resp.status_code == 400, resp.text
    assert "tool_name" in resp.text


async def test_permission_hook_finally_emits_elicitation_resolved_on_timeout(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    On hook timeout, the ``finally`` block publishes a
    ``response.elicitation_resolved`` SSE event (in addition to
    clearing the cross-session index). Multi-tab web UI clients
    rely on the SSE event to flip their copy of the
    ``ApprovalCard`` — without it, every tab the user hasn't
    actively interacted with would hold the prompt as pending
    until the user refreshes.

    Asserts on the SSE event, not just the index, because the
    chat-store consumer is the visible artifact for the
    multi-tab path.
    """
    from omnigent.runtime import pending_elicitations, session_stream

    monkeypatch.setattr(
        sessions_route,
        "_CLAUDE_NATIVE_PERMISSION_HOOK_TIMEOUT_S",
        0.1,
    )
    # The resolved event is deferred by the re-park grace; shrink it
    # so the 3s capture window sees the publish.
    monkeypatch.setattr(
        sessions_route,
        "_HARNESS_ELICITATION_REPARK_GRACE_S",
        0.05,
    )
    pending_elicitations.reset_for_tests()
    agent = await create_test_agent(client, "test-permission-timeout-resolved-event")
    session_id = await _create_session(client, agent["id"])
    payload = await _claude_permission_payload()

    captured: list[dict[str, Any]] = []

    async def _capture() -> None:
        async with asyncio.timeout(3.0):
            async for event in session_stream.subscribe(session_id):
                captured.append(event)
                if event.get("type") == "response.elicitation_resolved":
                    return

    drain_task = asyncio.create_task(_capture())
    await asyncio.sleep(0.05)

    resp = await client.post(
        f"/v1/sessions/{session_id}/hooks/permission-request",
        json=payload,
    )
    assert resp.status_code == 200, resp.text
    await drain_task

    resolved = [e for e in captured if e.get("type") == "response.elicitation_resolved"]
    assert len(resolved) == 1, (
        f"expected one elicitation_resolved on timeout, got {resolved!r}. "
        f"The PermissionRequest hook's finally block must publish "
        f"response.elicitation_resolved so other tabs / TUI clients "
        f"learn the prompt is dead."
    )
    # And the original request matches the resolved one (single
    # round trip — the id from the request must be the same id
    # cleared at the end).
    requested = [e for e in captured if e.get("type") == "response.elicitation_request"]
    assert len(requested) == 1
    assert resolved[0]["elicitation_id"] == requested[0]["elicitation_id"]
    pending_elicitations.reset_for_tests()


async def test_approval_dispatch_publishes_elicitation_resolved(
    client: httpx.AsyncClient,
) -> None:
    """
    The happy-path approval dispatch on ``POST /events`` publishes
    a ``response.elicitation_resolved`` SSE event after resolving
    the harness future. Tabs other than the one that submitted
    the verdict rely on this signal to flip their card from
    pending to resolved — without it, the second tab would stay
    frozen on the original prompt until refresh.

    Triggers the harness-future branch by registering an
    elicitation through the real ``PermissionRequest`` hook, then
    delivering an ``approval`` verdict from the test side.
    """
    from omnigent.runtime import pending_elicitations, session_stream

    pending_elicitations.reset_for_tests()
    agent = await create_test_agent(client, "test-approval-dispatch-emits-resolved")
    session_id = await _create_session(client, agent["id"])
    payload = await _claude_permission_payload()

    # Capture both the request AND the resolved event in one
    # subscriber so we can verify ordering as well as presence.
    captured: list[dict[str, Any]] = []

    async def _capture() -> None:
        async with asyncio.timeout(5.0):
            async for event in session_stream.subscribe(session_id):
                captured.append(event)
                if event.get("type") == "response.elicitation_resolved":
                    return

    drain_task = asyncio.create_task(_capture())
    await asyncio.sleep(0.05)

    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/permission-request",
            json=payload,
        )
    )

    # Wait for the request event to surface so we know the id.
    elicit_event = await _drain_until_elicitation(session_id)
    elicitation_id = elicit_event["elicitation_id"]

    verdict = await _post_approval(client, session_id, elicitation_id, "accept")
    assert verdict.status_code == 202, verdict.text

    resp = await hook_task
    assert resp.status_code == 200, resp.text
    await drain_task

    resolved = [e for e in captured if e.get("type") == "response.elicitation_resolved"]
    # The dispatch path AND the hook's finally each publish — both
    # are idempotent on consumers. At least one must fire.
    assert len(resolved) >= 1, (
        f"expected approval dispatch to publish elicitation_resolved, "
        f"got events {[e.get('type') for e in captured]!r}"
    )
    assert resolved[0]["elicitation_id"] == elicitation_id
    pending_elicitations.reset_for_tests()


async def _subscribe_into(
    session_id: str,
    sink: list[dict[str, Any]],
    *,
    timeout_s: float = 10.0,
) -> None:
    """
    Append every SSE event for ``session_id`` into ``sink`` until the
    enclosing task is cancelled or ``timeout_s`` elapses.

    A single long-lived subscriber (vs. :func:`_drain_until_elicitation`,
    which returns on the first elicitation) lets a test observe the
    *absence* of a later event — here, that an unrelated tool result did
    NOT publish a ``response.elicitation_resolved`` for a pending prompt.

    :param session_id: Session to subscribe to.
    :param sink: List the subscriber appends each event dict into.
    :param timeout_s: Hard cap so a leaked subscriber can't hang pytest.
    """
    try:
        async with asyncio.timeout(timeout_s):
            async for event in session_stream.subscribe(session_id):
                sink.append(event)
    except asyncio.TimeoutError:
        return


async def _wait_for_event(
    sink: list[dict[str, Any]],
    predicate: Any,
    *,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    """
    Poll ``sink`` until an event matching ``predicate`` appears.

    Used with :func:`_subscribe_into` so the test reads ids out of the
    same subscriber that also watches for the absence of later events
    (a second `subscribe` could miss a publish that landed before it
    registered — publish is broadcast-to-current-subscribers).

    :param sink: Event list a subscriber task is appending into.
    :param predicate: ``Callable[[dict], bool]`` selecting the event.
    :param timeout_s: Fail the test if no match arrives in this window.
    :returns: The first matching event dict.
    """
    async with asyncio.timeout(timeout_s):
        while True:
            for event in sink:
                if predicate(event):
                    return event
            await asyncio.sleep(0.01)


async def _post_external_conversation_item(
    client: httpx.AsyncClient,
    session_id: str,
    item_type: str,
    item_data: dict[str, Any],
    *,
    source_id: str,
    response_id: str,
) -> httpx.Response:
    """
    Forward one claude-native transcript item through the public route.

    Mirrors what ``claude_native_forwarder`` POSTs for an observed
    ``function_call`` / ``function_call_output`` — the path that lands
    in :func:`_publish_external_conversation_item`.

    :param client: Test HTTP client.
    :param session_id: Target session.
    :param item_type: ``"function_call"`` or ``"function_call_output"``.
    :param item_data: The item body (sans ``type``), e.g.
        ``{"call_id": "...", "output": "..."}``.
    :param source_id: Producer-stable dedup key.
    :param response_id: Turn-scoped response id the item belongs to.
    :returns: The HTTP response from the session event route.
    """
    return await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": item_type,
                "item_data": item_data,
                "source_id": source_id,
                "response_id": response_id,
            },
        },
    )


async def test_non_gated_tool_output_does_not_resolve_pending_elicitation(
    client: httpx.AsyncClient,
) -> None:
    """
    An unrelated tool's ``function_call_output`` must NOT resolve a
    pending approval prompt for a different gated tool the user hasn't
    answered yet.

    Scenario (claude-native, the reported config):
    - Claude's ``PermissionRequest`` hook parks an approval for ``Bash``.
      The user has answered neither the TUI nor the web UI; the
      ``ApprovalCard`` is live and ``elicit_bash`` sits in the pending
      index.
    - Before that resolves, Claude runs an auto-allowed ``Read`` (no
      permission prompt). Its ``function_call`` + ``function_call_output``
      are forwarded as ``external_conversation_item`` POSTs.

    The old auto-resolve heuristic popped pending elicitations from
    forwarded tool observations. An unrelated ``Read`` output could
    silently resolve the ``Bash`` prompt: the web UI received
    ``response.elicitation_resolved`` and the card vanished, leaving
    "Claude is working" while the terminal stayed blocked.

    Asserts the ``Bash`` prompt survives the ``Read`` output: no
    ``elicitation_resolved`` for its id is published, and the pending
    index still counts it. Fails if forwarded tool observations start
    resolving pending permissions again.
    """
    from omnigent.runtime import pending_elicitations

    pending_elicitations.reset_for_tests()
    agent = await create_test_agent(client, "test-1594-non-gated")
    session_id = await _create_session(client, agent["id"])

    # One long-lived subscriber: it both yields the Bash elicitation id
    # and lets us assert the *absence* of a resolved event afterwards.
    captured: list[dict[str, Any]] = []
    capture_task = asyncio.create_task(_subscribe_into(session_id, captured))
    # Let the subscriber register before any publish (broadcast-only).
    await asyncio.sleep(0.05)

    # Park a gated Bash approval — nobody answers it.
    bash_payload = await _claude_permission_payload(tool_name="Bash")
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/permission-request",
            json=bash_payload,
        )
    )
    bash_event = await _wait_for_event(
        captured,
        lambda e: e.get("type") == "response.elicitation_request",
    )
    bash_elicit_id = bash_event["elicitation_id"]
    # Precondition: exactly the Bash prompt is outstanding.
    assert pending_elicitations.count_for(session_id) == 1, (
        f"expected the Bash prompt to be the only pending elicitation, "
        f"got count {pending_elicitations.count_for(session_id)!r}"
    )

    # Claude runs an auto-allowed Read (no PermissionRequest), forwarded
    # as its tool_use then tool_result. Different tool, different call id.
    read_call_id = "toolu_read_1594"
    fc = await _post_external_conversation_item(
        client,
        session_id,
        "function_call",
        {
            "agent": "Claude",
            "name": "Read",
            "arguments": json.dumps({"file_path": "/etc/hosts"}),
            "call_id": read_call_id,
        },
        source_id="src_read_fc_1594",
        response_id="resp_1594",
    )
    assert fc.status_code < 300, fc.text
    fco = await _post_external_conversation_item(
        client,
        session_id,
        "function_call_output",
        {"call_id": read_call_id, "output": "127.0.0.1 localhost\n"},
        source_id="src_read_fco_1594",
        response_id="resp_1594",
    )
    assert fco.status_code < 300, fco.text

    # Give the publish path time to wrongly resolve the prompt.
    await asyncio.sleep(0.2)

    # The bug: the unrelated Read output resolved the Bash prompt. If any
    # elicitation_resolved for the Bash id was published, the web UI's
    # ApprovalCard was cleared while the terminal is still blocked.
    resolved_for_bash = [
        e
        for e in captured
        if e.get("type") == "response.elicitation_resolved"
        and e.get("elicitation_id") == bash_elicit_id
    ]
    assert resolved_for_bash == [], (
        f"the auto-allowed Read tool's function_call_output "
        f"resolved the still-pending Bash approval (events: "
        f"{[e.get('type') for e in captured]!r}). Forwarded tool "
        f"observations must not pop pending permission prompts."
    )
    # Index unchanged: the sidebar/snapshot still show the Bash prompt.
    assert pending_elicitations.count_for(session_id) == 1, (
        f"expected the Bash prompt to survive the Read output, but the "
        f"index now counts {pending_elicitations.count_for(session_id)!r}"
    )

    # Cleanup: answer the Bash prompt so the parked hook returns, then
    # stop the subscriber so pytest doesn't wait out the timeout.
    verdict = await _post_approval(client, session_id, bash_elicit_id, "accept")
    assert verdict.status_code == 202, verdict.text
    await hook_task
    capture_task.cancel()
    pending_elicitations.reset_for_tests()


async def test_gated_tool_output_resolves_pending_elicitation(
    client: httpx.AsyncClient,
) -> None:
    """
    The gated tool's OWN ``function_call_output`` resolves its pending
    permission prompt promptly — the terminal-resolved fast path that
    fixes the reported "stuck for minutes" bug.

    Scenario (claude-native, the reported config): the
    ``PermissionRequest`` hook parks a ``Bash`` approval; the user
    answers in Claude's TUI, NOT the web UI, so the parked hook never
    gets a web verdict and ``request.is_disconnected()`` does not fire
    behind the Databricks Apps proxy. Claude runs the tool and the
    forwarder mirrors its ``function_call`` then ``function_call_output``.
    The output carries the SAME tool name + input as the parked prompt,
    so it must resolve the prompt now instead of leaving the badge up
    until the 300s hook timeout.

    Asserts: ``response.elicitation_resolved`` for the Bash prompt is
    published, the pending index drops to 0, and the parked hook POST
    returns ``200`` with an empty body (fail-ask — Claude already has
    its TUI answer). Positive counterpart to
    ``test_non_gated_tool_output_does_not_resolve_pending_elicitation``.
    """
    from omnigent.runtime import pending_elicitations

    pending_elicitations.reset_for_tests()
    agent = await create_test_agent(client, "test-terminal-resolved")
    session_id = await _create_session(client, agent["id"])

    captured: list[dict[str, Any]] = []
    capture_task = asyncio.create_task(_subscribe_into(session_id, captured))
    await asyncio.sleep(0.05)

    # Park a gated Bash approval — nobody answers it via the web UI.
    bash_payload = await _claude_permission_payload(tool_name="Bash")
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/permission-request",
            json=bash_payload,
        )
    )
    bash_event = await _wait_for_event(
        captured,
        lambda e: e.get("type") == "response.elicitation_request",
    )
    bash_elicit_id = bash_event["elicitation_id"]
    assert pending_elicitations.count_for(session_id) == 1, (
        f"expected the Bash prompt to be parked, got "
        f"{pending_elicitations.count_for(session_id)!r}"
    )

    # The user answered in the TUI; Claude runs the gated Bash and the
    # forwarder mirrors its tool_use then tool_result. Same tool name AND
    # input as the parked prompt → correlated to this prompt.
    bash_call_id = "toolu_bash_resolved"
    fc = await _post_external_conversation_item(
        client,
        session_id,
        "function_call",
        {
            "agent": "Claude",
            "name": "Bash",
            "arguments": json.dumps(bash_payload["tool_input"]),
            "call_id": bash_call_id,
        },
        source_id="src_bash_fc",
        response_id="resp_bash",
    )
    assert fc.status_code < 300, fc.text
    fco = await _post_external_conversation_item(
        client,
        session_id,
        "function_call_output",
        {"call_id": bash_call_id, "output": "total 0\n"},
        source_id="src_bash_fco",
        response_id="resp_bash",
    )
    assert fco.status_code < 300, fco.text

    # The mirrored result resolves the prompt: elicitation_resolved fires,
    # the parked hook POST returns 200 empty body (fail-ask), index → 0.
    resolved = await _wait_for_event(
        captured,
        lambda e: (
            e.get("type") == "response.elicitation_resolved"
            and e.get("elicitation_id") == bash_elicit_id
        ),
    )
    assert resolved["elicitation_id"] == bash_elicit_id
    hook_resp = await hook_task
    assert hook_resp.status_code == 200, hook_resp.text
    assert not hook_resp.content, (
        f"terminal-resolved prompt must return empty body (fail-ask), got {hook_resp.content!r}"
    )
    assert pending_elicitations.count_for(session_id) == 0, (
        f"expected the Bash prompt to clear off the mirrored result, but "
        f"the index still counts {pending_elicitations.count_for(session_id)!r}"
    )

    capture_task.cancel()
    pending_elicitations.reset_for_tests()


async def test_tool_output_resolves_only_the_matching_same_name_prompt(
    client: httpx.AsyncClient,
) -> None:
    """
    With two same-named prompts parked, a tool result clears only the
    one whose input matches — not the other, and not both.

    Guards the correlation's input disambiguation. Claude blocks
    per-prompt so two outstanding ``Bash`` approvals is rare, but a
    parallel gated batch can park two at once; a mirrored result for one
    specific command must resolve exactly that prompt. Without input
    matching the fast path would fall back to resolving an arbitrary
    same-named prompt — the kind of mis-resolution this guards against.
    """
    from omnigent.runtime import pending_elicitations

    pending_elicitations.reset_for_tests()
    agent = await create_test_agent(client, "test-terminal-resolved-match")
    session_id = await _create_session(client, agent["id"])

    captured: list[dict[str, Any]] = []
    capture_task = asyncio.create_task(_subscribe_into(session_id, captured))
    await asyncio.sleep(0.05)

    # Park two Bash prompts with distinct inputs.
    payload_a = {**await _claude_permission_payload("Bash"), "tool_input": {"command": "ls"}}
    payload_b = {**await _claude_permission_payload("Bash"), "tool_input": {"command": "rm x"}}
    hook_a = asyncio.create_task(
        client.post(f"/v1/sessions/{session_id}/hooks/permission-request", json=payload_a)
    )
    elicit_a = (
        await _wait_for_event(
            captured,
            lambda e: e.get("type") == "response.elicitation_request",
        )
    )["elicitation_id"]
    hook_b = asyncio.create_task(
        client.post(f"/v1/sessions/{session_id}/hooks/permission-request", json=payload_b)
    )
    elicit_b = (
        await _wait_for_event(
            captured,
            lambda e: (
                e.get("type") == "response.elicitation_request"
                and e.get("elicitation_id") != elicit_a
            ),
        )
    )["elicitation_id"]
    assert pending_elicitations.count_for(session_id) == 2

    # Mirror the result for command "rm x" (prompt B) only.
    call_id = "toolu_bash_b"
    await _post_external_conversation_item(
        client,
        session_id,
        "function_call",
        {
            "agent": "Claude",
            "name": "Bash",
            "arguments": json.dumps({"command": "rm x"}),
            "call_id": call_id,
        },
        source_id="src_fc_b",
        response_id="resp_b",
    )
    await _post_external_conversation_item(
        client,
        session_id,
        "function_call_output",
        {"call_id": call_id, "output": "done\n"},
        source_id="src_fco_b",
        response_id="resp_b",
    )

    # Prompt B resolves; prompt A stays live.
    await _wait_for_event(
        captured,
        lambda e: (
            e.get("type") == "response.elicitation_resolved"
            and e.get("elicitation_id") == elicit_b
        ),
    )
    # Give any wrong resolution of A a chance to surface before asserting.
    await asyncio.sleep(0.1)
    resolved_a = [
        e
        for e in captured
        if e.get("type") == "response.elicitation_resolved" and e.get("elicitation_id") == elicit_a
    ]
    assert resolved_a == [], "prompt A (command 'ls') must stay live"
    assert pending_elicitations.count_for(session_id) == 1
    assert (await hook_b).status_code == 200

    # Cleanup: resolve A via web verdict so its parked hook returns.
    verdict = await _post_approval(client, session_id, elicit_a, "accept")
    assert verdict.status_code == 202, verdict.text
    await hook_a
    capture_task.cancel()
    pending_elicitations.reset_for_tests()


async def test_resolved_prompt_output_does_not_clear_other_same_name_prompt(
    client: httpx.AsyncClient,
) -> None:
    """
    A mirrored result for an ALREADY-RESOLVED prompt must not clear a
    different, still-pending prompt of the same tool.

    Reproduces the reported "Resolved elsewhere" bug for a parallel
    same-tool batch (e.g. several ``Read`` prompts surfaced at once):

    - Two ``Bash`` prompts park with distinct inputs: A (``ls``) and
      B (``rm x``).
    - The user approves A in the web UI. Its hook returns ``allow`` and
      A leaves the parked set; B is still live and unanswered.
    - Claude runs A's command; its ``function_call`` /
      ``function_call_output`` mirror back. The result carries A's input
      (``ls``), which matches no still-parked prompt — A is already gone.

    The terminal-resolved fast path must NOT misattribute A's output to
    the lone remaining candidate B: B's input (``rm x``) is not A's
    (``ls``). Without the fix the ``len(candidates) == 1`` fallback flips
    B to "Resolved elsewhere", forcing the user to the native terminal to
    answer a prompt the web card silently abandoned. Companion to
    ``test_tool_output_resolves_only_the_matching_same_name_prompt`` (the
    exact-match case); this guards the no-match case.
    """
    from omnigent.runtime import pending_elicitations

    pending_elicitations.reset_for_tests()
    agent = await create_test_agent(client, "test-resolved-output-no-cross-clear")
    session_id = await _create_session(client, agent["id"])

    captured: list[dict[str, Any]] = []
    capture_task = asyncio.create_task(_subscribe_into(session_id, captured))
    await asyncio.sleep(0.05)

    # Park two Bash prompts with distinct inputs.
    payload_a = {**await _claude_permission_payload("Bash"), "tool_input": {"command": "ls"}}
    payload_b = {**await _claude_permission_payload("Bash"), "tool_input": {"command": "rm x"}}
    hook_a = asyncio.create_task(
        client.post(f"/v1/sessions/{session_id}/hooks/permission-request", json=payload_a)
    )
    elicit_a = (
        await _wait_for_event(
            captured,
            lambda e: e.get("type") == "response.elicitation_request",
        )
    )["elicitation_id"]
    hook_b = asyncio.create_task(
        client.post(f"/v1/sessions/{session_id}/hooks/permission-request", json=payload_b)
    )
    elicit_b = (
        await _wait_for_event(
            captured,
            lambda e: (
                e.get("type") == "response.elicitation_request"
                and e.get("elicitation_id") != elicit_a
            ),
        )
    )["elicitation_id"]
    assert pending_elicitations.count_for(session_id) == 2

    # The user approves A in the web UI; its hook returns allow and A
    # leaves the parked set. B stays live and unanswered.
    verdict_a = await _post_approval(client, session_id, elicit_a, "accept")
    assert verdict_a.status_code == 202, verdict_a.text
    assert (await hook_a).status_code == 200
    assert pending_elicitations.count_for(session_id) == 1

    # Claude runs A's command; mirror A's tool_use then tool_result. The
    # result's input ("ls") matches no still-parked prompt (A is gone).
    call_id = "toolu_bash_a"
    await _post_external_conversation_item(
        client,
        session_id,
        "function_call",
        {
            "agent": "Claude",
            "name": "Bash",
            "arguments": json.dumps({"command": "ls"}),
            "call_id": call_id,
        },
        source_id="src_fc_a",
        response_id="resp_a",
    )
    await _post_external_conversation_item(
        client,
        session_id,
        "function_call_output",
        {"call_id": call_id, "output": "file1\n"},
        source_id="src_fco_a",
        response_id="resp_a",
    )

    # Give any wrong resolution of B a chance to surface before asserting.
    await asyncio.sleep(0.1)
    resolved_b = [
        e
        for e in captured
        if e.get("type") == "response.elicitation_resolved" and e.get("elicitation_id") == elicit_b
    ]
    assert resolved_b == [], "prompt B (command 'rm x') must stay live"
    assert pending_elicitations.count_for(session_id) == 1

    # Cleanup: resolve B via web verdict so its parked hook returns.
    verdict_b = await _post_approval(client, session_id, elicit_b, "accept")
    assert verdict_b.status_code == 202, verdict_b.text
    await hook_b
    capture_task.cancel()
    pending_elicitations.reset_for_tests()


async def test_pre_permission_tool_call_does_not_deny_permission_hook(
    client: httpx.AsyncClient,
) -> None:
    """
    A same-tool ``function_call`` observed before the user verdict must
    not wake the parked Claude ``PermissionRequest`` hook as a deny.

    Claude Code 2.1.157 writes the ``Edit`` tool-use record to its
    transcript before the PermissionRequest command hook has returned.
    The old auto-resolve path treated that forwarded ``function_call``
    as proof that the user approved in the terminal, resolved the hook
    future with ``cancel``, and the hook mapped every non-``accept``
    result to ``decision.behavior == "deny"``. This reproduces that
    order: hook publishes approval, the transcript forwards ``Edit``,
    then the web UI accepts. The final hook response must still allow.
    """
    from omnigent.runtime import pending_elicitations

    pending_elicitations.reset_for_tests()
    agent = await create_test_agent(client, "test-permission-edit-pre-call")
    session_id = await _create_session(client, agent["id"])

    captured: list[dict[str, Any]] = []
    capture_task = asyncio.create_task(_subscribe_into(session_id, captured))
    await asyncio.sleep(0.05)

    edit_payload = await _claude_permission_payload(tool_name="Edit")
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/permission-request",
            json=edit_payload,
        )
    )
    edit_event = await _wait_for_event(
        captured,
        lambda e: e.get("type") == "response.elicitation_request",
    )
    edit_elicit_id = edit_event["elicitation_id"]

    # Newer Claude writes the tool_use before the PermissionRequest
    # hook has returned. Forwarding that item must not resolve the
    # parked hook as a cancel/deny.
    fc = await _post_external_conversation_item(
        client,
        session_id,
        "function_call",
        {
            "agent": "Claude",
            "name": "Edit",
            "arguments": json.dumps(
                {
                    "file_path": "/tmp/TODO.md",
                    "old_string": "x",
                    "new_string": "x\n",
                }
            ),
            "call_id": "toolu_edit_pre_permission",
        },
        source_id="src_edit_fc_pre_permission",
        response_id="resp_edit_pre_permission",
    )
    assert fc.status_code < 300, fc.text

    await asyncio.sleep(0.1)
    assert not hook_task.done(), (
        "Forwarding a same-tool function_call before the approval verdict "
        "must not finish the PermissionRequest hook."
    )
    assert pending_elicitations.count_for(session_id) == 1, (
        f"Edit prompt should remain pending until approval, got "
        f"{pending_elicitations.count_for(session_id)!r}"
    )

    verdict = await _post_approval(client, session_id, edit_elicit_id, "accept")
    assert verdict.status_code == 202, verdict.text
    resp = await hook_task
    assert resp.status_code == 200, resp.text
    decision = resp.json()["hookSpecificOutput"]["decision"]
    assert decision == {"behavior": "allow"}

    resolved_for_edit = await _wait_for_event(
        captured,
        lambda e: (
            e.get("type") == "response.elicitation_resolved"
            and e.get("elicitation_id") == edit_elicit_id
        ),
    )
    assert resolved_for_edit["elicitation_id"] == edit_elicit_id
    assert pending_elicitations.count_for(session_id) == 0, (
        f"Edit prompt should clear after approval, got "
        f"{pending_elicitations.count_for(session_id)!r}"
    )

    capture_task.cancel()
    pending_elicitations.reset_for_tests()


async def test_hook_returns_verdict_when_disconnect_poll_swallows_its_cancel(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A verdict releases the hook even if the disconnect watcher ignores
    its cancellation.

    Regression test for the gate's cleanup-cancellation race: the gate's cleanup cancels the
    disconnect-watcher task and awaits it. Starlette's
    ``is_disconnected()`` runs inside a pre-cancelled anyio cancel
    scope, and a cancel landing in that window is coalesced with the
    scope's own cancellation and swallowed — the watcher survives and
    the old unbounded ``await race_task`` wedged the request for the
    gate's full timeout (24h on this path), hanging CI workers. The
    stub watcher below swallows its cancel deterministically; the hook
    must still return the verdict within the bounded reap window.
    """
    swallowed_cancel = asyncio.Event()
    stop_watcher = asyncio.Event()

    async def _cancellation_immune_poll(_request: Any) -> None:
        # Deterministic stand-in for the swallowed-cancel race: keep
        # running after Task.cancel(), exactly like a poller whose
        # CancelledError was absorbed by an anyio scope unwind.
        while not stop_watcher.is_set():
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                swallowed_cancel.set()

    monkeypatch.setattr(
        sessions_route,
        "_poll_request_disconnect",
        _cancellation_immune_poll,
    )
    # Short reap cap so the test proves the bound, not just outlasts it.
    # raising=False: on a tree without the bounded reap the constant
    # doesn't exist; the test must then fail on the wedge itself (the
    # 5s timeout below), not on the patch.
    monkeypatch.setattr(sessions_route, "_RACE_TASK_REAP_TIMEOUT_S", 0.2, raising=False)

    agent = await create_test_agent(client, "test-permission-swallowed-cancel")
    session_id = await _create_session(client, agent["id"])
    payload = await _claude_permission_payload()

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/permission-request",
            json=payload,
        )
    )
    try:
        event = await drain_task
        verdict = await _post_approval(client, session_id, event["elicitation_id"], "accept")
        assert verdict.status_code == 202, verdict.text

        # The whole point: the verdict must release the hook promptly.
        # Pre-fix, the gate's cleanup awaited the cancellation-immune
        # watcher forever and this timed out.
        async with asyncio.timeout(5.0):
            resp = await hook_task
        assert resp.status_code == 200, resp.text
        assert resp.json()["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
        # Prove the scenario actually exercised the swallow path — if the
        # watcher was never cancelled (or its cancel propagated), this
        # test silently degrades into a plain allow round-trip.
        assert swallowed_cancel.is_set(), (
            "watcher never saw (and swallowed) a cancellation — the gate "
            "cleanup did not cancel the disconnect watcher"
        )
    finally:
        stop_watcher.set()
        if not hook_task.done():
            hook_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hook_task


_CODEX_REPARK_PAYLOAD: dict[str, Any] = {
    "id": 12,
    "method": "mcpServer/elicitation/request",
    "params": {
        "threadId": "thread_rp",
        "turnId": "turn_rp",
        "serverName": "files",
        "mode": "form",
        "message": "Overwrite the file?",
        "requestedSchema": {"type": "object", "properties": {"ok": {"type": "string"}}},
    },
}


async def test_codex_hook_repark_after_disconnect_keeps_card_pending(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A re-POSTed codex envelope re-parks the SAME elicitation after a cut.

    Codex ids are deterministic per (session, method, rpc id), so the
    forwarder's retry needs no payload changes — this pins the server
    half of that contract: the deferred clear no-ops on the re-park,
    the card stays pending, and the verdict resolves the retried poll
    in Codex's JSON-RPC result shape.
    """
    from omnigent.runtime import pending_elicitations

    disconnect_calls = 0

    async def _disconnect_first_call_only(_request: Any) -> None:
        """
        Sever the first hook long-poll; park every later one.

        :param _request: Ignored FastAPI request.
        :returns: None when simulating the first call's disconnect.
        """
        nonlocal disconnect_calls
        disconnect_calls += 1
        if disconnect_calls == 1:
            await asyncio.sleep(0.01)
            return
        await asyncio.Event().wait()

    monkeypatch.setattr(
        sessions_route,
        "_poll_request_disconnect",
        _disconnect_first_call_only,
    )
    # Wide enough that the re-park always lands before the grace expires.
    monkeypatch.setattr(
        sessions_route,
        "_HARNESS_ELICITATION_REPARK_GRACE_S",
        1.0,
    )
    pending_elicitations.reset_for_tests()
    agent = await create_test_agent(client, "test-codex-repark")
    session_id = await _create_session(client, agent["id"])

    first = await client.post(
        f"/v1/sessions/{session_id}/hooks/codex-elicitation-request",
        json=_CODEX_REPARK_PAYLOAD,
    )
    assert first.status_code == 200, first.text
    assert first.content == b"", f"severed poll should fail-ask, got {first.content!r}"
    deferred_tasks = set(sessions_route._deferred_elicitation_clear_tasks)
    assert len(deferred_tasks) == 1, (
        f"expected one deferred clear after the severed poll, got "
        f"{len(deferred_tasks)} — disconnect regressed to immediate wipe, or leaked tasks."
    )

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    hook_task = asyncio.create_task(
        client.post(
            f"/v1/sessions/{session_id}/hooks/codex-elicitation-request",
            json=_CODEX_REPARK_PAYLOAD,
        )
    )
    event = await drain_task
    # Same envelope → same deterministic id → the SAME elicitation
    # re-parks instead of minting a second card.
    assert event["params"]["codex_request_id"] == 12

    # Run the deferred clear to completion: it must see the re-park and no-op.
    for task in deferred_tasks:
        await asyncio.wait_for(task, timeout=5.0)
    # 1 = the card survived the severed long-poll. 0 = the deferred
    # clear ignored the re-park and wiped the live prompt.
    assert pending_elicitations.count_for(session_id) == 1, (
        f"re-parked codex elicitation should stay pending, got "
        f"{pending_elicitations.count_for(session_id)!r}"
    )

    verdict = await _post_approval(
        client,
        session_id,
        event["elicitation_id"],
        "accept",
        content={"ok": "yes"},
    )
    assert verdict.status_code == 202, verdict.text
    resp = await hook_task
    assert resp.status_code == 200, resp.text
    # The retried poll carries the verdict in Codex's result shape —
    # exactly what the forwarder relays back over JSON-RPC.
    assert resp.json() == {"action": "accept", "content": {"ok": "yes"}, "_meta": None}
    pending_elicitations.reset_for_tests()


async def test_codex_hook_gap_verdict_returned_on_repost(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A verdict landing between a severed codex poll and its retry is
    handed to the retry via the pre-resolved tombstone, mapped into
    Codex's JSON-RPC result shape.

    Without it the retry would re-publish the prompt and fail-ask
    later — the click dropped, the codex sub-agent still blocked.
    """
    from omnigent.runtime import pending_elicitations

    async def _disconnect_immediately(_request: Any) -> None:
        """
        Sever every hook long-poll straight away.

        :param _request: Ignored FastAPI request.
        :returns: None.
        """
        await asyncio.sleep(0.01)

    monkeypatch.setattr(
        sessions_route,
        "_poll_request_disconnect",
        _disconnect_immediately,
    )
    monkeypatch.setattr(
        sessions_route,
        "_HARNESS_ELICITATION_REPARK_GRACE_S",
        0.25,
    )
    pending_elicitations.reset_for_tests()
    agent = await create_test_agent(client, "test-codex-gap-verdict")
    session_id = await _create_session(client, agent["id"])

    drain_task = asyncio.create_task(_drain_until_elicitation(session_id))
    await asyncio.sleep(0.05)
    first = await client.post(
        f"/v1/sessions/{session_id}/hooks/codex-elicitation-request",
        json=_CODEX_REPARK_PAYLOAD,
    )
    assert first.status_code == 200, first.text
    assert first.content == b""
    event = await drain_task

    # Verdict arrives while NO poll is parked (the gap).
    verdict = await _post_approval(
        client,
        session_id,
        event["elicitation_id"],
        "accept",
        content={"ok": "go"},
    )
    assert verdict.status_code == 202, verdict.text
    assert pending_elicitations.count_for(session_id) == 0

    # The retry consumes the tombstone and returns the verdict in the
    # codex result shape without re-publishing the prompt; an empty
    # body here means the tombstone was dropped and the click lost.
    second = await client.post(
        f"/v1/sessions/{session_id}/hooks/codex-elicitation-request",
        json=_CODEX_REPARK_PAYLOAD,
    )
    assert second.status_code == 200, second.text
    assert second.json() == {"action": "accept", "content": {"ok": "go"}, "_meta": None}
    # Drain the severed poll's deferred clear so it doesn't outlive the
    # test's event loop (it no-ops the index either way).
    for task in set(sessions_route._deferred_elicitation_clear_tasks):
        await asyncio.wait_for(task, timeout=5.0)
    pending_elicitations.reset_for_tests()
