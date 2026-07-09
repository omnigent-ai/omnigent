"""Name↔int codecs for enum-like columns stored as ``SMALLINT``.

Several low-cardinality closed-set columns (``conversations.kind``,
``conversation_items.type``/``status``, ``comments.status``,
``account_tokens.kind``, ``policies.type``, ``policies.scope``,
``hosts.status``, ``agents.kind``) are stored as integer codes rather
than their string names — smaller rows and a tighter ``CHECK`` than a
free ``VARCHAR``. The string names remain the
contract for entities, the HTTP API, the web client, and the SDKs; the
integer form never leaves the store row↔entity boundary. These codecs are
the single place that translates between the two.

Codes are STABLE and append-only: never renumber or reuse a shipped code,
and leave gaps rather than reordering, so old rows keep their meaning.
This mirrors :data:`omnigent.server.auth.LEVEL_READ` and friends, the
existing int-coded ``session_permissions.level``.
"""

from __future__ import annotations

from omnigent.entities.conversation import ITEM_TYPE_TO_DATA_CLS

# ── Code tables (name → stable int code) ───────────────

CONVERSATION_KIND: dict[str, int] = {
    "default": 1,
    "sub_agent": 2,
}

# Item type codes. The key set is kept in lock-step with
# ITEM_TYPE_TO_DATA_CLS (the app-layer source of truth) by
# _assert_item_type_codes_cover_data_classes below, so a newly added item
# type cannot ship without a code. Codes are append-only.
ITEM_TYPE: dict[str, int] = {
    "message": 1,
    "function_call": 2,
    "function_call_output": 3,
    "reasoning": 4,
    "error": 5,
    "compaction": 6,
    "native_tool": 7,
    "resource_event": 8,
    "routing_decision": 9,
    "slash_command": 10,
    "terminal_command": 11,
}

# Item status codes. Only "completed" is written today (items are final on
# append), but the field is semantically an OpenAI-style status that may
# widen, so codes for the rest of that vocabulary are reserved up front and
# the column CHECK admits all of them.
ITEM_STATUS: dict[str, int] = {
    "completed": 1,
    "in_progress": 2,
    "incomplete": 3,
    "failed": 4,
}

COMMENT_STATUS: dict[str, int] = {
    "draft": 1,
    "addressed": 2,
}

ACCOUNT_TOKEN_KIND: dict[str, int] = {
    "invite": 1,
    "magic": 2,
}

POLICY_TYPE: dict[str, int] = {
    "python": 1,
    "url": 2,
}

HOST_STATUS: dict[str, int] = {
    "online": 1,
    "offline": 2,
}

AGENT_KIND: dict[str, int] = {
    "template": 1,
    "session": 2,
}

POLICY_SCOPE: dict[str, int] = {
    "default": 1,
    "session": 2,
}


def _assert_item_type_codes_cover_data_classes() -> None:
    """
    Guard that :data:`ITEM_TYPE` matches the app's item-type registry.

    Raised at import time (and asserted by a unit test) so a new item type
    added to ``ITEM_TYPE_TO_DATA_CLS`` without a corresponding code fails
    loudly instead of silently breaking persistence.

    :raises RuntimeError: If the two key sets diverge.
    """
    missing = set(ITEM_TYPE_TO_DATA_CLS) - set(ITEM_TYPE)
    extra = set(ITEM_TYPE) - set(ITEM_TYPE_TO_DATA_CLS)
    if missing or extra:
        raise RuntimeError(
            "ITEM_TYPE codes are out of sync with ITEM_TYPE_TO_DATA_CLS "
            f"(missing codes for {sorted(missing)}, "
            f"unknown types {sorted(extra)})."
        )


_assert_item_type_codes_cover_data_classes()


# ── Encode / decode ────────────────────────────────────


def _invert(table: dict[str, int]) -> dict[int, str]:
    """Return the code→name inverse of a name→code table."""
    return {code: name for name, code in table.items()}


_CODE_TO_NAME: dict[int, dict[int, str]] = {}


