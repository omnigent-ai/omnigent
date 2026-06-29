"""Runtime-native credential resolvers for native executors."""

from __future__ import annotations


def resolve_user_secret(user_id: str, name: str) -> str | None:
    """Resolve the acting user's stored secret from the per-user vault (#5).

    The building block for per-user credentials in a shared session: given the
    *acting* user's identity (threaded to tool execution via
    :class:`~omnigent.tools.base.ToolContext`) and a logical credential name,
    return that user's decrypted secret — so a collaborator's tool action can
    run under their own credentials, not the session owner's.

    Returns ``None`` when the vault isn't configured, the user has no such
    secret, or the identity is missing — callers fall back to ambient
    credentials in that case.

    :param user_id: The acting user (the secret's owner).
    :param name: The logical credential name, e.g. ``"github"``.
    :returns: The decrypted secret, or ``None``.
    """
    if not user_id or not name:
        return None
    from omnigent.runtime import get_caps, get_user_credential_store

    store = get_user_credential_store()
    key = get_caps().vault_key
    if store is None or key is None:
        return None
    encrypted = store.get_encrypted(user_id, name)
    if encrypted is None:
        return None
    from omnigent.server.secret_vault import decrypt_secret

    return decrypt_secret(key, encrypted)
