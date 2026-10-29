"""Immutable provider references stored in existing conversation labels."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from omnigent.db.db_models import current_workspace_id
from omnigent.onboarding.sandboxes.profiles import ProviderSandboxProfile

if TYPE_CHECKING:
    from omnigent.stores.conversation_store import ConversationStore

PROFILE_LABEL_PREFIX = "sandbox_profile:"
_FIELDS = ("provider", "id", "revision", "user_id", "name")


def reject_profile_labels(labels: Mapping[str, str]) -> None:
    if any(key.startswith(PROFILE_LABEL_PREFIX) for key in labels):
        raise ValueError("Sandbox profile labels are server-owned and cannot be edited")


def same_profile_identity(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(
        key in left and key in right and left[key] == right[key]
        for key in ("id", "revision", "provider", "user_id", "workspace_id")
    )


def profile_labels(snapshot: dict[str, Any]) -> dict[str, str]:
    public = ProviderSandboxProfile.model_validate(
        {key: snapshot[key] for key in ("id", "revision", "name", "description")}
    )
    if snapshot["workspace_id"] != current_workspace_id():
        raise ValueError("Sandbox profile belongs to another workspace")
    values = {key: snapshot[key] for key in _FIELDS}
    if any(not isinstance(value, str) or not 1 <= len(value) <= 256 for value in values.values()):
        raise ValueError("Sandbox profile fields must fit within the label value limit")
    # Preserve the description without truncating the existing 256-char column.
    values.update(
        {"description:0": public.description[:250], "description:1": public.description[250:]}
    )
    return {PROFILE_LABEL_PREFIX + key: value for key, value in values.items()}


def profile_from_labels(labels: Mapping[str, str]) -> dict[str, Any] | None:
    values = {
        key.removeprefix(PROFILE_LABEL_PREFIX): value
        for key, value in labels.items()
        if key.startswith(PROFILE_LABEL_PREFIX)
    }
    if not values:
        return None
    if any(key not in values for key in _FIELDS):
        raise ValueError("Incomplete saved sandbox profile")
    snapshot: dict[str, Any] = {key: values[key] for key in _FIELDS}
    snapshot.update(
        description=values.get("description:0", "") + values.get("description:1", ""),
        workspace_id=current_workspace_id(),
    )
    profile_labels(snapshot)
    return snapshot


def public_profile(labels: Mapping[str, str]) -> dict[str, Any] | None:
    snapshot = profile_from_labels(labels)
    return (
        {key: value for key, value in snapshot.items() if key not in ("user_id", "workspace_id")}
        if snapshot
        else None
    )


def encode_snapshot(value: dict[str, Any]) -> bytes:
    profile_labels(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class SandboxProfileStore:
    """Session references; host identity remains in conversation metadata."""

    def __init__(self, conversations: ConversationStore) -> None:
        self.conversations = conversations

    def bind_session(self, session_id: str, snapshot: dict[str, Any]) -> None:
        self.conversations.accept_sandbox_profile(session_id, snapshot)

    def get_session_snapshot(self, session_id: str) -> dict[str, Any] | None:
        conversation = self.conversations.get_conversation(session_id)
        return profile_from_labels(conversation.labels) if conversation else None

    def can_bind_host(self, session_id: str, host_id: str) -> bool:
        return self.conversations.can_bind_profile_host(session_id, host_id)

    def get_session_host_id(self, session_id: str) -> str | None:
        conversation = self.conversations.get_conversation(session_id)
        return (
            conversation.host_id
            if conversation and profile_from_labels(conversation.labels)
            else None
        )

    def get_host_snapshot(self, host_id: str) -> dict[str, Any] | None:
        after = None
        accepted = None
        while True:
            ids = self.conversations.list_host_conversation_ids(host_id, after=after, limit=100)
            for session_id in ids:
                snapshot = self.get_session_snapshot(session_id)
                if snapshot is not None:
                    if accepted is not None and not same_profile_identity(accepted, snapshot):
                        raise ValueError("Sessions on this host have conflicting sandbox profiles")
                    accepted = snapshot
            if len(ids) < 100:
                return accepted
            after = ids[-1]
