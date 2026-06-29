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


def resolve_user_credential_env(user_id: str) -> dict[str, str]:
    """Resolve all of a user's vault secrets, mapped to subprocess env vars (#5).

    The server side of transparent per-user credentials: given the *acting*
    user, decrypt every secret they've stored and map it to the environment
    variable(s) the relevant CLI reads (see
    :func:`omnigent.runtime.credentials.injection.build_credential_env`). The
    server pushes this dict to the runner on the turn dispatch so the runner can
    overlay it onto that user's tool subprocesses — the runner never needs to
    pull individual secrets, and no "resolve arbitrary user" endpoint is exposed.

    Returns an empty dict (never raises) when the vault isn't configured, the
    identity is missing, or the user has stored nothing — the overwhelming
    common case, so a turn for a credential-less user costs one empty list query.

    :param user_id: The acting user whose secrets to resolve.
    :returns: ``{ENV_VAR: value}`` ready to overlay onto a subprocess env, or
        ``{}`` when there is nothing to inject.
    """
    if not user_id:
        return {}
    from omnigent.runtime import get_caps, get_user_credential_store

    store = get_user_credential_store()
    key = get_caps().vault_key
    if store is None or key is None:
        return {}
    from omnigent.runtime.credentials.injection import build_credential_env
    from omnigent.server.secret_vault import decrypt_secret

    secrets: dict[str, str] = {}
    for cred in store.list_for_user(user_id):
        encrypted = store.get_encrypted(user_id, cred.name)
        if encrypted is not None:
            secrets[cred.name] = decrypt_secret(key, encrypted)
    return build_credential_env(secrets)
