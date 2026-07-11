"""Resolution of the Z.AI API key (env → Omnigent keychain).

Z.AI (Zhipu AI, https://z.ai) provides GLM models. OpenCode's built-in
``zai-coding-plan`` provider authenticates via the ``ZHIPU_API_KEY`` env
var (or its own ``auth.json`` via ``/connect``). Omnigent can additionally
hold the key in its keychain-backed secret store (``omnigent setup`` →
OpenCode → "Set Z.AI API key") so sessions work without any ambient env.
This module is the single resolver used by the runner (spawn-env injection),
setup reporting, and the sbx passthrough.

Resolution order — ambient env always wins over the keychain:

1. ``ZHIPU_API_KEY``
2. ``OMNIGENT_ZHIPU_API_KEY`` (the standard Omnigent-prefixed alias)
3. Keychain secret ``zai-coding-plan`` (:mod:`omnigent.onboarding.secrets`)
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from omnigent.env_credentials import env_names_with_omnigent_prefix

#: Env var OpenCode reads to authenticate the ``zai-coding-plan`` provider.
ZHIPU_API_KEY_ENV_VAR = "ZHIPU_API_KEY"

#: Keychain secret name ``omnigent setup`` stores the Z.AI key under.
ZAI_SECRET_NAME = "zai-coding-plan"

#: Source tag for a keychain-resolved key.
KEYCHAIN_SOURCE = "keychain"


def resolve_zai_key(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str] | None:
    """Resolve the Z.AI (Zhipu) API key.

    :param environ: Optional environment mapping; defaults to ``os.environ``.
    :returns: ``(source, key)`` — source is ``"env:<NAME>"`` or
        :data:`KEYCHAIN_SOURCE` — or ``None`` when no key is configured.
        Keychain read failures count as "no key"; this never raises.
    """
    env = os.environ if environ is None else environ
    for name in env_names_with_omnigent_prefix(ZHIPU_API_KEY_ENV_VAR):
        value = env.get(name, "")
        if value.strip():
            return f"env:{name}", value.strip()
    try:
        from omnigent.onboarding.secrets import load_secret

        stored = load_secret(ZAI_SECRET_NAME)
    except Exception:  # noqa: BLE001 - a locked/broken keyring must degrade to "no key".
        return None
    if stored and stored.strip():
        return KEYCHAIN_SOURCE, stored.strip()
    return None


def zai_spawn_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Env to merge into a spawned ``opencode`` process.

    Ambient env still wins in the spawned process: an env-resolved key just
    re-stamps the value the child would inherit anyway.

    :param environ: Optional environment mapping; defaults to ``os.environ``.
    :returns: ``{"ZHIPU_API_KEY": <key>}`` when a key resolves, else ``{}``.
    """
    resolved = resolve_zai_key(environ)
    if resolved is None:
        return {}
    return {ZHIPU_API_KEY_ENV_VAR: resolved[1]}
