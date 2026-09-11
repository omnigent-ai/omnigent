"""Regression: cancel + retry must not re-send the prompt in a loop.

Reproduces the reported Smart-Routing web journey at the server/runner layer:

    1. Start a session and send a prompt (it routes/starts a turn that the user
       does not want).
    2. Cancel the in-flight turn (the "routed to the wrong model" moment).
    3. Retry -- the web Retry button POSTs a ``retry_session`` event, which
       relaunches a runner whose tunnel dropped.

Observed failure (the bug): the relaunched runner re-runs the cancelled prompt
on its own -- ``retry_session`` is a pure reconnect and must NOT dispatch a new
turn, yet a spurious turn fires and the LLM is hit again. Repeated cancel/retry
cycles re-fire it every time -- the "re-sends the same prompt in a loop" the
user sees.

Mechanism (root-cause lead, not asserted here): a cancel discards the partial
assistant output and persists a synthetic ``role: user`` cancellation marker
(``[System: interrupted] ...``, ``omnigent/runner/app.py``
``_append_cancellation_items``), so the transcript's trailing item is a user
message. ``retry_session`` relaunches the runner and initializes the session
with ``suppress_recovery_turn=False``
(``omnigent/server/routes/sessions/routes_events.py`` ``_recover_retry_session``);
the runner's crash-recovery heuristic (``last_type == "message" and last_role
== "user"`` => ``needs_turn``, ``omnigent/runner/app.py``) does not exclude the
cancellation marker, so it starts a spurious ``turn-recover-*`` turn.

This runs against a REAL server + host daemon + relaunchable runner + the mock
LLM -- no real credentials. It is the reachable equivalent of the reported
journey: the re-send loop is orthogonal to Smart Routing's model choice -- it
is the cancel -> runner-relaunch -> recovery-turn interaction, which this
exercises end to end.

The mock queue is claimed by CONTENT (a unique token embedded in the prompt)
rather than by model id, so both the original request and any spurious
recovery request -- which replays the same transcript and therefore carries the
same token -- deterministically route to this test's queue.
"""

from __future__ import annotations

import json
import os
import signal
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    POLL_INTERVAL_S,
    configure_mock_llm,
    get_mock_requests,
    lookup_agent_id,
    release_mock_gate,
    reset_mock_llm,
    send_user_message_to_session,
    upload_agent,
)
from tests.e2e.test_host_e2e import (
    _runner_pid_from_daemon_log,
    _spawn_host_daemon,
    _wait_for_host_online,
)

pytestmark = [pytest.mark.timeout(600, method="signal")]

# The runner persists a cancellation as a synthetic user message; match on the
# stable substring rather than the full wording so this survives copy edits.
_CANCELLATION_MARKER_TEXT = "interrupted"

# What the buggy spurious recovery turn would produce if it fires. Its presence
# in the transcript, or any new LLM request after retry_session, is the re-send.
_SPURIOUS_RESEND_REPLY = "SPURIOUS_RESEND_REPLY"

# How long after retry_session we watch for a spurious re-sent LLM request.
# The recovery turn (when the bug fires) starts during the retry_session call
# itself, so this only needs to cover harness startup + one request.
_SPURIOUS_WATCH_SECONDS = 45.0


def _write_agent_yaml(tmp_path: Path, model: str) -> Path:
    """Write a minimal single-model agent whose model routes to a mock queue.

    :param tmp_path: Pytest temp directory.
    :param model: Unique model id baked into the spec so mock-LLM requests from
        this agent are attributable to this test.
    :returns: Path to the agent directory.
    """
    agent_dir = tmp_path / "cancel-retry-agent"
    agent_dir.mkdir()
    (agent_dir / "cancel-retry-agent.yaml").write_text(
        "\n".join(
            [
                "name: cancel-retry-agent",
                "description: Minimal agent for the cancel+retry reconnect repro.",
                "executor:",
                "  harness: openai-agents",
                f"  model: {model}",
                "prompt: |",
                "  You are a terse smoke-test assistant.",
                "  Follow the user's instruction exactly.",
                "",
            ]
        )
    )
    return agent_dir


def _token_requests(mock_llm_server_url: str, token: str) -> list[dict[str, Any]]:
    """Return captured mock-LLM requests whose body carries *token*.

    Counts by content rather than by the request's ``model`` field so the
    attribution matches the content routing used to claim the queue.

    :param mock_llm_server_url: Mock server base URL.
    :param token: Unique prompt token embedded by this test.
    :returns: Captured request bodies containing the token.
    """
    return [req for req in get_mock_requests(mock_llm_server_url) if token in json.dumps(req)]


