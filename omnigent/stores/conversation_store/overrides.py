"""Encoding and storage bounds shared by session validation and persistence."""

import json
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError

# Matches conversations.session_overrides VARCHAR(512).
SESSION_OVERRIDES_MAX_LENGTH = 512


# Per-session config overrides packed into the ``conversations.session_overrides``
# JSON blob. Order is fixed so the encoded object is stable across writes.
_SESSION_OVERRIDE_KEYS = (
    "reasoning_effort",
    "model_override",
    "reported_model",
    "cost_control_mode_override",
    "subagent_routing_override",
    "harness_override",
    # A JSON object of declared env-var name -> per-session value, not a
    # scalar like its neighbours; an empty object encodes as unset.
    "env_passthrough_values",
    # Stored as the string ``"on"`` when the owner shares workspace files
    # with view-level collaborators; absent (SQL NULL blob key) otherwise.
    "share_workspace_files",
)


def encode_session_overrides(overrides: dict[str, Any]) -> str | None:
    """Pack the set per-session overrides into a compact JSON blob.

    Omits keys whose value is ``None`` and returns ``None`` when nothing is
    set, so a session on all agent/spec defaults stores SQL ``NULL`` rather
    than an empty object. Only the :data:`_SESSION_OVERRIDE_KEYS` are
    considered; any other keys in *overrides* are ignored.

    :param overrides: Mapping of override key to value (missing / ``None``
        values mean "unset"). An empty collection also means "unset", so a
        caller clearing ``env_passthrough_values`` drops the key rather than
        storing an empty object.
    :returns: Compact JSON object string, or ``None`` when no override is set.
    """
    data = {
        key: overrides[key]
        for key in _SESSION_OVERRIDE_KEYS
        if overrides.get(key) is not None and overrides[key] != {}
    }
    encoded = json.dumps(data, separators=(",", ":")) if data else None
    if encoded is not None and len(encoded) > SESSION_OVERRIDES_MAX_LENGTH:
        raise OmnigentError(
            f"Session overrides exceed the {SESSION_OVERRIDES_MAX_LENGTH}-character limit. "
            "Use shorter harness or model identifiers, or fewer env passthrough values.",
            code=ErrorCode.INVALID_INPUT,
        )
    return encoded


def decode_session_overrides(raw: str | None) -> dict[str, Any]:
    """Unpack the ``session_overrides`` blob to a full override dict.

    Every one of the :data:`_SESSION_OVERRIDE_KEYS` is present in the
    result (unset keys read back as ``None``) so read-modify-write callers can
    treat the dict uniformly regardless of which overrides were stored.

    :param raw: The stored JSON blob, or ``None``.
    :returns: Dict keyed by every override name, value ``None`` when unset.
    """
    data: dict[str, Any] = json.loads(raw) if raw else {}
    return {key: data.get(key) for key in _SESSION_OVERRIDE_KEYS}
