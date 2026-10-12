"""Tests for smart_routing_available in the session-init protocol.

The routing backends are built in the server process, so a runner attached to a
remote server has none of its own. The server reports what it can answer with
on the session-init envelope, and the runner records it on its caps — otherwise
every runner reads routing as off and hides ``sys_advise_models`` from sessions
whose server routes.
"""

from __future__ import annotations

import pytest

from omnigent.entities import Conversation
from omnigent.runner.session_init_protocol import (
    SESSION_INIT_PAYLOAD_KEY,
    build_runner_session_init_payload,
    parse_runner_session_init_envelope,
)
from omnigent.runtime import get_caps, set_remote_routing_available

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


def test_envelope_from_an_older_server_defaults_to_unavailable() -> None:
    """A server predating the field must not read as "routing on"."""
    payload = build_runner_session_init_payload(_conversation(), server_version="0.0.0-test")
    del payload[SESSION_INIT_PAYLOAD_KEY]["smart_routing_available"]
    envelope = parse_runner_session_init_envelope(payload)
    assert envelope is not None
    assert envelope.smart_routing_available is False


def test_set_remote_routing_available_lands_on_the_process_caps() -> None:
    previous = get_caps().remote_routing_available
    try:
        set_remote_routing_available(True)
        assert get_caps().remote_routing_available is True
        set_remote_routing_available(False)
        assert get_caps().remote_routing_available is False
    finally:
        set_remote_routing_available(previous)
