"""Non-interactive credential-write core for UI-driven harness auth.

The web UI's "Add a credential" flow (Setup From the UI, M3) needs to write a
provider credential onto a connected host without the interactive
``omnigent setup`` wizard. This module is the shared, prompt-free core the host
daemon's ``host.store_secret`` handler calls — and which the CLI wizard can
call too — mirroring how M1 extracted :func:`try_install_harness_cli` as the
non-interactive install core.

It does exactly what the wizard's "add a key / gateway" path does, minus the
prompts: store the secret (OS keychain, else ``~/.omnigent/secrets.json``),
write a ``providers:`` entry referencing it by ``keychain:<name>`` (never the
raw key), and — for the first provider on a family — make it the default. The
raw secret never lands in ``config.yaml``; it lives only in the secret store.

Scope is deliberately narrow (the v1 UI-auth surface): the ``anthropic`` /
``openai`` families served by ``key`` and ``gateway`` providers, which back the
Claude / Codex / Pi harnesses. Subscription / bedrock / databricks kinds are
out of scope here — they are either CLI-login-bound or configured elsewhere.
"""

from __future__ import annotations

import logging
import os
from typing import Literal, NamedTuple

from omnigent.onboarding.configure_models import (
    build_gateway_provider_entry,
    build_key_provider_entry,
    default_base_url_for_family,
)
from omnigent.onboarding.provider_config import (
    ANTHROPIC_FAMILY,
    OPENAI_FAMILY,
    load_config,
    provider_entry_settings,
    set_default_provider,
)

_logger = logging.getLogger(__name__)

# The families the UI auth surface can write. Keeping this explicit (rather
# than accepting any string) means a malformed/spoofed frame can't drive the
# credential writer for a family this flow doesn't support.
_SUPPORTED_FAMILIES: frozenset[str] = frozenset({ANTHROPIC_FAMILY, OPENAI_FAMILY})

CredentialKind = Literal["key", "gateway"]


class StoreCredentialResult(NamedTuple):
    """Outcome of :func:`store_harness_credential`.

    :param stored: Whether the credential was written (secret + provider entry).
    :param provider_name: The ``providers:`` entry name written, or ``None`` on
        failure. Never the secret — only the entry name (e.g. ``"anthropic"``).
    :param reason: Human-readable failure reason when ``stored`` is False;
        ``None`` on success. Never contains the secret value.
    """

    stored: bool
    provider_name: str | None
    reason: str | None


def _config_writer():  # type: ignore[no-untyped-def]
    """Return a ``(load, save)`` pair for the global config on this host.

    Isolated so the daemon writes to the same ``~/.omnigent/config.yaml`` the
    readiness layer reads, without importing the CLI. ``save`` deep-merges the
    ``providers:`` block (adds/updates one entry without dropping siblings),
    matching ``omnigent setup``'s writer.
    """
    import yaml

    from omnigent.onboarding.provider_config import _config_path

    def _load() -> dict[str, object]:
        return load_config()

    def _save(settings: dict[str, object], *, deep_merge_providers: bool) -> None:
        path = _config_path()
        cfg = _load()
        for key, value in settings.items():
            if deep_merge_providers and key == "providers" and isinstance(value, dict):
                existing = cfg.get("providers")
                merged = dict(existing) if isinstance(existing, dict) else {}
                merged.update(value)
                cfg["providers"] = merged
            else:
                cfg[key] = value
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=True)

    return _load, _save


