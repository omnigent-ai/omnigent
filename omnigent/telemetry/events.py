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
    :param agent_name: Agent name for known multi-agent orchestrators
        (e.g. ``"polly"``, ``"debby"``); ``None`` for all other agents to
        avoid leaking user-defined agent names.
    :param routing_enabled: ``True`` when smart routing is on for this
        session at creation time.
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
    agent_name: str | None = None
    routing_enabled: bool = False


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


@dataclass
class TurnEndEvent:
    """Fired at the end of each LLM turn.

    Emitted on two paths:

    - **Relay / SDK harnesses**: once per terminal ``response.*`` event
      (``completed``, ``failed``, ``cancelled``, ``incomplete``).
      ``latency_ms`` and token fields are populated when available.
    - **Native harnesses** (claude-native, codex-native): once per
      ``external_session_status`` event with ``status`` in
      ``{"idle", "failed"}``.  ``latency_ms`` and token fields are
      always ``None`` on this path (native harnesses report cumulative
      totals separately via ``SessionDeletedEvent``).

    Token fields are only present on ``status="completed"`` relay turns;
    they are ``None`` for all other statuses and for the native path.

    :param installation_id: Server-side installation ID.
    :param session_id: Omnigent conversation/session identifier.
    :param status: Terminal status of the turn: ``"completed"``,
        ``"failed"``, ``"cancelled"``, or ``"incomplete"``.
    :param latency_ms: Wall-clock turn duration in milliseconds from
        ``response.in_progress`` to the terminal event.  ``None`` when
        the start timestamp was not captured (e.g. stream reconnect).
    :param model: LLM model name, e.g. ``"claude-3-7-sonnet-20250219"``.
        ``None`` when not reported by the harness.
    :param input_tokens: Input tokens for this turn (per-turn delta).
        ``None`` for non-completed turns or when not reported.
    :param output_tokens: Output tokens for this turn (per-turn delta).
        ``None`` for non-completed turns or when not reported.
    :param cost_usd: Cost for this turn in USD (per-turn delta).
        ``None`` when the model is unpriced or the turn did not complete.
    """

    installation_id: str | None
    session_id: str
    status: str
    latency_ms: float | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None


@dataclass
class NativeSessionUsageEvent:
    """Fired on each ``external_session_usage`` flush from a native harness.

    Native harnesses (claude-native, codex-native) report *cumulative*
    session totals rather than per-turn deltas, so this event carries
    running totals at the time of the flush.  Multiple events per session
    are expected; consumers should take the last one (or compute deltas
    between consecutive events) to derive per-turn spend.

    :param installation_id: Server-side installation ID.
    :param session_id: Omnigent conversation/session identifier.
    :param input_tokens: Cumulative input tokens at time of flush.
        ``None`` when not reported in this flush.
    :param output_tokens: Cumulative output tokens at time of flush.
        ``None`` when not reported in this flush.
    :param cost_usd: Cumulative cost in USD at time of flush.
        ``None`` when the session is unpriced or not reported.
    :param model: LLM model name forwarded by the harness, e.g.
        ``"claude-opus-4-5"``.  ``None`` when not reported.
    """

    installation_id: str | None
    session_id: str
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    model: str | None


@dataclass
class PolicyRegisteredEvent:
    """Fired when a policy is created via the API.

    Covers both session-level (``POST /v1/sessions/{id}/policies``) and
    admin-level (``POST /v1/policies``) creation.

    :param installation_id: Server-side installation ID.
    :param handler: Registered handler path (e.g.
        ``"omnigent.policies.blast_radius"``) or HTTPS endpoint URL for
        ``type="url"`` policies.
    :param policy_type: Handler type: ``"python"`` or ``"url"``.
    :param scope: ``"session"`` for per-session policies, ``"admin"`` for
        server-wide default policies.
    :param session_id: Owning session identifier for scope ``"session"``;
        ``None`` for admin policies.
    :param anon_user_id: First 16 hex chars of ``sha256("<installation_id>:<user_id>")``.
    """

    installation_id: str | None
    handler: str
    policy_type: str
    scope: str
    session_id: str | None
    anon_user_id: str | None


@dataclass
class PolicyDeletedEvent:
    """Fired when a policy is deleted via the API.

    Covers both session-level (``DELETE /v1/sessions/{id}/policies/{pid}``)
    and admin-level (``DELETE /v1/policies/{pid}``) deletion.

    :param installation_id: Server-side installation ID.
    :param scope: ``"session"`` for per-session policies, ``"admin"`` for
        server-wide default policies.
    :param session_id: Owning session identifier for scope ``"session"``;
        ``None`` for admin policies.
    :param anon_user_id: First 16 hex chars of ``sha256("<installation_id>:<user_id>")``.
    """

    installation_id: str | None
    scope: str
    session_id: str | None
    anon_user_id: str | None
