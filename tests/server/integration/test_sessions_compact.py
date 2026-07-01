"""Integration tests for the explicit ``compact`` control event.

The web-UI ``/compact`` command and compact button POST
``{"type": "compact"}`` to ``POST /v1/sessions/{id}/events``. Per
``designs/CLAUDE_NATIVE.md`` ("Control events dispatch on the runner"),
the Omnigent server stays harness-agnostic: it forwards the control to the
bound runner and only runs its own in-process compaction
(``_run_compact_locked`` → ``compact_conversation_now``) when the
runner did NOT handle it.

The runner's dispatch contract (verified in
``tests/runner/test_app_sessions_native.py``):

* claude-native injects ``/compact`` into the tmux pane and returns
  **200** — Claude Code compacts its own context.
* other harnesses **204** no-op — the Omnigent server owns the operation.
* a failed injection (pane not attached) returns **503**.

These tests pin the Omnigent side of that contract by stubbing the runner's
HTTP response and asserting whether the AP-side compaction ran.
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
        ``compact`` ``/events`` POST, e.g. ``200`` (claude-native
        handled), ``204`` (in-process no-op), or ``503`` (pane not
        attached).
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

    This is the fix for the original bug: claude-native sessions bind
    to an LLM-less pseudo-agent, so ``_run_compact_locked`` would 400.
    When the runner reports it handled the control (200), the Omnigent server
    must NOT run ``compact_conversation_now`` at all.
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
    # Exactly one compact control was forwarded to the runner. 0 = the
    # Omnigent server never forwarded (it would have run _run_compact_locked
    # directly — the pre-fix behavior); 2+ = duplicate forward.
    assert captured == [{"type": "compact"}], (
        f"AP server must forward exactly one compact control to the runner; got {captured!r}."
    )


async def test_compact_runs_omnigent_compaction_when_runner_noops(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A 204 from the runner (in-process harness) makes the Omnigent server run
    its own ``compact_conversation_now``.

    In-process harnesses have no terminal to inject into — explicit
    compaction is an AP-side LLM summarisation. The 204 no-op tells the
    Omnigent server it owns the operation, so it must still forward the
    control (harness-agnostic) AND then run the compaction.
    """
    from omnigent.runtime import set_runner_client

    calls: list[dict[str, Any]] = []

    async def _record(**kwargs: Any) -> CompactionResult:
        """Record that AP-side compaction ran; return a real result."""
        calls.append(kwargs)
        return CompactionResult(messages=[], summary_metadata=None, total_tokens=1234)

    monkeypatch.setattr(
        "omnigent.runtime.workflow.compact_conversation_now",
        _record,
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

    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": False}, resp.text
    # Control was still forwarded even though the runner no-ops — the
    # Omnigent server is harness-agnostic and forwards for every harness.
    assert captured == [{"type": "compact"}], (
        f"AP server must forward compact to the runner even on the "
        f"in-process path; got {captured!r}."
    )
    # AP-side compaction ran exactly once for the session it was asked
    # to compact. 0 = the 204 path skipped compaction (the in-process
    # /compact silently does nothing); 2+ = double compaction.
    assert len(calls) == 1, (
        f"Expected exactly one compact_conversation_now call on the 204 path; got {len(calls)}."
    )
    assert calls[0]["conversation_id"] == sid, (
        f"AP-side compaction ran for the wrong session; got "
        f"{calls[0].get('conversation_id')!r}, expected {sid!r}."
    )


async def test_compact_model_less_sdk_harness_returns_clear_unavailable_message(
    client: httpx.AsyncClient,
) -> None:
    """
    A model-less SDK-style harness should not expose the raw server-side
    compaction model requirement.
    """
    agent = await create_test_agent(
        client,
        name="model-less-sdk",
        executor={"type": "agents_sdk"},
        include_llm=False,
    )
    sid = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={"type": "compact", "data": {}},
    )

    assert resp.status_code == 400, resp.text
    assert "/compact is unavailable" in resp.text
    assert "agents_sdk" in resp.text
    assert "llm.model" in resp.text
    assert "executor.model" in resp.text


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
        """Fail loudly if AP-side compaction is reached on the error path."""
        raise AssertionError(
            "compact_conversation_now must not run when the runner "
            "returned a non-200/204 status — Omnigent fell through to its "
            "own compaction instead of surfacing the runner failure."
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

    # 500 = INTERNAL_ERROR raised from the compact branch on a runner
    # 5xx. A 200 here would mean the error was swallowed; a 400 would
    # mean it fell through to _run_compact_locked's LLM-config check.
    assert resp.status_code == 500, resp.text
    # The control was forwarded before the failure was detected.
    assert captured == [{"type": "compact"}], (
        f"AP server must have forwarded the compact control before "
        f"surfacing the runner failure; got {captured!r}."
    )


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
