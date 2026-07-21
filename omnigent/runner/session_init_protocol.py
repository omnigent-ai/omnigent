"""Versioned server-to-runner session initialization payloads."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omnigent.entities import Conversation

SESSION_INIT_PROTOCOL_VERSION = 2
SESSION_INIT_PAYLOAD_KEY = "session_init"


class RunnerSessionInitSnapshot(BaseModel):
    """Server-owned session state needed while starting a runner session."""

    model_config = ConfigDict(extra="ignore")

    created_at: int
    updated_at: int
    workspace: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    reasoning_effort: str | None = None
    model_override: str | None = None
    harness_override: str | None = None
    cost_control_mode_override: str | None = None
    terminal_launch_args: list[str] | None = None
    external_session_id: str | None = None
    parent_session_id: str | None = None
    root_session_id: str | None = None


class RunnerSessionInitEnvelope(BaseModel):
    """Metadata a current server can send instead of runner callback reads."""

    model_config = ConfigDict(extra="ignore")

    protocol_version: Literal[SESSION_INIT_PROTOCOL_VERSION]
    server_version: str
    session_id: str
    agent_id: str
    sub_agent_name: str | None = None
    snapshot: RunnerSessionInitSnapshot


def build_runner_session_init_payload(
    conversation: Conversation,
    *,
    server_version: str,
) -> dict[str, Any]:
    """Build the versioned initialization fields appended to the legacy body."""
    if conversation.agent_id is None:
        raise ValueError("runner session initialization requires an agent_id")
    envelope = RunnerSessionInitEnvelope(
        protocol_version=SESSION_INIT_PROTOCOL_VERSION,
        server_version=server_version,
        session_id=conversation.id,
        agent_id=conversation.agent_id,
        sub_agent_name=conversation.sub_agent_name,
        snapshot=RunnerSessionInitSnapshot(
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
            workspace=conversation.workspace,
            labels=conversation.labels,
            reasoning_effort=conversation.reasoning_effort,
            model_override=conversation.model_override,
            harness_override=conversation.harness_override,
            cost_control_mode_override=conversation.cost_control_mode_override,
            terminal_launch_args=conversation.terminal_launch_args,
            external_session_id=conversation.external_session_id,
            parent_session_id=conversation.parent_conversation_id,
            root_session_id=conversation.root_conversation_id,
        ),
    )
    return {
        "session_id": conversation.id,
        "agent_id": conversation.agent_id,
        "sub_agent_name": conversation.sub_agent_name,
        SESSION_INIT_PAYLOAD_KEY: envelope.model_dump(mode="json"),
    }


def parse_runner_session_init_envelope(
    body: dict[str, Any],
) -> RunnerSessionInitEnvelope | None:
    """Return a supported envelope, or ``None`` for the removable legacy path."""
    raw = body.get(SESSION_INIT_PAYLOAD_KEY)
    if not isinstance(raw, dict):
        return None
    if raw.get("protocol_version") != SESSION_INIT_PROTOCOL_VERSION:
        return None
    try:
        return RunnerSessionInitEnvelope.model_validate(raw)
    except ValidationError as exc:
        raise ValueError("invalid runner session initialization envelope") from exc
