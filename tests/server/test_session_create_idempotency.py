"""Unit tests for the public session-create idempotency binding."""

from __future__ import annotations

import pytest

from omnigent.errors import OmnigentError
from omnigent.server.schemas import SessionCreateRequest
from omnigent.server.session_create_idempotency import (
    build_session_create_idempotency,
)


def _request(**updates: object) -> SessionCreateRequest:
    payload: dict[str, object] = {"agent_id": "a" * 32}
    payload.update(updates)
    return SessionCreateRequest.model_validate(payload)


def test_binding_is_deterministic_and_scoped_to_owner() -> None:
    body = _request(title="automation shell")

    first = build_session_create_idempotency("workflow-42", body, "alice@example.com")
    replay = build_session_create_idempotency("workflow-42", body, "alice@example.com")
    other_owner = build_session_create_idempotency("workflow-42", body, "bob@example.com")

    assert first is not None
    assert first == replay
    assert other_owner is not None
    assert first.conversation_id != other_owner.conversation_id


def test_same_key_changed_request_keeps_identity_but_breaks_binding() -> None:
    first = build_session_create_idempotency(
        "workflow-42",
        _request(title="first title"),
        "alice@example.com",
    )
    changed = build_session_create_idempotency(
        "workflow-42",
        _request(title="changed title"),
        "alice@example.com",
    )

    assert first is not None and changed is not None
    assert first.conversation_id == changed.conversation_id
    assert not first.matches(changed.labels)


@pytest.mark.parametrize(
    "key",
    ["", "contains space", "line\nbreak", "\x1f", "x" * 256],
)
def test_invalid_keys_fail_closed(key: str) -> None:
    with pytest.raises(OmnigentError, match="visible ASCII"):
        build_session_create_idempotency(key, _request(), "alice@example.com")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("labels", {"client": "value"}),
        ("initial_items", [{"type": "message", "data": {"role": "user", "content": []}}]),
        ("host_id", "b" * 32),
        ("workspace", "/tmp/workspace"),
        ("model_override", "test-model"),
        ("harness_override", "openai-agents"),
    ],
)
def test_side_effecting_request_shapes_fail_closed(field: str, value: object) -> None:
    with pytest.raises(OmnigentError, match="empty top-level external session shell"):
        build_session_create_idempotency(
            "workflow-42",
            _request(**{field: value}),
            "alice@example.com",
        )


def test_missing_header_preserves_existing_non_idempotent_contract() -> None:
    assert (
        build_session_create_idempotency(
            None,
            _request(labels={"client": "value"}),
            None,
        )
        is None
    )
