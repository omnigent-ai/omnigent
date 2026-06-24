"""GitHub Copilot token storage for ``omnigent setup`` and the runtime.

Copilot is deliberately outside the anthropic/openai provider-family + gateway
machinery (see :func:`omnigent.runtime.workflow._build_copilot_spawn_env`): the
GitHub Copilot SDK (``github-copilot-sdk``) talks only to GitHub's Copilot
backend, authenticated by a **GitHub token** — never the Databricks AI gateway.
It therefore has no ``providers:`` family entry, but a user should still be able
to register a Copilot token once through ``omnigent setup`` rather than
exporting it in every shell.

This module is that home. The token is stored exactly like the api-key
providers' secrets — in the omnigent secret store (OS keychain, else a ``0600``
JSON file; see :mod:`omnigent.onboarding.secrets`) — and referenced from a
dedicated top-level ``copilot:`` block in ``~/.omnigent/config.yaml``::

    copilot:
      github_token_ref: keychain:copilot   # or env:GH_TOKEN

The reference is resolved with the same :func:`resolve_secret` resolver the
provider families use. A dedicated block (rather than the shared global
``auth:`` block) is required because ``auth:`` is the *gateway* credential the
SDK harnesses inherit when their spec declares no auth — a Copilot token parked
there would be mis-consumed by claude-sdk / codex / pi / openai-agents.

Accepted token types mirror what the Copilot CLI/SDK honors: a fine-grained PAT
(``github_pat_``) with the "Copilot Requests" permission, or an OAuth token from
the GitHub CLI (``gho_``) / Copilot CLI app. Classic PATs (``ghp_``) are NOT
accepted by Copilot.
"""

from __future__ import annotations

from omnigent.errors import OmnigentError
from omnigent.onboarding.provider_config import load_config, resolve_secret

# The secret-store name (and thus ``keychain:<name>``) under which a Copilot
# GitHub token is stored — stable so the setup flow and the resolver agree.
COPILOT_SECRET_NAME = "copilot"

# The dedicated top-level config block and the field that references the token.
COPILOT_CONFIG_KEY = "copilot"
_TOKEN_REF_FIELD = "github_token_ref"
_TOKEN_FIELD = "github_token"

# Ambient GitHub-token env vars, in the precedence the Copilot CLI/SDK honors.
COPILOT_TOKEN_ENV_VARS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")

# Token-shape prefixes Copilot accepts. The check is deliberately *soft* — a
# user may force a non-matching value through — so a future prefix change can
# never lock anyone out of their own token. Classic ``ghp_`` PATs are excluded
# because Copilot rejects them.
_GITHUB_TOKEN_PREFIXES = ("github_pat_", "gho_", "ghu_", "ghs_")


def looks_like_github_copilot_token(value: str) -> bool:
    """Return whether *value* has the shape of a Copilot-capable GitHub token.

    :param value: A pasted/typed candidate token, e.g. ``"gho_AbC123"`` or
        ``"github_pat_..."``.
    :returns: ``True`` when *value* starts with a known Copilot-capable prefix
        (a fine-grained PAT or an OAuth token); ``False`` for an empty string or
        a classic ``ghp_`` PAT (which Copilot rejects).
    """
    return value.startswith(_GITHUB_TOKEN_PREFIXES)


def copilot_github_token_ref(config: dict[str, object] | None = None) -> str | None:
    """Return the configured Copilot GitHub-token secret reference, if any.

    Reads the dedicated ``copilot:`` block of the global config. Both the
    ``github_token_ref`` (``keychain:`` / ``env:``) and an inline ``github_token``
    (``$VAR`` / literal) shapes are accepted so a hand-edited config works too;
    ``github_token_ref`` wins when both are present.

    :param config: A pre-loaded config mapping; ``None`` loads
        ``~/.omnigent/config.yaml`` via :func:`load_config`.
    :returns: The secret reference, e.g. ``"keychain:copilot"`` or
        ``"env:GH_TOKEN"``, or ``None`` when no Copilot token is configured.
    """
    cfg = load_config() if config is None else config
    block = cfg.get(COPILOT_CONFIG_KEY)
    if not isinstance(block, dict):
        return None
    ref = block.get(_TOKEN_REF_FIELD) or block.get(_TOKEN_FIELD)
    return ref if isinstance(ref, str) and ref else None


def resolve_copilot_github_token(config: dict[str, object] | None = None) -> str | None:
    """Resolve the configured Copilot GitHub token to its plaintext value, softly.

    Looks up the ``copilot:`` block's secret reference and resolves it via
    :func:`resolve_secret`. Unlike :func:`resolve_secret`, this **never raises**:
    a missing block or an unresolvable reference (deleted keychain entry, unset
    env var) returns ``None`` so the caller — the copilot spawn-env builder and
    the setup readout — can fall back to an inherited ``GH_TOKEN`` instead of
    crashing a run.

    :param config: A pre-loaded config mapping; ``None`` loads the global config.
    :returns: The plaintext GitHub token, or ``None`` when none is configured or
        it cannot be resolved.
    """
    ref = copilot_github_token_ref(config)
    if ref is None:
        return None
    try:
        return resolve_secret(ref)
    except OmnigentError:
        return None


def copilot_github_token_configured(config: dict[str, object] | None = None) -> bool:
    """Return whether a usable Copilot GitHub token is configured.

    ``True`` only when the ``copilot:`` block names a reference **and** it
    resolves — a dangling reference reads as not-configured so the setup readout
    never claims a credential the runtime can't actually use.

    :param config: A pre-loaded config mapping; ``None`` loads the global config.
    :returns: ``True`` when a Copilot GitHub token is configured and resolvable.
    """
    return resolve_copilot_github_token(config) is not None


def copilot_github_token_settings(ref: str) -> dict[str, object]:
    """Build the ``{"copilot": {...}}`` settings dict that records *ref*.

    Handed to :func:`omnigent.cli._save_global_config` (a shallow update, so it
    replaces the whole ``copilot:`` block) to persist the reference.

    :param ref: The secret reference to record, e.g. ``"keychain:copilot"`` or
        ``"env:GH_TOKEN"``.
    :returns: ``{"copilot": {"github_token_ref": ref}}``.
    """
    return {COPILOT_CONFIG_KEY: {_TOKEN_REF_FIELD: ref}}
