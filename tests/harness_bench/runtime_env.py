"""Derive the bench's server+runner environment the way ``omni run`` does.

``omni run`` resolves credentials through the canonical
:func:`omnigent.runtime.credentials.databricks.resolve_databricks_workspace`,
takes its profile from ``~/.omnigent/config.yaml``'s ``auth:``/``profile`` block,
and lets an ambient ``OPENAI_*`` env win when present. This module mirrors that
layering for the bench so a no-flag run behaves like ``omni run``, while an
explicit ``--profile`` overrides the config-derived profile.

The old bench path always minted its own bearer via a ``databricks auth token``
subprocess (which did not handle OAuth profiles) and required ``--profile``;
this replaces it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class BenchRuntimeEnv:
    """The environment a bench server/runner/host-daemon is spawned with.

    :param base_env: The full env dict (``os.environ`` plus any derived
        ``OPENAI_*`` / ``DATABRICKS_CONFIG_PROFILE``).
    :param db_profile: The resolved Databricks profile name (for
        ``spec.executor.profile`` / skip-gating), or ``None`` when auth came
        from the ambient env or the SDK default.
    """

    base_env: dict[str, str]
    db_profile: str | None


def _profile_from_config() -> str | None:
    """Return the Databricks profile ``omni run`` would use, or ``None``.

    Mirrors ``omni run``'s profile sources, in the same precedence:

    1. the user-level ``auth:`` block (``omni setup`` writes this);
    2. a top-level ``profile:`` key;
    3. the default ``providers:`` entry of ``kind: databricks`` — the common
       case for a machine configured through the provider wizard rather than
       ``auth:`` (e.g. ``providers.databricks {default: true, profile: DEFAULT}``).
       ``omni run`` resolves creds from this entry's ``profile`` (see
       ``runtime/workflow.py`` ``DATABRICKS_KIND`` branch), so the bench must too
       or a no-flag run would go offline where ``omni run`` goes live.

    All imports are lazy so importing this module never drags in ``omnigent.cli``
    at load time.
    """
    try:
        from omnigent.runtime.workflow import _load_global_auth

        auth = _load_global_auth()
    except Exception:
        auth = None
    profile = getattr(auth, "profile", None)
    if profile:
        return str(profile)

    try:
        from omnigent.cli import _load_effective_config

        config = _load_effective_config()
    except Exception:
        return None

    cfg_profile = config.get("profile")
    if cfg_profile:
        return str(cfg_profile)

    # Tier 3: the configured ``providers:`` block. Reuse ``omni``'s own resolver
    # (``default_provider_for_harness`` — the same call ``resolve_credential``
    # and the runtime spawn-env builder use) so the bench picks exactly the
    # provider a launch would, with no reinvented selection logic. When that
    # provider is a Databricks profile, its ``profile`` is the gateway route.
    try:
        from omnigent.onboarding.provider_config import (
            DATABRICKS_KIND,
            default_provider_for_harness,
        )

        # claude-sdk is a representative gateway harness; the databricks
        # provider is auth-only (serves every family), so the harness choice
        # only decides which family's default is consulted, not the profile.
        provider = default_provider_for_harness(config, "claude-sdk")
    except Exception:
        provider = None
    if provider is not None and provider.kind == DATABRICKS_KIND and provider.profile:
        return str(provider.profile)
    return None


def resolve_bench_env(explicit_profile: str | None) -> BenchRuntimeEnv:
    """Build the bench runtime env, mirroring ``omni run``'s credential layering.

    Precedence:

    1. **Ambient wins.** If both ``OPENAI_BASE_URL`` and ``OPENAI_API_KEY`` are
       already in the environment, use them and skip credential resolution
       entirely — the same short-circuit ``omni run`` has
       (``inner/databricks_executor.py``). This also lets a run with an
       exported gateway token work with no profile configured.
    2. **Profile.** ``explicit_profile`` (the ``--profile`` flag) wins; else the
       config-derived profile (``auth:``/``profile`` in
       ``~/.omnigent/config.yaml``). May be ``None`` (the resolver then uses the
       SDK / ``[DEFAULT]`` path, as ``omni run`` does).
    3. **Compose** ``OPENAI_*`` from
       :func:`resolve_databricks_workspace` — OAuth-profile aware, fails loud on
       a typo'd named profile — filling in only the vars not already ambient.

    :param explicit_profile: The ``--profile`` value, or ``None`` to derive.
    :returns: A :class:`BenchRuntimeEnv`.
    :raises OSError: When credentials cannot be resolved and no ambient
        ``OPENAI_*`` covers auth (surfaced by the caller as a clean skip).
    """
    base = dict(os.environ)
    have_ambient = bool(base.get("OPENAI_BASE_URL")) and bool(base.get("OPENAI_API_KEY"))
    profile = explicit_profile or _profile_from_config()

    if have_ambient:
        # Ambient OPENAI_* already routes the gateway; don't mint. Still stamp
        # the profile so the runner's DATABRICKS_CONFIG_PROFILE matches.
        if profile:
            base["DATABRICKS_CONFIG_PROFILE"] = profile
        return BenchRuntimeEnv(base_env=base, db_profile=profile)

    from omnigent.runtime.credentials.databricks import resolve_databricks_workspace

    creds = resolve_databricks_workspace(profile)
    base["OPENAI_BASE_URL"] = f"{creds.host}/serving-endpoints"
    base["OPENAI_API_KEY"] = creds.token
    if profile:
        base["DATABRICKS_CONFIG_PROFILE"] = profile
    return BenchRuntimeEnv(base_env=base, db_profile=profile)


def bench_creds_skip_reason(explicit_profile: str | None) -> str | None:
    """Cheap gate: why the bench cannot get gateway creds, or ``None`` if it can.

    Mirrors :func:`resolve_bench_env`'s precedence without minting a token, so a
    driver's ``unavailable()`` can skip a live run cleanly (no creds) rather than
    fail mid-provision. A ``--profile`` is no longer required: an ambient
    ``OPENAI_*`` or a configured ``~/.omnigent`` profile is enough, matching
    ``omni run``.

    :param explicit_profile: The ``--profile`` value, or ``None`` to derive.
    :returns: A skip reason, or ``None`` when creds are resolvable.
    """
    if os.environ.get("OPENAI_BASE_URL") and os.environ.get("OPENAI_API_KEY"):
        return None
    profile = explicit_profile or _profile_from_config()
    if not profile:
        return (
            "no gateway creds: pass --profile, configure a profile in "
            "~/.omnigent/config.yaml (like `omni run`), or export OPENAI_API_KEY + "
            "OPENAI_BASE_URL"
        )
    from tests.e2e.helpers import lookup_databricks_host

    if lookup_databricks_host(profile) is None:
        return f"databricks profile {profile!r} missing/hostless in ~/.databrickscfg"
    return None


__all__ = ["BenchRuntimeEnv", "bench_creds_skip_reason", "resolve_bench_env"]