def _encode(table: dict[str, int], name: str, *, field: str) -> int:
    """
    Map an enum *name* to its stable integer code.

    :param table: The name→code table for the field.
    :param name: The string enum name, e.g. ``"sub_agent"``.
    :param field: Field label used in the error message, e.g.
        ``"conversations.kind"``.
    :returns: The integer code.
    :raises ValueError: If *name* is not a known value for the field.
    """
    try:
        return table[name]
    except KeyError:
        raise ValueError(f"unknown {field} value: {name!r}") from None


def _decode(table: dict[str, int], code: int, *, field: str) -> str:
    """
    Map an integer *code* back to its enum name.

    :param table: The name→code table for the field.
    :param code: The stored integer code.
    :param field: Field label used in the error message, e.g.
        ``"conversations.kind"``.
    :returns: The string enum name.
    :raises ValueError: If *code* is not a known code for the field.
    """
    inverse = _CODE_TO_NAME.get(id(table))
    if inverse is None:
        inverse = _invert(table)
        _CODE_TO_NAME[id(table)] = inverse
    try:
        return inverse[code]
    except KeyError:
        raise ValueError(f"unknown {field} code: {code!r}") from None


def encode_conversation_kind(name: str) -> int:
    """Encode a ``conversations.kind`` name to its int code."""
    return _encode(CONVERSATION_KIND, name, field="conversations.kind")


def decode_conversation_kind(code: int) -> str:
    """Decode a ``conversations.kind`` int code to its name."""
    return _decode(CONVERSATION_KIND, code, field="conversations.kind")


def encode_item_type(name: str) -> int:
    """Encode a ``conversation_items.type`` name to its int code."""
    return _encode(ITEM_TYPE, name, field="conversation_items.type")


def decode_item_type(code: int) -> str:
    """Decode a ``conversation_items.type`` int code to its name."""
    return _decode(ITEM_TYPE, code, field="conversation_items.type")


def encode_item_status(name: str) -> int:
    """Encode a ``conversation_items.status`` name to its int code."""
    return _encode(ITEM_STATUS, name, field="conversation_items.status")


def decode_item_status(code: int) -> str:
    """Decode a ``conversation_items.status`` int code to its name."""
    return _decode(ITEM_STATUS, code, field="conversation_items.status")


def encode_comment_status(name: str) -> int:
    """Encode a ``comments.status`` name to its int code."""
    return _encode(COMMENT_STATUS, name, field="comments.status")


def decode_comment_status(code: int) -> str:
    """Decode a ``comments.status`` int code to its name."""
    return _decode(COMMENT_STATUS, code, field="comments.status")


def encode_account_token_kind(name: str) -> int:
    """Encode an ``account_tokens.kind`` name to its int code."""
    return _encode(ACCOUNT_TOKEN_KIND, name, field="account_tokens.kind")


def decode_account_token_kind(code: int) -> str:
    """Decode an ``account_tokens.kind`` int code to its name."""
    return _decode(ACCOUNT_TOKEN_KIND, code, field="account_tokens.kind")


def encode_policy_type(name: str) -> int:
    """Encode a ``policies.type`` name to its int code."""
    return _encode(POLICY_TYPE, name, field="policies.type")


def decode_policy_type(code: int) -> str:
    """Decode a ``policies.type`` int code to its name."""
    return _decode(POLICY_TYPE, code, field="policies.type")


def encode_host_status(name: str) -> int:
    """Encode a ``hosts.status`` name to its int code."""
    return _encode(HOST_STATUS, name, field="hosts.status")


def decode_host_status(code: int) -> str:
    """Decode a ``hosts.status`` int code to its name."""
    return _decode(HOST_STATUS, code, field="hosts.status")


def encode_agent_kind(name: str) -> int:
    """Encode an ``agents.kind`` name to its int code."""
    return _encode(AGENT_KIND, name, field="agents.kind")


def decode_agent_kind(code: int) -> str:
    """Decode an ``agents.kind`` int code to its name."""
    return _decode(AGENT_KIND, code, field="agents.kind")


def encode_policy_scope(name: str) -> int:
    """Encode a ``policies.scope`` name to its int code."""
    return _encode(POLICY_SCOPE, name, field="policies.scope")


def decode_policy_scope(code: int) -> str:
    """Decode a ``policies.scope`` int code to its name."""
    return _decode(POLICY_SCOPE, code, field="policies.scope")
