"""Resolve automation provenance through spawned and independently created sessions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from omnigent.entities import Conversation
from omnigent.stores import ConversationStore
from omnigent.stores.conversation_store import (
    CREATED_BY_SESSION_LABEL_KEY,
    SCHEDULED_TASK_ORIGIN_LABEL_KEY,
    SCHEDULED_TASK_RUN_LABEL_KEY,
)


@dataclass(frozen=True)
class SessionProvenance:
    """An observed automation ancestor, or the completeness of interactive ancestry."""

    execution_source: Literal["interactive", "automation", "unknown"]
    automation_session: Conversation | None = None


def session_provenance(
    conversations: ConversationStore, session: Conversation
) -> SessionProvenance:
    """Follow both ancestry edges; missing ancestry is not proof of interactive use."""
    pending = [(session, False)]
    seen: set[str] = set()
    active: set[str] = set()
    missing = False
    while pending:
        current, finished = pending.pop()
        if finished:
            active.remove(current.id)
            continue
        if current.id in active:
            missing = True
            continue
        if current.id in seen:
            continue
        seen.add(current.id)
        active.add(current.id)
        if current.labels.get(SCHEDULED_TASK_ORIGIN_LABEL_KEY) or current.labels.get(
            SCHEDULED_TASK_RUN_LABEL_KEY
        ):
            return SessionProvenance("automation", current)
        pending.append((current, True))
        for ancestor_id in dict.fromkeys(
            (
                current.parent_conversation_id,
                current.labels.get(CREATED_BY_SESSION_LABEL_KEY),
            )
        ):
            if ancestor_id in active:
                missing = True
            if not ancestor_id or ancestor_id in seen:
                continue
            ancestor = conversations.get_conversation(ancestor_id)
            if ancestor is None:
                missing = True
            else:
                pending.append((ancestor, False))
    return SessionProvenance("unknown" if missing else "interactive")
