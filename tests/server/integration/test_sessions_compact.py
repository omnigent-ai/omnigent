"""Integration tests for the explicit ``compact`` control event.

The web-UI ``/compact`` command and compact button POST
``{"type": "compact"}`` to ``POST /v1/sessions/{id}/events``. Per
``designs/CLAUDE_NATIVE.md`` ("Control events dispatch on the runner"),
the Omnigent server forwards the control to the bound runner and lets the
runner's harness-specific handler own the operation.

The runner's dispatch contract (verified in
``tests/runner/test_app_sessions_native.py``):

* Native harnesses inject ``/compact`` into the vendor TUI and return
  **200** on success or **5xx** on failure.
* SDK harnesses return **204** (no-op) because their context is controlled
  entirely by the vendor harness; the server surfaces a 400 error.
* A failed injection (pane not attached) returns **503**.

These tests pin the Omnigent side of that contract by stubbing the runner's
HTTP response and asserting the correct server behaviour.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.runtime.compaction import CompactionResult
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def _create_session(client: httpx.AsyncClient, agent_id: str) -> str:
    """
    Create a bare session bound to *agent_id* and return its id.

    :param client: The test HTTP client.
    :param agent_id: Agent id to bind, e.g. ``"ag_abc123"``.
    :returns: The new session id, e.g. ``"conv_abc123"``.
    """
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent_id, "initial_items": []},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _fake_runner_returning(compact_status: int) -> tuple[httpx.AsyncClient, list[dict[str, Any]]]:
    """
    Build a mock runner client that returns *compact_status* for compact.

    The transport records every ``{"type": "compact"}`` body it sees so
    the test can assert the Omnigent server actually forwarded the control,
    and returns *compact_status* for those POSTs (204 for any other
    runner POST so unrelated session traffic passes through).

    :param compact_status: HTTP status the fake runner returns for a
        ``compact`` ``/events`` POST, e.g. ``200`` (native handled),
        ``204`` (SDK no-op), or ``503`` (pane not attached).
    :returns: The mock ``httpx.AsyncClient`` and the list that captures
        forwarded compact bodies.
    """
    captured: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """Record compact POSTs and return the configured status."""
        if request.method != "POST":
            return httpx.Response(204)
        body: dict[str, Any] | None = None
        if request.content:
            try:
                body = json.loads(request.content)
            except json.JSONDecodeError:
                body = None
        if isinstance(body, dict) and body.get("type") == "compact":
            captured.append(body)
            return httpx.Response(compact_status)
        return httpx.Response(204)

    runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )
    return runner, captured


async def test_compact_skips_omnigent_compaction_when_runner_handles_it(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A 200 from the runner (claude-native injected ``/compact``) makes
    the Omnigent server skip its own compaction.

    When the runner reports it handled the control (200), the Omnigent
    server must NOT run ``compact_conversation_now`` at all.
    """
    from omnigent.runtime import set_runner_client

    async def _must_not_run(**_: Any) -> CompactionResult:
        """Fail loudly if AP-side compaction is reached on the 200 path."""
        raise AssertionError(
            "compact_conversation_now must not run when the runner "
            "reported it handled /compact (200). The Omnigent server fell "
            "through to its own compaction instead of skipping."
        )

    monkeypatch.setattr(
        "omnigent.runtime.workflow.compact_conversation_now",
        _must_not_run,
    )

    runner, captured = _fake_runner_returning(200)
    set_runner_client(runner)
    try:
        agent = await create_test_agent(client)
        sid = await _create_session(client, agent["id"])
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={"type": "compact", "data": {}},
        )
    finally:
        await runner.aclose()
        set_runner_client(None)

    # 202 (route default) with queued=False: control forwarded, runner
    # handled it, Omnigent returned without running (or raising from) its own
    # compaction.
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": False}, resp.text
    # Exactly one compact control was forwarded to the runner.
    assert captured == [{"type": "compact"}], (
        f"AP server must forward exactly one compact control to the runner; got {captured!r}."
    )


