"""Content-free harness and session hierarchy observations for debug reports."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable

from omnigent._wrapper_labels import (
    ACP_SUBAGENT_ID_LABEL_KEY,
    ANTIGRAVITY_NATIVE_SUBAGENT_WRAPPER_VALUE,
    WRAPPER_LABEL_KEY,
)
from omnigent.debug_logging import debug_event, debug_sink_enabled
from omnigent.entities import Conversation
from omnigent.harness_aliases import canonicalize_harness
from omnigent.harness_plugins import ANTIGRAVITY_NATIVE_CODING_AGENT
from omnigent.native.native_coding_agents import (
    NATIVE_CODING_AGENTS,
    native_coding_agent_for_wrapper_label,
)

_logger = logging.getLogger(__name__)
_NATIVE_SUBAGENT_HARNESSES = {
    agent.subagent_wrapper_label: agent.harness
    for agent in NATIVE_CODING_AGENTS
    if agent.subagent_wrapper_label is not None
}
# TODO: Remove the fallback when Antigravity declares subagent_wrapper_label.
_NATIVE_SUBAGENT_HARNESSES.setdefault(
    ANTIGRAVITY_NATIVE_SUBAGENT_WRAPPER_VALUE, ANTIGRAVITY_NATIVE_CODING_AGENT.harness
)


def harness_attributes(harness: str | None, *, source: str) -> dict[str, str | None]:
    """Keep deferred selections out of the concrete harness dimension."""
    harness = canonicalize_harness(harness)
    deferred = harness in {"auto", "any"}
    return {
        "harness": None if deferred else harness or None,
        "harness_source": source,
        "harness_resolution": "deferred" if deferred else "resolved" if harness else "unknown",
    }


def log_session_metadata(
    conv: Conversation,
    *,
    observation: str,
    resolve_harness: Callable[[], str | None],
) -> None:
    """Observe an active session using an already-authorized conversation.

    Call from a worker thread: the resolver may load the session's agent spec.
    Native mirrors use their own wrapper identity, never the parent's current
    spec. Lookup and logging failures must not interrupt session execution.

    A resolver returning ``None`` produces ``agent_spec/unknown``, including
    failures it handles internally. Only escaping exceptions are ``lookup_failed``.
    """
    if not debug_sink_enabled():
        return
    harness = None
    source = "agent_spec"
    lookup_error_type = None
    wrapper = conv.labels.get(WRAPPER_LABEL_KEY)
    native_agent = native_coding_agent_for_wrapper_label(wrapper)
    if conv.kind == "sub_agent" and wrapper is not None and wrapper in _NATIVE_SUBAGENT_HARNESSES:
        harness = _NATIVE_SUBAGENT_HARNESSES[wrapper]
        source = "native_subagent"
    elif conv.kind == "sub_agent" and conv.labels.get(ACP_SUBAGENT_ID_LABEL_KEY):
        # ACP mirrors share a parent agent ID without proving a concrete harness.
        source = "acp_subagent"
    elif conv.harness_override:
        harness = conv.harness_override
        source = "session_override"
    elif native_agent is not None:
        harness = native_agent.harness
        source = "native_wrapper"
    elif conv.agent_id is None:
        source = "missing_agent"
    else:
        try:
            harness = resolve_harness()
        except Exception as exc:  # noqa: BLE001 — optional lookups must not break execution
            source = "lookup_failed"
            lookup_error_type = type(exc).__name__
    with contextlib.suppress(Exception):
        _logger.info(
            "Session metadata observed",
            extra=debug_event(
                "session_metadata",
                session_id=conv.id,
                runner_id=conv.runner_id,
                agent_id=conv.agent_id,
                session_kind=conv.kind,
                parent_session_id=conv.parent_conversation_id,
                root_session_id=conv.root_conversation_id,
                observation=observation,
                harness_lookup_error_type=lookup_error_type,
                **harness_attributes(harness, source=source),
            ),
        )
