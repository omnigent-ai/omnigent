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
    """
    Cross-vendor review: the delivered ``reason.message`` is a fixed, safe
    string selected by ``error_code`` — never a passthrough of
    ``error_message`` (see :data:`session_outbox._SAFE_FAILURE_MESSAGES`
    and :func:`session_outbox.record_session_failed`'s docstring). Free-form
    diagnostic text like ``error_message`` here can incidentally embed
    secrets and this payload is HMAC-signed and delivered externally.
    """
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
    assert payload["reason"]["message"] == "The runner reported an error."
    assert "turn setup failed" not in row.payload


def test_record_session_failed_unrecognized_code_falls_back_to_generic_message(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    """An ``error_code`` with no entry in the safe-message map still never
    leaks ``error_message`` — it falls back to the fully generic message,
    not an unsafe default."""
    session_outbox.record_session_failed(
        workspace_id=0,
        session_id=SESSION_ID,
        response_id="resp1",
        error_code="some_unmapped_future_code",
        error_message="anything at all, even secret-shaped text",
    )
    row = _outbox_row_for(store, SESSION_ID, "session.failed")
    payload = json.loads(row.payload)
    assert payload["reason"]["code"] == "some_unmapped_future_code"
    assert payload["reason"]["message"] == session_outbox._DEFAULT_SAFE_FAILURE_MESSAGE
    assert "anything at all" not in row.payload


def test_record_session_failed_message_length_never_depends_on_error_message(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    """The delivered message is a fixed short safe string regardless of how
    long (or short) ``error_message`` is — proving the fix genuinely
    decouples the delivered payload from the raw diagnostic text's size,
    not just its content."""
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
    assert message == "The runner reported an error."
    assert message != long_message
    assert "x" * 100 not in row.payload


def test_record_session_failed_never_leaks_secret_shaped_error_message(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    """
    Cross-vendor review P1: a message that DELIBERATELY embeds
    secret-shaped strings (a fake API key, a bearer token, a DB connection
    string) — the exact shape of a provider error, subprocess stderr, or a
    runner-forwarded, structurally-unvalidated ``error`` dict — must NEVER
    reach the persisted outbox row (and therefore never the HMAC-signed,
    externally-delivered webhook payload). ``_redact_denylist`` alone
    (KEY-based, not value-based) could never catch this; the fix is that
    ``error_message`` is simply never used to build the delivered message
    at all.
    """
    fake_api_key = "sk-live-abc123FAKEKEYdeadbeefCAFEBABE"
    fake_bearer_token = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.FAKE.TOKEN"
    fake_db_conn_string = (
        "postgresql://admin:SuperSecretPassw0rd@internal-db.example.com:5432/prod"
    )
    malicious_message = (
        f"provider error: api_key={fake_api_key} "
        f"auth_header={fake_bearer_token} "
        f"connection_failed to {fake_db_conn_string}"
    )
    session_outbox.record_session_failed(
        workspace_id=0,
        session_id=SESSION_ID,
        response_id="resp1",
        error_code="runner_error",
        error_message=malicious_message,
    )
    row = _outbox_row_for(store, SESSION_ID, "session.failed")
    payload = json.loads(row.payload)
    assert payload["reason"]["message"] == "The runner reported an error."
    assert payload["reason"]["code"] == "runner_error"
    # None of the embedded secrets, nor any fragment of the raw message,
    # appear anywhere in the persisted (and therefore delivered) row.
    assert fake_api_key not in row.payload
    assert fake_bearer_token not in row.payload
    assert fake_db_conn_string not in row.payload
    assert "SuperSecretPassw0rd" not in row.payload
    assert malicious_message not in row.payload


def test_record_session_failed_denylist_is_key_based_not_value_based(
    store: SqlAlchemySessionLifecycleStore,
) -> None:
    """``_redact_denylist`` itself still redacts by DICT KEY, not by
    scanning value contents — this is documented, narrow, defensive
    belt-and-suspenders behavior on the ``{"code","message"}`` dict's own
    keys. It is NOT what protects ``session.failed`` from leaking secrets
    (that's the allowlisted safe-message map tested above); confirming
    this narrower fact separately so a future denylist change doesn't
    silently assume it does more than it does.
    """
    from omnigent.server.session_outbox import _redact_denylist

    result = _redact_denylist({"code": "runner_error", "message": "leaked the secret token"})
    assert result == {"code": "runner_error", "message": "leaked the secret token"}


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
