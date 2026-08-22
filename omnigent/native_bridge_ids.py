"""Identifier normalisation shared by the native-harness bridges."""

from __future__ import annotations

# Conversation ids carried this prefix before they became bare 32-char hex
# (see ``omnigent.db.db_models._LEGACY_ID_PREFIXES``). A bridge-id label stores
# a session id as its *value*, and the binary-uuid migration rewrote id columns
# only -- so a session created before it still names itself ``conv_<hex>``
# there, while every other code path now says ``<hex>``.
_LEGACY_CONVERSATION_PREFIX = "conv_"


def normalize_bridge_id(bridge_id: str) -> str:
    """
    Return a native-harness bridge id in its bare, post-migration form.

    Bridge dirs are keyed on a digest of this id, so a legacy-prefixed and a
    bare spelling of the same session must not resolve to different dirs --
    the harness executor and the terminal that launched the agent would then
    rendezvous in two places and no message would ever reach the pane.

    :param bridge_id: Bridge id from a label or a session id, e.g.
        ``"conv_abc123"``, ``"abc123"`` or ``"abc123-cleared"``.
    :returns: The same id without the legacy ``conv_`` prefix.
    """
    return bridge_id.removeprefix(_LEGACY_CONVERSATION_PREFIX)