def store_harness_credential(
    *,
    family: str,
    kind: CredentialKind,
    secret: str,
    base_url: str | None = None,
    default_model: str | None = None,
    wire_api: str | None = None,
) -> StoreCredentialResult:
    """Write a provider credential for a harness family, non-interactively.

    Stores *secret* in the secret store under a family-derived name, then writes
    a ``providers:`` entry (``key`` or ``gateway``) referencing it as
    ``keychain:<name>`` — never the raw secret — and makes it the family default
    when no default is set yet. This is the prompt-free equivalent of the
    ``omnigent setup`` "add a key / gateway" path.

    The secret is passed by value and handed straight to the secret store; it is
    never logged, echoed, or written to ``config.yaml``.

    :param family: ``"anthropic"`` or ``"openai"`` (the v1 UI-auth families).
    :param kind: ``"key"`` (a vendor API key at the family's canonical endpoint)
        or ``"gateway"`` (an OpenAI/Anthropic-compatible proxy at *base_url*).
    :param secret: The API key / gateway token to store. Required and non-empty.
    :param base_url: Required for ``kind="gateway"``; ignored for ``kind="key"``
        (which uses the family's canonical endpoint).
    :param default_model: Optional family default model id to pin.
    :param wire_api: Optional OpenAI wire protocol (``"chat"`` / ``"responses"``)
        for the ``openai`` family; ignored for ``anthropic``.
    :returns: A :class:`StoreCredentialResult`.
    """
    if family not in _SUPPORTED_FAMILIES:
        return StoreCredentialResult(False, None, f"unsupported family {family!r}")
    if not secret or not secret.strip():
        return StoreCredentialResult(False, None, "no credential provided")
    if kind == "gateway":
        if not (base_url and base_url.strip()):
            return StoreCredentialResult(False, None, "a gateway requires a base_url")
        # Reject a non-http(s) base_url here rather than writing a malformed
        # provider entry that fails opaquely at the harness's first turn.
        if not base_url.strip().lower().startswith(("http://", "https://")):
            return StoreCredentialResult(
                False, None, "a gateway base_url must start with http:// or https://"
            )

    from omnigent.onboarding import secrets as secret_store

    # The entry name is the family for a key (one canonical vendor key per
    # family), or "<family>-gateway" for a gateway, so a re-add updates in place
    # rather than piling up duplicates. Keychain slot == entry name.
    name = family if kind == "key" else f"{family}-gateway"
    try:
        secret_store.store_secret(name, secret.strip())
    except Exception as exc:  # pragma: no cover - keychain/file backend failure
        _logger.debug("store_harness_credential: secret store failed", exc_info=True)
        return StoreCredentialResult(False, None, f"could not store the credential: {exc}")

    api_key_ref = f"keychain:{name}"
    if kind == "key":
        entry = build_key_provider_entry(
            family=family,
            base_url=default_base_url_for_family(family),
            api_key_ref=api_key_ref,
            default_model=default_model,
            wire_api=wire_api,
        )
    else:
        entry = build_gateway_provider_entry(
            base_url=base_url.strip(),  # type: ignore[union-attr]  # validated above
            api_key_ref=api_key_ref,
            families=[family],
            wire_api=wire_api,
            models={family: default_model} if default_model else None,
        )

    _load, _save = _config_writer()
    try:
        # Add/update the one provider entry (deep-merge keeps siblings).
        _save(provider_entry_settings(name, entry, make_default=False), deep_merge_providers=True)
        # Make it the family default only when nothing else claims it yet, so we
        # don't silently re-route a family the user already configured. When we
        # do set it, rewrite the whole providers block (set_default_provider
        # clears sibling default flags a deep-merge can't reach).
        from omnigent.onboarding.provider_config import get_default_provider

        cfg = _load()
        if get_default_provider(cfg, family) is None:
            block = cfg.get("providers")
            if isinstance(block, dict):
                _save(
                    {"providers": set_default_provider(block, name, family)},
                    deep_merge_providers=False,
                )
    except Exception as exc:  # pragma: no cover - config write failure
        _logger.debug("store_harness_credential: config write failed", exc_info=True)
        return StoreCredentialResult(False, None, f"could not write provider config: {exc}")

    return StoreCredentialResult(True, name, None)


class DetectedCredential(NamedTuple):
    """A credential already present on the host, offered for one-click adopt.

    Carries only non-secret metadata — the source label (e.g. an env var name)
    and the family it serves — never the secret value itself.

    :param family: The family it serves, ``"anthropic"`` or ``"openai"``.
    :param source: A human-readable, non-secret source label, e.g.
        ``"$ANTHROPIC_API_KEY"`` (an env var) or ``"claude CLI login"``.
    :param env_var: The environment variable name when the source is an env
        key (so adopt can write ``api_key_ref: env:<VAR>`` without ever reading
        the value); ``None`` for non-env sources.
    """

    family: str
    source: str
    env_var: str | None


