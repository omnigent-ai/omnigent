"""Usage telemetry event dataclasses.

Each dataclass is passed to :func:`omnigent.telemetry.emit`.  The client
serialises it into the gateway wire format: ``installation_id`` becomes a
top-level ``data`` field; all remaining fields are JSON-encoded into
``data.params`` (the gateway's ``additionalProperties: false`` constraint
means only documented top-level fields are accepted).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SessionCreatedEvent:
    """Fired once when a session row is created.

    :param installation_id: Server-side installation ID (top-level in wire
        format; see :mod:`omnigent.telemetry.client`).
    :param session_id: Omnigent conversation/session identifier (goes into
        ``params``).
    :param agent_id: The agent bound to this session.
    :param harness: Harness kind, e.g. ``"claude-native"`` or ``"pi"``.
    :param surface: Client surface: ``"web"``, ``"desktop"``, ``"ios"``,
        ``"android"``, ``"cli"``, or ``"unknown"``.
    :param anon_user_id: First 16 hex chars of ``sha256("<installation_id>:<user_id>")``.
    :param host_installation_id: Installation ID of the host machine
        (``omnigent host``); ``None`` for CLI sessions.
    :param is_fork: ``True`` when the session was forked from another.
    :param is_sub_agent: ``True`` when ``sub_agent_name`` is set.
    """

    installation_id: str | None
    session_id: str
    agent_id: str | None
    harness: str | None
    surface: str | None
    anon_user_id: str | None
    host_installation_id: str | None
    is_fork: bool
    is_sub_agent: bool


@dataclass
class SessionStoppedEvent:
    """Fired after a session is successfully stopped via the runner.

    :param installation_id: Server-side installation ID.
    :param session_id: Omnigent conversation/session identifier.
    :param anon_user_id: First 16 hex chars of ``sha256("<installation_id>:<user_id>")``.
    """

    installation_id: str | None
    session_id: str
    anon_user_id: str | None


@dataclass
class SessionDeletedEvent:
    """Fired after a session row is deleted from the store.

    :param installation_id: Server-side installation ID.
    :param session_id: Omnigent conversation/session identifier.
    :param anon_user_id: First 16 hex chars of ``sha256("<installation_id>:<user_id>")``.
    :param duration_seconds: Wall-clock lifetime of the session.
    :param input_tokens: Cumulative input tokens from ``session_usage``.
    :param output_tokens: Cumulative output tokens from ``session_usage``.
    :param total_cost_usd: Cumulative cost from ``session_usage``.
    """

    installation_id: str | None
    session_id: str
    anon_user_id: str | None
    duration_seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    total_cost_usd: float | None
