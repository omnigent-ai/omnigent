"""Session-scoped, non-human runner capability authentication and authorization.

A runner callback is authenticated by proving possession of a per-launch binding
token whose derived runner ID matches the conversation's current ``runner_id``.
The resulting :class:`RunnerPrincipal` carries no human identity — it cannot be
mistaken for a user or host owner. Authorization is a separate, explicit
action allow-list so a valid runner token can never widen into general edit
authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from omnigent.runner.identity import token_bound_runner_id
from omnigent.stores.conversation_store import ConversationStore


class RunnerAction(str, Enum):
    """Closed set of operations a runner principal may perform."""

    READ_SESSION = "read_session"
    READ_SESSION_SPEC = "read_session_spec"
    APPEND_EVENT = "append_event"
    REPORT_USAGE = "report_usage"
    EVALUATE_POLICY = "evaluate_policy"
    # Dispatch the session's own MCP tool calls through the server MCP
    # proxy. Needed when the runner's inherited credential is not the
    # session owner (in-app / shared host, where session owner differs
    # from host owner) and so cannot pass a human EDIT check. Server-side
    # TOOL_CALL / TOOL_RESULT policy still runs on every proxied call, so
    # this never widens a runner into general edit authority.
    PROXY_MCP = "proxy_mcp"


_ALLOWED_ACTIONS: frozenset[RunnerAction] = frozenset(RunnerAction)


@dataclass(frozen=True)
class RunnerPrincipal:
    """Immutable, non-human authorization context for one runner session.

    Deliberately carries no user ID, permission level, host owner, or raw token.
    """

    runner_id: str
    conversation_id: str


def authenticate_runner(
    binding_token: str | None,
    conversation_id: str,
    conversation_store: ConversationStore,
) -> RunnerPrincipal | None:
    """Resolve a runner principal from a binding token, or ``None``.

    Fails closed for missing, blank, mismatched, or superseded tokens without
    revealing which check failed. The raw token is never logged or persisted.

    :param binding_token: Opaque per-launch secret from the runner request
        header. ``None`` or whitespace produces no principal.
    :param conversation_id: Target session identifier from the route.
    :param conversation_store: Store used to read the conversation's current
        bound runner.
    :returns: A :class:`RunnerPrincipal` only when the token-derived runner ID
        exactly matches the conversation's current ``runner_id``; otherwise
        ``None``.
    """
    if not binding_token or not binding_token.strip():
        return None
    try:
        derived_runner_id = token_bound_runner_id(binding_token)
    except RuntimeError:
        return None

    conversation = conversation_store.get_conversation(conversation_id)
    if conversation is None or conversation.runner_id is None:
        return None
    if conversation.runner_id != derived_runner_id:
        return None
    return RunnerPrincipal(runner_id=derived_runner_id, conversation_id=conversation_id)


def runner_allows(
    principal: RunnerPrincipal,
    conversation_id: str,
    action: RunnerAction | str,
) -> bool:
    """Return ``True`` only when *action* is an allowed runner callback.

    Rejects raw strings that happen to match an action value — routes must pass
    a declared :class:`RunnerAction` member. Unknown or future actions fail
    closed.

    :param principal: A previously authenticated runner principal.
    :param conversation_id: Target session identifier from the route; must
        match the principal's bound conversation.
    :param action: Runner action selected by the route, never by client input.
    """
    if principal.conversation_id != conversation_id:
        return False
    if not isinstance(action, RunnerAction):
        return False
    return action in _ALLOWED_ACTIONS