def _wait_for_gate_pending(mock_llm_server_url: str, timeout: float = 120.0) -> None:
    """Poll until a request is blocked on the mock LLM gate.

    :param mock_llm_server_url: Mock server base URL.
    :param timeout: Max seconds to wait.
    :raises AssertionError: If no request blocks within *timeout*; the message
        includes the models of all captured requests so a routing miss (request
        arrived but drew from another queue) is distinguishable from a dispatch
        failure (no request at all).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = httpx.get(f"{mock_llm_server_url}/gate/pending", timeout=2.0)
        resp.raise_for_status()
        if resp.json().get("pending"):
            return
        time.sleep(0.2)
    captured = get_mock_requests(mock_llm_server_url)
    models = [req.get("model") for req in captured]
    raise AssertionError(
        f"No gate pending within {timeout}s. Captured {len(captured)} mock-LLM "
        f"request(s) with model field(s) {models!r} -- zero requests means the "
        "turn never dispatched; nonzero means the request routed to a "
        "non-blocking queue."
    )


def _all_items(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    """Return all durable session items in chronological order.

    :param client: HTTP client pointed at the live server.
    :param session_id: Session id to query.
    :returns: Durable session item dictionaries.
    """
    resp = client.get(
        f"/v1/sessions/{session_id}/items",
        params={"limit": 1000, "order": "asc"},
    )
    resp.raise_for_status()
    return resp.json()["data"]


def _wait_for_cancellation_marker(
    client: httpx.Client,
    session_id: str,
    timeout: float = 60.0,
) -> None:
    """Wait until the synthetic cancellation marker is durably persisted.

    :param client: HTTP client pointed at the live server.
    :param session_id: Session id to query.
    :param timeout: Max seconds to wait.
    :raises AssertionError: If the marker never appears.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for item in _all_items(client, session_id):
            if (
                item.get("type") == "message"
                and item.get("role") == "user"
                and any(
                    _CANCELLATION_MARKER_TEXT in c.get("text", "") for c in item.get("content", [])
                )
            ):
                return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"No cancellation marker in session {session_id} within {timeout}s")


def _wait_for_runner_offline(
    client: httpx.Client,
    runner_id: str,
    timeout: float = 15.0,
) -> None:
    """Wait until the server observes the runner tunnel gone.

    Ensures ``retry_session`` exercises the relaunch path rather than the
    already-connected fast path.

    :param client: HTTP client pointed at the live server.
    :param runner_id: Runner id to poll.
    :param timeout: Max seconds to wait.
    :raises AssertionError: If the runner stays online.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sr = client.get(f"/v1/runners/{runner_id}/status")
        if sr.status_code == 200 and not sr.json().get("online"):
            return
        time.sleep(0.5)
    raise AssertionError(f"Runner {runner_id} still online after SIGKILL")


def _wait_for_runner_online(
    client: httpx.Client,
    runner_id: str,
    timeout: float = 30.0,
) -> None:
    """Wait until the runner reports online.

    :param client: HTTP client pointed at the live server.
    :param runner_id: Runner id to poll.
    :param timeout: Max seconds to wait.
    :raises AssertionError: If the runner never comes online.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sr = client.get(f"/v1/runners/{runner_id}/status")
        if sr.status_code == 200 and sr.json().get("online"):
            return
        time.sleep(0.5)
    raise AssertionError(f"Runner {runner_id} never came online")


