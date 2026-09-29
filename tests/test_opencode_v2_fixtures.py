"""Fixture-integrity tests for tests/fixtures/opencode_v2/.

The fixtures are committed, not generated in CI: they were captured from a
real ``opencode serve`` 2.0.18 and must be recaptured when the OpenCode wire
protocol this harness targets changes. This module fails loudly if they are
missing or malformed, so the harness tests never run against nothing.
"""

from __future__ import annotations

import json

from tests.opencode_v2_fixtures import (
    EVENTS_PATH,
    MESSAGES_PATH,
    OPENAPI_PATH,
    events_of_type,
    load_events,
    load_messages,
)

_REQUIRED_EVENT_TYPES = [
    "session.text.delta",
    "session.reasoning.delta",
    "session.tool.called",
    "permission.asked",
    "permission.replied",
    "form.created",
    "form.replied",
    "session.compaction.started",
    "session.compaction.ended",
    "session.usage.updated",
]


def test_fixture_files_exist() -> None:
    missing = [path for path in (OPENAPI_PATH, EVENTS_PATH, MESSAGES_PATH) if not path.is_file()]
    assert not missing, (
        f"Missing OpenCode v2 wire fixtures: {missing}. Recapture them from a "
        "real `opencode serve` 2.0.x (GET /openapi.json, the /api/event stream of one "
        "turn, and GET /api/session/{id}/message) into tests/fixtures/opencode_v2/."
    )


def test_openapi_fixture_parses_and_has_expected_paths() -> None:
    payload = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    assert "/api/session/{sessionID}/prompt" in payload["paths"]
    assert "/api/session/{sessionID}/permission/{requestID}/reply" in payload["paths"]


def test_events_fixture_has_required_types() -> None:
    events = load_events()
    assert events, "events.ndjson is empty"
    seen_types = {event["type"] for event in events}
    missing_types = [t for t in _REQUIRED_EVENT_TYPES if t not in seen_types]
    assert not missing_types, f"events.ndjson is missing event types: {missing_types}"


def test_events_of_type_filters_by_type() -> None:
    deltas = events_of_type("session.text.delta")
    assert deltas
    assert all(event["type"] == "session.text.delta" for event in deltas)
    assert events_of_type("no.such.type") == []


def test_messages_fixture_parses_as_session_messages_response() -> None:
    messages = load_messages()
    assert "data" in messages
    assert "cursor" in messages
    assert isinstance(messages["data"], list)
