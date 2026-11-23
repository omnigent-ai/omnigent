"""The server's routing answer travels on the session-init envelope.

A runner holds no routing backends of its own, so this field is how it learns
whether to advertise ``sys_advise_models``.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from omnigent.entities import Conversation
from omnigent.runner.session_init_protocol import (
    SESSION_INIT_PAYLOAD_KEY,
    build_runner_session_init_payload,
    parse_runner_session_init_envelope,
)
from omnigent.runtime import _globals
from omnigent.runtime.caps import RuntimeCaps
from omnigent.server.routing_backend import RoutingBackends

SESSION_ID = "conv_smart_routing"
AGENT_ID = "ag_smart_routing"


def _conversation() -> Conversation:
    return Conversation(
        id=SESSION_ID,
        agent_id=AGENT_ID,
        runner_id="runner_test",
        created_at=0,
        updated_at=0,
        root_conversation_id=SESSION_ID,
    )


@pytest.mark.parametrize("available", [True, False])
def test_payload_carries_the_servers_routing_answer(available: bool) -> None:
    payload = build_runner_session_init_payload(
        _conversation(),
        server_version="0.0.0-test",
        smart_routing_available=available,
    )
    assert payload[SESSION_INIT_PAYLOAD_KEY]["smart_routing_available"] is available
    envelope = parse_runner_session_init_envelope(payload)
    assert envelope is not None
    assert envelope.smart_routing_available is available


@pytest.mark.parametrize("routes", [True, False])
def test_builder_stamps_the_servers_own_answer_by_default(
    monkeypatch: pytest.MonkeyPatch, routes: bool
) -> None:
    """Every server call site inherits the answer, so none can forget to state it."""
    caps = RuntimeCaps(routing_backends=RoutingBackends(local=Mock())) if routes else RuntimeCaps()
    monkeypatch.setattr(_globals, "_caps", caps)
    payload = build_runner_session_init_payload(_conversation(), server_version="0.0.0-test")
    envelope = parse_runner_session_init_envelope(payload)
    assert envelope is not None
    assert envelope.smart_routing_available is routes


def test_envelope_from_an_older_server_defaults_to_unavailable() -> None:
    """A server predating the field must not read as "routing on"."""
    payload = build_runner_session_init_payload(_conversation(), server_version="0.0.0-test")
    del payload[SESSION_INIT_PAYLOAD_KEY]["smart_routing_available"]
    envelope = parse_runner_session_init_envelope(payload)
    assert envelope is not None
    assert envelope.smart_routing_available is False
