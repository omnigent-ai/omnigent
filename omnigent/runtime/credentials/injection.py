"""Map per-user vault secrets to subprocess environment variables (#5).

The per-user vault stores secrets under arbitrary logical *names* (``"github"``,
``"aws_access_key_id"``, …). To run a collaborator's git/aws/etc. action under
*their* credentials, the runner resolves those secrets for the acting user and
injects them into the tool subprocess's environment under the variable names the
relevant CLIs actually read. This module is the pure, host-agnostic name→env
mapping; the runner does the resolving and the injecting.

Two rules, applied per secret:

1. **Known aliases** — a curated table maps common logical names to the
   canonical env var(s). ``"github"`` → ``GITHUB_TOKEN`` *and* ``GH_TOKEN`` (so
   both ``gh`` and raw ``git`` HTTPS helpers see it); ``"aws_secret_access_key"``
   → ``AWS_SECRET_ACCESS_KEY``; and so on.
2. **Env-style passthrough** — a name already in ``UPPER_SNAKE`` form
   (``MY_SERVICE_TOKEN``) is injected verbatim as that variable, so a user can
   wire an arbitrary credential without a table entry.

Anything else is skipped (not injected) rather than guessed at, so a stray
lowercase name can never silently clobber an unrelated variable.
"""

from __future__ import annotations

import re

# Logical vault name (lowercased) → env var names it should populate. A single
# logical secret may feed several vars that different tools read for the same
# credential (e.g. GitHub HTTPS auth is read from GH_TOKEN by `gh` and from
# GITHUB_TOKEN by many actions/helpers).
_KNOWN_ALIASES: dict[str, tuple[str, ...]] = {
    "github": ("GITHUB_TOKEN", "GH_TOKEN"),
    "github_token": ("GITHUB_TOKEN", "GH_TOKEN"),
    "gh_token": ("GITHUB_TOKEN", "GH_TOKEN"),
    "gh": ("GITHUB_TOKEN", "GH_TOKEN"),
    "gitlab": ("GITLAB_TOKEN",),
    "gitlab_token": ("GITLAB_TOKEN",),
    "aws_access_key_id": ("AWS_ACCESS_KEY_ID",),
    "aws_secret_access_key": ("AWS_SECRET_ACCESS_KEY",),
    "aws_session_token": ("AWS_SESSION_TOKEN",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "anthropic_api_key": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "openai_api_key": ("OPENAI_API_KEY",),
}

# A name already shaped like a conventional env var: uppercase letters/digits/
# underscore, not starting with a digit.
_ENV_STYLE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def build_credential_env(secrets: dict[str, str]) -> dict[str, str]:
    """Map resolved per-user secrets to subprocess env vars.

    :param secrets: The acting user's resolved secrets, ``{logical_name:
        secret_value}`` (e.g. ``{"github": "ghp_…", "aws_access_key_id":
        "AKIA…"}``). Empty values are skipped.
    :returns: A flat ``{ENV_VAR: value}`` dict ready to overlay onto a
        subprocess environment. A name maps via :data:`_KNOWN_ALIASES`, else
        passes through verbatim if it is already env-var-shaped, else is
        dropped. Empty input → empty dict (no injection).
    """
    env: dict[str, str] = {}
    for name, value in secrets.items():
        if not name or not value:
            continue
        targets = _KNOWN_ALIASES.get(name.strip().lower())
        if targets is None:
            # No alias — only inject when the name is itself a valid env var,
            # so we never invent a surprising variable from a freeform name.
            candidate = name.strip()
            targets = (candidate,) if _ENV_STYLE.match(candidate) else ()
        for var in targets:
            env[var] = value
    return env


def injectable_credential_names() -> frozenset[str]:
    """The set of known logical vault names this module maps to env vars.

    Useful for the runner to limit how many vault lookups it makes per tool
    call: rather than blindly probing, it resolves only names the agent's
    action plausibly needs. Env-style passthrough names are not enumerable, so
    they are not included here.

    :returns: The known logical names (lowercased), e.g. ``{"github",
        "aws_access_key_id", …}``.
    """
    return frozenset(_KNOWN_ALIASES)
