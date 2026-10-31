"""Immutable provider references stored in existing conversation labels."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from omnigent.db.db_models import current_workspace_id, uuid_to_bytes
from omnigent.onboarding.sandboxes.profiles import ProviderSandboxProfile
from omnigent.stores.conversation_store import ConversationNotFoundError

if TYPE_CHECKING:
    from omnigent.stores.conversation_store import ConversationStore

PROFILE_LABEL_PREFIX = "sandbox_profile:"
PROFILE_HOST_SESSION_LIMIT = 200
_FIELDS = ("provider", "id", "revision", "user_id", "name")
PROFILE_LABEL_KEYS = tuple(
    PROFILE_LABEL_PREFIX + field for field in (*_FIELDS, "description:0", "description:1")
)


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
    try:
        snapshot = profile_from_labels(labels)
    except ValueError:
        # Malformed legacy references must not break session reads.
        return None
    return (
        {key: value for key, value in snapshot.items() if key not in ("user_id", "workspace_id")}
        if snapshot
        else None
    )


def has_profile_reference(labels: Mapping[str, str]) -> bool:
    """Check reference shape for affinity guards, without authorizing execution."""
    try:
        return profile_from_labels(labels) is not None
    except ValueError:
        return False


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
        try:
            binding = self.conversations.get_profile_host_binding(session_id)
        except ConversationNotFoundError:
            return None
        return json.loads(binding.profile_snapshot) if binding.profile_snapshot else None

    def can_bind_host(self, session_id: str, host_id: str) -> bool:
        return self.conversations.can_bind_profile_host(session_id, host_id)

    def get_session_host_id(self, session_id: str) -> str | None:
        try:
            binding = self.conversations.get_profile_host_binding(session_id)
        except ConversationNotFoundError:
            return None
        return binding.profile_host_id

    def get_host_snapshot(
        self, host_id: str, *, reference_session_id: str | None = None
    ) -> dict[str, Any] | None:
        host_id = uuid_to_bytes(host_id).hex()
        if reference_session_id is not None:
            try:
                binding = self.conversations.get_profile_host_binding(reference_session_id)
            except ConversationNotFoundError:
                pass
            else:
                if binding.host_id == host_id and binding.profile_snapshot:
                    return json.loads(binding.profile_snapshot)
        ids = self.conversations.list_host_conversation_ids(
            host_id, limit=PROFILE_HOST_SESSION_LIMIT + 1
        )
        if len(ids) > PROFILE_HOST_SESSION_LIMIT:
            raise ValueError(
                "Host history exceeds the profile lookup limit; "
                "enable controller assignment lookup"
            )
        accepted = None
        for session_id in ids:
            snapshot = self.get_session_snapshot(session_id)
            if snapshot is not None:
                if accepted is not None and not same_profile_identity(accepted, snapshot):
                    raise ValueError("Sessions on this host have conflicting sandbox profiles")
                accepted = snapshot
        return accepted