def test_cancel_then_retry_does_not_resend_prompt(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """Cancel + ``retry_session`` must reconnect only, never re-run the prompt.

    Drives the reported journey against a real host + relaunchable runner:
    send a prompt (blocked mid-flight) -> cancel -> kill the runner ->
    ``retry_session`` relaunches it. A correct reconnect dispatches no turn, so
    the mock LLM must receive no further request and no assistant reply may
    appear. The bug makes the relaunched runner re-run the cancelled prompt on
    its own -- the re-send loop.
    """
    model = f"cancelretry-{uuid.uuid4().hex[:8]}"
    token = f"cancelretry-token-{uuid.uuid4().hex[:8]}"
    prompt = f"{token} Summarize the quarterly report."
    reset_mock_llm(mock_llm_server_url)
    # Response 1 blocks so we can cancel mid-flight. Response 2 is what a
    # spurious recovery turn would emit if the bug re-runs the prompt. The
    # queue is claimed by the token in the prompt (content routing), so the
    # recovery turn's request -- which replays the same transcript -- routes
    # here too, whatever model id the harness sends.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": "Routing to a model and starting a long answer...", "block": True},
            {"text": _SPURIOUS_RESEND_REPLY},
        ],
        key=model,
        match=token,
    )

    daemon = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=live_server,
        mock_llm_server_url=mock_llm_server_url,
    )
    host_proc = daemon.proc
    host_id = daemon.host_id

    try:
        _wait_for_host_online(http_client, host_id, timeout=30.0)

        # Step 1: create a session and launch a runner for it via the host.
        agent_name = upload_agent(http_client, _write_agent_yaml(tmp_path, model))
        agent_id = lookup_agent_id(http_client, agent_name)
        session_id = http_client.post("/v1/sessions", json={"agent_id": agent_id}).json()["id"]

        launch_resp = http_client.post(
            f"/v1/hosts/{host_id}/runners",
            json={"session_id": session_id, "workspace": str(tmp_path)},
            timeout=60.0,
        )
        assert launch_resp.status_code == 200, launch_resp.text
        runner_id = launch_resp.json()["runner_id"]
        _wait_for_runner_online(http_client, runner_id, timeout=30.0)
        http_client.patch(
            f"/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
        ).raise_for_status()

        # Send the prompt; the mock blocks mid-response so we can cancel it.
        send_user_message_to_session(
            http_client,
            session_id=session_id,
            content=prompt,
        )

        # Step 2: cancel the in-flight turn (routed to the wrong model).
        _wait_for_gate_pending(mock_llm_server_url)
        cancel_resp = http_client.post(
            f"/v1/sessions/{session_id}/events", json={"type": "interrupt"}
        )
        cancel_resp.raise_for_status()
        release_mock_gate(mock_llm_server_url)

        # The cancel persists a synthetic user "interrupted" marker -- so the
        # transcript now ends with a user message.
        _wait_for_cancellation_marker(http_client, session_id)

        # Baseline: the LLM requests seen so far (the cancelled prompt).
        requests_before = len(_token_requests(mock_llm_server_url, token))
        assert requests_before >= 1, "The original prompt never reached the mock LLM"

        # Step 3: the runner drops (host stays up) and the web Retry button
        # POSTs retry_session, relaunching the runner. Kill the runner first so
        # retry exercises the relaunch path, not the already-connected fast path.
        runner_pid = _runner_pid_from_daemon_log(daemon.daemon_log)
        assert runner_pid is not None, daemon.daemon_log.read_text()
        os.kill(runner_pid, signal.SIGKILL)
        _wait_for_runner_offline(http_client, runner_id, timeout=15.0)

        retry_resp = http_client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "retry_session", "data": {}},
            timeout=120.0,
        )
        assert retry_resp.status_code in (200, 202), retry_resp.text
        recovery = retry_resp.json()
        assert recovery.get("recovered") is True, recovery
        assert recovery.get("recovery") == "runner_relaunched", recovery

        # Correct behavior: retry_session is a pure reconnect -- no new turn,
        # so the LLM sees no further request. The bug starts a spurious
        # turn-recover-* during the relaunch init, which re-sends the
        # cancelled prompt here. Watch for it; fail as soon as it appears.
        watch_deadline = time.monotonic() + _SPURIOUS_WATCH_SECONDS
        while time.monotonic() < watch_deadline:
            requests_now = len(_token_requests(mock_llm_server_url, token))
            assert requests_now == requests_before, (
                "retry_session re-ran the cancelled prompt: the mock LLM "
                f"received {requests_now - requests_before} new request(s) "
                "after retry. retry_session must reconnect only, not dispatch "
                "a turn -- this is the re-send loop."
            )
            time.sleep(1.0)

        items_text = " ".join(
            c.get("text", "")
            for item in _all_items(http_client, session_id)
            if item.get("type") == "message" and item.get("role") == "assistant"
            for c in item.get("content", [])
        )
        assert _SPURIOUS_RESEND_REPLY not in items_text, (
            "A spurious recovery turn ran after retry_session and produced an "
            "assistant reply -- the cancelled prompt was re-sent."
        )

    finally:
        if host_proc.poll() is None:
            host_proc.send_signal(signal.SIGTERM)
            try:
                host_proc.wait(timeout=5)
            except Exception:
                host_proc.kill()
                host_proc.wait()
