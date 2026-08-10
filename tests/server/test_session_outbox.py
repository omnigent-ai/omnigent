"""Tests for the durable session-lifecycle-event producer.

Covers ``omnigent.server.session_outbox`` against a real
``SqlAlchemySessionLifecycleStore`` (SQLite) — the point of these tests is
the redaction/allowlist boundary (§3.2 of
``docs/architecture/2026-08-10-durable-session-lifecycle-push.md``), which
only a real round-trip through JSON serialization can prove.
"""

from __future__ import annotations

import json

import pytest

from omnigent.db.db_models import LABEL_VALUE_MAX_LEN
from omnigent.server import session_outbox
from omnigent.stores.session_lifecycle_store.sqlalchemy_store import (
    SqlAlchemySessionLifecycleStore,
)

SESSION_ID = "a1" * 16
ELICITATION_ID = "e1" * 16


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemySessionLifecycleStore:
    """Wire a real session-lifecycle store into the module; unwire on teardown."""
    real_store = SqlAlchemySessionLifecycleStore(db_uri)
    session_outbox.configure(real_store)
    yield real_store
    session_outbox.configure(None)


def _outbox_row_for(store: SqlAlchemySessionLifecycleStore, session_id: str, event_type: str):
    deliveries, _ = store.list_deliveries(session_id, limit=100)
    matches = [d for d in deliveries if d.event_type == event_type]
    assert len(matches) == 1, f"expected exactly one {event_type} row, got {len(matches)}"
    return matches[0]


# ── record_session_completed ────────────────────────────────────


def test_record_session_completed_writes_row(store: SqlAlchemySessionLifecycleStore) -> None:
    session_outbox.record_session_completed(
        workspace_id=0, session_id=SESSION_ID, response_id="resp1"
    )
    row = _outbox_row_for(store, SESSION_ID, "session.completed")
    assert row.transition_key == "turn:resp1:completed"
    payload = json.loads(row.payload)
    assert payload["response_id"] == "resp1"


