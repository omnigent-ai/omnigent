"""E2E: deleting a session on a replica that does not hold its runner relay.

Replica A holds the runner's tunnel and the session's relay. The tunnel drops
mid-turn and the runner stays away. Inside A's reconnect grace the session is
deleted through replica B, which shares A's database but has no handle on A's
relay. When A's grace expires it must not record a failed turn for a session
that no longer exists.

Run::

    uv run --no-sync pytest \
        tests/e2e/test_delete_session_other_replica_relay_grace_e2e.py -v
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests._helpers.conversation_rows import conversation_rows
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    register_inline_agent,
    release_mock_gate,
    reset_mock_llm,
)
from tests.e2e.test_runner_tunnel_mid_turn_reconnect_grace_e2e import (
    _gate_pending,
    _poll_until,
    _ReconnectStack,
    _send_user_message,
    _session_snapshot,
)

pytestmark = [pytest.mark.timeout(420, method="signal")]

_ANSWER = "DELETE_OTHER_REPLICA_E2E_NEVER_DELIVERED"


@pytest.fixture
def replica_a_stack(mock_llm_server_url: str, tmp_path: Path) -> Iterator[_ReconnectStack]:
    """Yield replica A with its ingress proxy and the real runner tunnelled through it."""
    stack = _ReconnectStack(mock_llm_server_url, tmp_path)
    stack.start()
    try:
        yield stack
    finally:
        stack.teardown()


def test_delete_on_replica_without_the_relay_does_not_fail_the_deleted_turn(
    replica_a_stack: _ReconnectStack,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """Replica B deletes a session whose relay is riding out a runner drop on A."""
    from omnigent.server.routes.sessions import RUNNER_DISCONNECT_GRACE_S

    stack = replica_a_stack
    proxy = stack.proxy
    assert proxy is not None
    replica_b = stack.start_second_replica()
    observations: dict[str, object] = {
        "replica_a": stack.base_url,
        "replica_b": replica_b.base_url,
        "runner_id": stack.runner_id,
        "grace_s": RUNNER_DISCONNECT_GRACE_S,
    }
    try:
        reset_mock_llm(mock_llm_server_url)
        model = f"delete-other-replica-{uuid.uuid4().hex[:8]}"
        configure_mock_llm(mock_llm_server_url, [{"text": _ANSWER, "block": True}], key=model)
        agent_name = register_inline_agent(
            stack.client,
            name=f"delete-other-replica-{uuid.uuid4().hex[:8]}",
            harness="openai-agents",
            model=model,
            profile="",
            prompt="Return the configured answer.",
            mock_llm_base_url=f"{mock_llm_server_url}/v1",
        )
        session_id = create_runner_bound_session(
            stack.client, agent_name=agent_name, runner_id=stack.runner_id
        )
        observations["session_id"] = session_id
        _send_user_message(stack.client, session_id)
        _poll_until(
            lambda: _gate_pending(mock_llm_server_url),
            timeout=60.0,
            what="the real turn to block inside the mock LLM on replica A",
        )
        assert _session_snapshot(stack.client, session_id)["status"] == "running"
        assert f"Relay: creating new for session={session_id}" in stack.process_log.read_text()

        proxy.begin_blackout()
        _poll_until(
            lambda: proxy.rejected_connections > 0,
            timeout=10.0,
            what="the real runner to attempt a reconnect through the 503 ingress",
        )
        transport_lost = (
            f"Relay: runner transport lost for session={session_id} (intentional=False"
        )
        _poll_until(
            lambda: transport_lost in stack.process_log.read_text(),
            timeout=30.0,
            what="replica A to start the relay's reconnect grace",
        )
        grace_started = time.monotonic()

        deleted = replica_b.client.delete(f"/v1/sessions/{session_id}")
        observations["delete_status"] = deleted.status_code
        observations["delete_after_grace_start_s"] = round(time.monotonic() - grace_started, 2)
        assert deleted.status_code == 200, deleted.text
        assert observations["delete_after_grace_start_s"] < RUNNER_DISCONNECT_GRACE_S / 2
        observations["get_after_delete"] = {
            "replica_a": stack.client.get(f"/v1/sessions/{session_id}").status_code,
            "replica_b": replica_b.client.get(f"/v1/sessions/{session_id}").status_code,
        }
        assert observations["get_after_delete"] == {"replica_a": 404, "replica_b": 404}
        observations["rows_after_delete"] = conversation_rows(
            stack._conversation_database_uri, session_id
        )

        _poll_until(
            lambda: stack.relay_decision(session_id) is not None,
            timeout=RUNNER_DISCONNECT_GRACE_S + 30.0,
            what="replica A to resolve the relay's reconnect grace for the deleted session",
        )
        observations["decision_after_grace_start_s"] = round(time.monotonic() - grace_started, 2)
        # The failed edge and its label upsert follow the decision row.
        time.sleep(2.0)
        log_a = stack.process_log.read_text()
        decision = stack.relay_decision(session_id)
        decision_rows = [
            line
            for line in log_a.splitlines()
            if f"Runner disconnect for session={session_id}:" in line
        ]
        failed_rows = [
            line for line in log_a.splitlines() if f"session turn failed for {session_id}" in line
        ]
        rows_after_grace = conversation_rows(stack._conversation_database_uri, session_id)
        observations.update(
            {
                "relay_decision": decision,
                "disconnect_decision_rows": decision_rows,
                "session_turn_failed_rows": failed_rows,
                "rows_after_grace": rows_after_grace,
                "replica_b_failed_rows": [
                    line
                    for line in replica_b.process_log.read_text().splitlines()
                    if f"session turn failed for {session_id}" in line
                ],
            }
        )
        (tmp_path / "observations.json").write_text(json.dumps(observations, indent=2))

        assert (
            decision != "failed_mid_turn" and not failed_rows and not rows_after_grace["labels"]
        ), (
            f"Replica A recorded a failed turn for session {session_id} after replica B "
            f"deleted it {observations['delete_after_grace_start_s']}s into A's "
            f"{RUNNER_DISCONNECT_GRACE_S:.0f}s grace.\n"
            f"Relay decision: {decision}\n"
            f"Decision rows: {decision_rows}\n"
            f"Failed-turn rows: {failed_rows}\n"
            f"Rows in the shared database after the grace: {rows_after_grace}"
        )
    finally:
        proxy.end_blackout()
        release_mock_gate(mock_llm_server_url)
        replica_b.teardown()
