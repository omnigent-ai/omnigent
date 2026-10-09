"""The session-init envelope advertises the server's additive capabilities."""

from __future__ import annotations

from omnigent.entities import Conversation
from omnigent.runner.session_init_protocol import (
    SERVER_CAPABILITY_DURABLE_NOTICES,
    SESSION_INIT_PAYLOAD_KEY,
    build_runner_session_init_payload,
    parse_runner_session_init_envelope,
)


def _conversation() -> Conversation:
    return Conversation(
        id="79b22ebd2309e48fdeb450c65611d51b",
        created_at=1,
        updated_at=1,
        root_conversation_id="79b22ebd2309e48fdeb450c65611d51b",
        agent_id="087b7cb7ac30abf4debfaa578d052ec6",
    )


def test_current_server_advertises_durable_notices() -> None:
    body = build_runner_session_init_payload(_conversation(), server_version="0.18.0.dev0")
    envelope = parse_runner_session_init_envelope(body)
    assert envelope is not None
    assert SERVER_CAPABILITY_DURABLE_NOTICES in envelope.server_capabilities


def test_envelope_without_capabilities_advertises_none() -> None:
    """An older server's envelope omits the field: nothing is supported."""
    body = build_runner_session_init_payload(_conversation(), server_version="0.17.0")
    raw = body[SESSION_INIT_PAYLOAD_KEY]
    assert isinstance(raw, dict)
    raw.pop("server_capabilities")
    envelope = parse_runner_session_init_envelope(body)
    assert envelope is not None
    assert envelope.server_capabilities == []