async def test_compact_returns_error_when_runner_noops(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A 204 from the runner (SDK harness) surfaces a clear 400 error.

    SDK harnesses own their own context; the server cannot compact on their
    behalf. The 204 no-op signals "not handled here" and the server must
    reject the request rather than attempting AP-side compaction.
    """
    from omnigent.runtime import set_runner_client

    async def _must_not_run(**_: Any) -> CompactionResult:
        raise AssertionError("compact_conversation_now must not run when the runner returned 204")

    monkeypatch.setattr(
        "omnigent.runtime.workflow.compact_conversation_now",
        _must_not_run,
    )

    runner, captured = _fake_runner_returning(204)
    set_runner_client(runner)
    try:
        agent = await create_test_agent(client)
        sid = await _create_session(client, agent["id"])
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={"type": "compact", "data": {}},
        )
    finally:
        await runner.aclose()
        set_runner_client(None)

    assert resp.status_code == 400, resp.text
    assert "/compact is not available" in resp.text
    # Control was still forwarded before the error.
    assert captured == [{"type": "compact"}], (
        f"AP server must forward compact to the runner before returning the error; "
        f"got {captured!r}."
    )


async def test_compact_sdk_harness_no_runner_returns_not_available(
    client: httpx.AsyncClient,
) -> None:
    """
    A compact request for an SDK-harness session with no runner returns a
    clear 400 "not available for this session type" error.
    """
    agent = await create_test_agent(
        client,
        name="sdk-no-runner-compact",
        executor={"type": "omnigent", "config": {"harness": "openai-agents"}},
        include_llm=False,
    )
    sid = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={"type": "compact", "data": {}},
    )

    assert resp.status_code == 400, resp.text
    assert "/compact is not available" in resp.text


async def test_compact_errors_when_runner_injection_fails(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A 503 from the runner (pane not attached) surfaces as an error and
    does NOT fall through to AP-side compaction.

    A claude-native session whose tmux pane is gone cannot compact, and
    AP-side compaction would be both broken (no LLM) and semantically
    wrong (summarising the mirror). The Omnigent server must surface the
    failure rather than silently running its own compaction.
    """
    from omnigent.runtime import set_runner_client

    async def _must_not_run(**_: Any) -> CompactionResult:
        raise AssertionError(
            "compact_conversation_now must not run when the runner returned a 5xx"
        )

    monkeypatch.setattr(
        "omnigent.runtime.workflow.compact_conversation_now",
        _must_not_run,
    )

    runner, captured = _fake_runner_returning(503)
    set_runner_client(runner)
    try:
        agent = await create_test_agent(client)
        sid = await _create_session(client, agent["id"])
        resp = await client.post(
            f"/v1/sessions/{sid}/events",
            json={"type": "compact", "data": {}},
        )
    finally:
        await runner.aclose()
        set_runner_client(None)

    assert resp.status_code == 500, resp.text
    # The control was forwarded before the failure was detected.
    assert captured == [{"type": "compact"}], (
        f"AP server must have forwarded the compact control before "
        f"surfacing the runner failure; got {captured!r}."
    )


async def test_compact_native_session_no_runner_returns_reconnect_error(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A native-terminal /compact with no reachable runner surfaces a clear
    "reconnect first" 503, not a generic error.

    Native sessions compact only in their vendor TUI, so when no runner is
    bound the compact branch must try to wake the runner and, when it can't
    (un-host-bound session), return a RUNNER_UNAVAILABLE the user can act on.
    """

    async def _must_not_run(**_: Any) -> CompactionResult:
        raise AssertionError(
            "compact_conversation_now must not run for a native session with no runner"
        )

    monkeypatch.setattr(
        "omnigent.runtime.workflow.compact_conversation_now",
        _must_not_run,
    )

    # No runner bound (no set_runner_client) and no host_id → unwakeable.
    agent = await create_test_agent(
        client,
        name="claude-native-compact",
        executor={"type": "omnigent", "config": {"harness": "claude-native"}},
    )
    sid = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={"type": "compact", "data": {}},
    )

    assert resp.status_code == 503, resp.text
    assert "Reconnect the session" in resp.text
    assert "llm.model" not in resp.text


# ── external_compaction_status: terminal-observed compaction edge ────────
#
# The claude-native forwarder posts external_compaction_status when Claude
# Code's PreCompact / post-compaction SessionStart(source=compact) hooks
# fire, so the web UI brackets Claude's own terminal compaction with the
# same "Compacting conversation…" spinner the AP-side path drives.


@pytest.mark.parametrize(
    "status,expected_event",
    [
        ("in_progress", "response.compaction.in_progress"),
        ("completed", "response.compaction.completed"),
        ("failed", "response.compaction.failed"),
    ],
)
async def test_external_compaction_status_publishes_compaction_sse(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    expected_event: str,
) -> None:
    """
    external_compaction_status republishes the matching compaction SSE.

    The forwarder posts this from Claude's PreCompact (in_progress) and
    post-compaction SessionStart (completed) hooks. Omnigent must translate it
    into the same response.compaction.* SSE the web client already
    renders, otherwise the spinner never appears for claude-native
    sessions (the gap the user reported: summary flushes with no
    in-progress indicator).
    """
    published: list[tuple[str, dict[str, Any]]] = []

    def capture_publish(session_id: str, event: dict[str, Any]) -> None:
        """Capture session-stream events emitted by the route."""
        published.append((session_id, event))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        capture_publish,
    )
    agent = await create_test_agent(client)
    sid = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={"type": "external_compaction_status", "data": {"status": status}},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": False}, resp.text

    # Exactly the one matching compaction SSE, scoped to this session.
    # A different event type (or zero) would mean the status→SSE mapping
    # regressed and the web UI spinner would not bracket compaction.
    assert [event["type"] for _, event in published] == [expected_event], (
        f"Expected one {expected_event!r} event; got {published!r}."
    )
    assert published[0][0] == sid
    # completed carries no token count from the hook path (the context
    # ring is updated separately via external_session_usage), so the
    # payload must omit total_tokens rather than send a bogus value.
    if status == "completed":
        assert "total_tokens" not in published[0][1], (
            f"completed from the hook path must omit total_tokens; got {published[0][1]!r}."
        )


async def test_external_compaction_status_rejects_unknown_status(
    client: httpx.AsyncClient,
) -> None:
    """
    Unknown compaction-status values are rejected with a 400.

    Without this guard a typo in the forwarder would publish a
    non-conforming event the SDK's strict adapter drops downstream —
    the fail-loud guard rule 15 exists to prevent.
    """
    agent = await create_test_agent(client)
    sid = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={"type": "external_compaction_status", "data": {"status": "Done"}},
    )
    assert resp.status_code == 400, resp.text
    assert "external_compaction_status" in resp.text


async def test_compaction_snapshot_persists_without_base64_payloads(
    client: httpx.AsyncClient,
) -> None:
    """
    A compaction snapshot reaches storage with no inline base64 payload.

    The strip is unit-tested at ``parse_item_data``; this walks the whole
    user-visible path instead — the event a native forwarder POSTs, through
    the store, back out of ``GET /items`` — because that round trip is what
    the reported multi-MB rows were actually made of.
    """
    payload = "iVBORw0KGgoAAAANSUhEUgAAAAE" + "A" * 20_000
    agent = await create_test_agent(client)
    sid = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={
            "type": "compaction",
            "data": {
                "summary": "[Claude Code compaction — context was compacted in the terminal]",
                "last_item_id": "msg_boundary_abc123",
                "model": "unknown",
                "token_count": 0,
                "compacted_messages": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_screenshot_01",
                                "content": [
                                    {
                                        "type": "image",
                                        "source": {
                                            "type": "base64",
                                            "media_type": "image/png",
                                            "data": payload,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "I can see the screenshot."}],
                    },
                ],
            },
        },
    )
    assert resp.status_code == 202, resp.text

    items_resp = await client.get(f"/v1/sessions/{sid}/items")
    assert items_resp.status_code == 200, items_resp.text
    compaction_items = [i for i in items_resp.json()["data"] if i.get("type") == "compaction"]
    assert len(compaction_items) == 1, (
        f"Expected exactly one compaction item; got {len(compaction_items)}."
    )

    item = compaction_items[0]
    # to_api_dict spreads CompactionData onto the top level, so serialising the
    # whole item leaves nowhere for a stray copy of the payload to hide.
    item_json = json.dumps(item)
    assert payload[:200] not in item_json, (
        "Compaction snapshot persisted the full base64 image payload verbatim."
    )
    assert len(item_json) < len(payload) // 2, (
        f"Stored compaction item is {len(item_json):,} bytes — close to the "
        f"{len(payload):,}-byte payload, so it was not stripped."
    )

    # Still a usable snapshot: summary intact, message shape preserved, and
    # only the payload swapped for a marker that names what was dropped.
    assert item["summary"].startswith("[Claude Code compaction")
    messages = item["compacted_messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    source = messages[0]["content"][0]["content"][0]["source"]
    assert source["media_type"] == "image/png"
    assert source["data"] == "[image/png content omitted from the compaction snapshot]"
    assert messages[1]["content"][0]["text"] == "I can see the screenshot."
