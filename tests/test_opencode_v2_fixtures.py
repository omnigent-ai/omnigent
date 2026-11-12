"""Fixture-integrity tests for tests/fixtures/opencode_v2/.

The fixtures are committed, not generated in CI: they were captured from a
real ``opencode serve`` 2.0.18 and must be recaptured when the OpenCode wire
protocol this harness targets changes. ``openapi.json`` is trimmed to the routes
the client calls and the schemas those routes reference. This module fails loudly if they are
missing or malformed, so the harness tests never run against nothing.
"""

from __future__ import annotations

import inspect
import json
import re

from omnigent.harnesses.opencode_native import client as opencode_client
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


def test_every_client_route_exists_in_the_captured_spec() -> None:
    """The fixture keeps only the routes the client calls; each must be a real 2.0.18 route."""
    payload = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))

    def _shape(route: str) -> str:
        return re.sub(r"\{[^}]+\}", "{}", route)

    client_source = inspect.getsource(opencode_client)
    client_routes = {_shape(route) for route in re.findall(r'"(/api/[^"]*)"', client_source)}
    spec_routes = {_shape(route) for route in payload["paths"]}
    assert client_routes, "no /api routes found in the client source"
    assert client_routes <= spec_routes, sorted(client_routes - spec_routes)


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
