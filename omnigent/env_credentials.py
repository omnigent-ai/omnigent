"""Helpers for Omnigent-prefixed credential environment variables."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

OMNIGENT_ENV_PREFIX = "OMNIGENT_"

_ENV_REF_RE = re.compile(r"(?<!\$)\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def omnigent_prefixed_env_name(name: str) -> str:
    """Return the Omnigent-prefixed alias for *name*.

    :param name: Environment variable name, e.g. ``"ANTHROPIC_API_KEY"``.
    :returns: ``"OMNIGENT_ANTHROPIC_API_KEY"`` unless *name* is already
        Omnigent-prefixed.
    """
    return name if name.startswith(OMNIGENT_ENV_PREFIX) else f"{OMNIGENT_ENV_PREFIX}{name}"


def env_names_with_omnigent_prefix(name: str) -> tuple[str, ...]:
    """Return the canonical env var name plus its Omnigent-prefixed alias.

    The canonical name stays first so existing deployments keep precedence
    when both names are set.

    :param name: Environment variable name, e.g. ``"OPENAI_API_KEY"``.
    :returns: Candidate names in resolution order.
    """
    prefixed = omnigent_prefixed_env_name(name)
    if prefixed == name:
        return (name,)
    return (name, prefixed)


def getenv_with_omnigent_prefix(
    name: str, environ: Mapping[str, str] | None = None
) -> tuple[str, str] | None:
    """Read *name*, falling back to ``OMNIGENT_<name>`` when unset.

    :param name: Canonical environment variable name.
    :param environ: Optional environment mapping; defaults to ``os.environ``.
    :returns: ``(actual_name, value)`` for the first set candidate, or
        ``None`` when neither exists.
    """
    env = os.environ if environ is None else environ
    for candidate in env_names_with_omnigent_prefix(name):
        value = env.get(candidate)
        if value is not None:
            return candidate, value
    return None


def getenv_nonempty_with_omnigent_prefix(
    name: str, environ: Mapping[str, str] | None = None
) -> tuple[str, str] | None:
    """Read a non-empty env var with ``OMNIGENT_`` fallback.

    :param name: Canonical environment variable name.
    :param environ: Optional environment mapping; defaults to ``os.environ``.
    :returns: ``(actual_name, value)`` for the first non-empty candidate, or
        ``None`` when neither candidate has a non-blank value.
    """
    env = os.environ if environ is None else environ
    for candidate in env_names_with_omnigent_prefix(name):
        value = env.get(candidate)
        if value is not None and value.strip():
            return candidate, value
    return None


def expand_envvars_with_omnigent_prefix(value: str) -> str:
    """Expand ``$VAR`` references with ``OMNIGENT_VAR`` fallback.

    This mirrors ``os.path.expandvars`` for credential paths that already
    support ``$VAR`` references, with one extra rule: if ``VAR`` is unset but
    ``OMNIGENT_VAR`` is set, the prefixed value is used. Unresolved references
    are left intact so the caller's existing unresolved-var check can produce
    its normal error.

    :param value: String that may contain shell-style env references.
    :returns: Expanded string.
    """

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        resolved = getenv_with_omnigent_prefix(name)
        return resolved[1] if resolved is not None else match.group(0)

    return _ENV_REF_RE.sub(_replace, value)