def detect_adoptable_credentials() -> list[DetectedCredential]:
    """Return credentials already on the host the UI can offer to adopt.

    Wraps the existing ambient detection (:func:`ambient.detect_providers`),
    surfacing only the ``key``-kind env-var credentials for the v1 UI-auth
    families as non-secret descriptors. Never returns a secret value — only the
    source label + env var name, so the UI can offer "Use $ANTHROPIC_API_KEY"
    and adopt it by reference. Never raises (a detection failure yields an empty
    list rather than crashing the readiness/adopt path).
    """
    try:
        from omnigent.onboarding.ambient import detect_providers

        detected = detect_providers()
    except Exception:
        _logger.debug("detect_adoptable_credentials: detection failed", exc_info=True)
        return []

    result: list[DetectedCredential] = []
    seen: set[tuple[str, str]] = set()
    for provider in detected:
        family = getattr(provider, "family", None)
        kind = getattr(provider, "kind", None)
        source = getattr(provider, "source", None)
        if family not in _SUPPORTED_FAMILIES or kind != "key" or not isinstance(source, str):
            continue
        # Only env-var sources are adoptable by reference (env:<VAR>); a CLI
        # login isn't a key the UI can point a provider entry at.
        env_var = source.lstrip("$") if source.startswith("$") else None
        if env_var is None:
            continue
        key = (family, env_var)
        if key in seen:
            continue
        seen.add(key)
        result.append(DetectedCredential(family=family, source=source, env_var=env_var))
    return result


def adopt_env_credential(*, family: str, env_var: str) -> StoreCredentialResult:
    """Adopt an existing host env-var credential as a family key, by reference.

    Writes a ``key`` provider entry whose ``api_key_ref`` is ``env:<env_var>``
    — pointing at the credential the host already has, without ever reading its
    value. The reference form means the secret stays only in the environment;
    omnigent resolves it at run time.

    :param family: ``"anthropic"`` or ``"openai"``.
    :param env_var: The environment variable to reference, e.g.
        ``"ANTHROPIC_API_KEY"``. Must be present in the host's environment —
        adopting a var that isn't set would persist a provider entry that
        resolves to nothing at run time, so it's refused here.
    :returns: A :class:`StoreCredentialResult` (``provider_name`` is the entry).
    """
    if family not in _SUPPORTED_FAMILIES:
        return StoreCredentialResult(False, None, f"unsupported family {family!r}")
    env_var = env_var.strip() if env_var else ""
    if not env_var:
        return StoreCredentialResult(False, None, "no environment variable named")
    # Only adopt a var that's actually set on this host — otherwise the entry
    # would reference an empty credential and fail at the first turn. Test
    # PRESENCE ONLY (``in os.environ``, never reading the value) so the "never
    # reads the value" contract stays literally true; the caller (the daemon's
    # adopt handler) already restricts this to vars ambient detection surfaced,
    # which are meaningfully set. (This runs on the runner, so os.environ is the
    # host's environment.)
    if env_var not in os.environ:
        return StoreCredentialResult(False, None, f"{env_var} is not set on this host")

    name = family
    entry = build_key_provider_entry(
        family=family,
        base_url=default_base_url_for_family(family),
        api_key_ref=f"env:{env_var.strip()}",
        default_model=None,
    )
    _load, _save = _config_writer()
    try:
        _save(provider_entry_settings(name, entry, make_default=False), deep_merge_providers=True)
        from omnigent.onboarding.provider_config import get_default_provider

        cfg = _load()
        if get_default_provider(cfg, family) is None:
            block = cfg.get("providers")
            if isinstance(block, dict):
                _save(
                    {"providers": set_default_provider(block, name, family)},
                    deep_merge_providers=False,
                )
    except Exception as exc:  # pragma: no cover - config write failure
        _logger.debug("adopt_env_credential: config write failed", exc_info=True)
        return StoreCredentialResult(False, None, f"could not write provider config: {exc}")
    return StoreCredentialResult(True, name, None)