def test_record_session_completed_deterministic_event_id_and_idempotent(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    id_a = session_outbox._deterministic_event_id(0, SESSION_ID, "turn:resp1:completed")
    id_b = session_outbox._deterministic_event_id(0, SESSION_ID, "turn:resp1:completed")
    assert id_a == id_b

    session_outbox.record_session_completed(
        workspace_id=0, session_id=SESSION_ID, response_id="resp1"
    )
    session_outbox.record_session_completed(
        workspace_id=0, session_id=SESSION_ID, response_id="resp1"
    )
    deliveries, _ = store.list_deliveries(SESSION_ID, limit=100)
    assert len(deliveries) == 1
    assert deliveries[0].id == id_a


def test_record_session_completed_noop_when_unconfigured() -> None:
    session_outbox.configure(None)
    # Must not raise even though nothing is wired.
    session_outbox.record_session_completed(
        workspace_id=0, session_id=SESSION_ID, response_id="resp1"
    )


# ── record_session_failed ───────────────────────────────────────


def test_record_session_failed_writes_row(store: SqlAlchemySessionLifecycleStore) -> None:
    session_outbox.record_session_failed(
        workspace_id=0,
        session_id=SESSION_ID,
        response_id="resp1",
        error_code="runner_error",
        error_message="turn setup failed",
    )
    row = _outbox_row_for(store, SESSION_ID, "session.failed")
    assert row.transition_key == "turn:resp1:failed"
    payload = json.loads(row.payload)
    assert payload["response_id"] == "resp1"
    assert payload["reason"]["code"] == "runner_error"
    assert payload["reason"]["message"] == "turn setup failed"


def test_record_session_failed_truncates_long_message(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    long_message = "x" * (LABEL_VALUE_MAX_LEN + 500)
    session_outbox.record_session_failed(
        workspace_id=0,
        session_id=SESSION_ID,
        response_id="resp1",
        error_code="runner_error",
        error_message=long_message,
    )
    row = _outbox_row_for(store, SESSION_ID, "session.failed")
    payload = json.loads(row.payload)
    message = payload["reason"]["message"]
    assert len(message) == LABEL_VALUE_MAX_LEN
    assert message.endswith("…")
    assert message != long_message


def test_record_session_failed_denylist_is_key_based_not_value_based(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    """``_redact_denylist`` redacts by DICT KEY, not by scanning the value's
    contents. Neither "code" nor "message" matches
    ``_REDACT_KEY_SUBSTRINGS`` (token/secret/password/authorization/
    credential/api_key/apikey), so a message that happens to CONTAIN a
    denylisted word (e.g. "secret") passes through unredacted — this is
    real, current behavior: the denylist here is a defensive
    belt-and-suspenders pass on the {"code","message"} dict's own keys, not
    the primary protection (that's the allowlist for awaiting_decision/
    resumed payloads, tested below). Only the length clamp actually bounds
    this path.
    """
    session_outbox.record_session_failed(
        workspace_id=0,
        session_id=SESSION_ID,
        response_id="resp1",
        error_code="runner_error",
        error_message="leaked the secret token abc123",
    )
    row = _outbox_row_for(store, SESSION_ID, "session.failed")
    payload = json.loads(row.payload)
    # Confirms the real (key-based, not value-based) behavior.
    assert payload["reason"]["message"] == "leaked the secret token abc123"
    assert payload["reason"]["code"] == "runner_error"


def test_record_session_failed_noop_when_unconfigured() -> None:
    session_outbox.configure(None)
    session_outbox.record_session_failed(
        workspace_id=0,
        session_id=SESSION_ID,
        response_id="resp1",
        error_code="x",
        error_message="y",
    )


# ── record_elicitation_raised: the allowlist boundary ───────────


def test_record_elicitation_raised_allowlist_excludes_raw_env_and_content_preview(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    """Acceptance-test row: "a field outside the allowlist (e.g. raw env
    dict on the harness side) is asserted absent from the payload."

    ``ElicitationRequestParams`` has ``extra="allow"`` (MCP passthrough), so
    an unrecognized field must be structurally excluded by an allowlist,
    not filtered by key-name recognition. This test proves that both the
    durable ``session_elicitations.request_payload`` and the
    ``session.awaiting_decision`` outbox event's ``data.request`` are built
    from the same allowlist.
    """
    request = {
        # Allowlisted fields.
        "message": "Approve running 'rm -rf /tmp/cache'?",
        "mode": "form",
        "requestedSchema": {"type": "object", "properties": {"approve": {"type": "boolean"}}},
        "phase": "pre_tool_use",
        "policy_name": "approve_shell_commands",
        "target_session_id": None,  # falsy -> omitted regardless
        "tool_name": "Bash",
        # NOT allowlisted — must never reach persisted storage. Uses marker
        # strings distinct from the allowlisted "message" text above, so the
        # leakage check below can't false-positive on legitimately-allowed
        # content that happens to mention the same command.
        "tool_input": {
            "command": "rm -rf /tmp/cache",
            "env": {"AWS_SECRET_ACCESS_KEY": "shh-marker-1", "PATH": "/usr/bin"},
        },
        "content_preview": "leaked-marker-2 stuff that should not be stored",
        "url": None,  # falsy allowlisted field -> also omitted
    }
    session_outbox.record_elicitation_raised(
        workspace_id=0,
        session_id=SESSION_ID,
        elicitation_id=ELICITATION_ID,
        request=request,
    )

    elicitation = store.get_elicitation(ELICITATION_ID)
    assert elicitation is not None
    request_payload = json.loads(elicitation.request_payload)

    outbox_row = _outbox_row_for(store, SESSION_ID, "session.awaiting_decision")
    outbox_payload = json.loads(outbox_row.payload)
    outbox_request = outbox_payload["request"]

    for payload in (request_payload, outbox_request):
        assert payload["message"] == "Approve running 'rm -rf /tmp/cache'?"
        assert payload["mode"] == "form"
        assert payload["requestedSchema"] == {
            "type": "object",
            "properties": {"approve": {"type": "boolean"}},
        }
        assert payload["phase"] == "pre_tool_use"
        assert payload["policy_name"] == "approve_shell_commands"
        assert payload["tool_name"] == "Bash"
        # The core assertion: disallowed fields are structurally absent.
        assert "tool_input" not in payload
        assert "content_preview" not in payload
        # Also confirm no nested leakage anywhere in the serialized payload.
        serialized = json.dumps(payload)
        assert "AWS_SECRET_ACCESS_KEY" not in serialized
        assert "shh-marker-1" not in serialized
        assert "leaked-marker-2" not in serialized


def test_record_elicitation_raised_idempotent(store: SqlAlchemySessionLifecycleStore) -> None:
    request = {"message": "Approve?", "tool_name": "Bash"}
    session_outbox.record_elicitation_raised(
        workspace_id=0, session_id=SESSION_ID, elicitation_id=ELICITATION_ID, request=request
    )
    session_outbox.record_elicitation_raised(
        workspace_id=0, session_id=SESSION_ID, elicitation_id=ELICITATION_ID, request=request
    )
    deliveries, _ = store.list_deliveries(SESSION_ID, limit=100)
    awaiting = [d for d in deliveries if d.event_type == "session.awaiting_decision"]
    assert len(awaiting) == 1


# ── record_decision: the allowlist boundary on the decision side ─


def test_record_decision_allowlist_excludes_raw_headers(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    session_outbox.record_elicitation_raised(
        workspace_id=0,
        session_id=SESSION_ID,
        elicitation_id=ELICITATION_ID,
        request={"message": "Approve?"},
    )
    decision = {
        "action": "accept",
        "content": {"approve": True},
        "raw_headers": {"Authorization": "Bearer secret-token"},
    }
    session_outbox.record_decision(
        elicitation_id=ELICITATION_ID, decision=decision, decided_by="manager@example.com"
    )

    elicitation = store.get_elicitation(ELICITATION_ID)
    assert elicitation is not None
    assert elicitation.status == "decided"
    assert elicitation.decided_by == "manager@example.com"
    decision_payload = json.loads(elicitation.decision_payload)
    assert decision_payload["action"] == "accept"
    assert decision_payload["content"] == {"approve": True}
    assert "raw_headers" not in decision_payload
    assert "Bearer secret-token" not in json.dumps(decision_payload)


# ── record_elicitation_resumed ───────────────────────────────────


def test_record_elicitation_resumed_after_decision(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    session_outbox.record_elicitation_raised(
        workspace_id=0,
        session_id=SESSION_ID,
        elicitation_id=ELICITATION_ID,
        request={"message": "Approve?"},
    )
    session_outbox.record_decision(
        elicitation_id=ELICITATION_ID,
        decision={"action": "accept", "content": None},
        decided_by=None,
    )

    resumed = session_outbox.record_elicitation_resumed(
        workspace_id=0, elicitation_id=ELICITATION_ID
    )
    assert resumed is True

    outbox_row = _outbox_row_for(store, SESSION_ID, "session.resumed")
    payload = json.loads(outbox_row.payload)
    assert payload["elicitation_id"] == ELICITATION_ID
    assert payload["decision"]["action"] == "accept"

    elicitation = store.get_elicitation(ELICITATION_ID)
    assert elicitation is not None
    assert elicitation.status == "delivered_to_runner"
    assert elicitation.resolved_at is not None

    # Idempotent: a second call (no new decision) is a no-op.
    resumed_again = session_outbox.record_elicitation_resumed(
        workspace_id=0, elicitation_id=ELICITATION_ID
    )
    assert resumed_again is False
    deliveries, _ = store.list_deliveries(SESSION_ID, limit=100)
    resumed_rows = [d for d in deliveries if d.event_type == "session.resumed"]
    assert len(resumed_rows) == 1


def test_record_elicitation_resumed_without_decision_is_noop(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    session_outbox.record_elicitation_raised(
        workspace_id=0,
        session_id=SESSION_ID,
        elicitation_id=ELICITATION_ID,
        request={"message": "Approve?"},
    )
    # Never decided — still "pending".
    resumed = session_outbox.record_elicitation_resumed(
        workspace_id=0, elicitation_id=ELICITATION_ID
    )
    assert resumed is False
    deliveries, _ = store.list_deliveries(SESSION_ID, limit=100)
    resumed_rows = [d for d in deliveries if d.event_type == "session.resumed"]
    assert resumed_rows == []
