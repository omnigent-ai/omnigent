"""
Operator control over which packaged built-in agents a deployment offers.

:data:`SEEDED_AGENTS_ENV` names the packaged built-ins to keep in
``GET /v1/agents``:

- **unset** → offer every packaged built-in (the default).
- **set** → offer only the named ones. An empty value offers none.

Agents supplied through ``OMNIGENT_BUILTIN_AGENT_DIRS`` are never filtered.

Suppression hides, it never deletes: a session bound to a hidden agent keeps
its row and history. See ``deploy/docker/README.md`` for the operator guide.
"""

import os

#: Comma-separated allowlist of packaged built-in agent names to seed and
#: serve, e.g. ``"claude-native-ui,polly"``. Unset means "all".
SEEDED_AGENTS_ENV = "OMNIGENT_SEEDED_AGENTS"


def seeded_agent_allowlist() -> frozenset[str] | None:
    """
    Parse :data:`SEEDED_AGENTS_ENV` into an allowlist of agent names.

    Blank entries are dropped so ``"polly,,debby"`` and a trailing comma
    behave. Names are matched exactly — they are slugs
    (``[a-zA-Z0-9_-]+``), not labels.

    :returns: The allowed names, or ``None`` when the variable is unset
        (seed everything). An empty frozenset means "seed none", which is
        distinct from ``None``.
    """
    raw = os.environ.get(SEEDED_AGENTS_ENV)
    if raw is None:
        return None
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


def seeded_agent_allowed(name: str, allowlist: frozenset[str] | None) -> bool:
    """
    Whether a packaged built-in should be seeded.

    :param name: The packaged built-in's name, e.g. ``"polly"``.
    :param allowlist: Result of :func:`seeded_agent_allowlist`.
    :returns: ``True`` when the allowlist is absent or names this agent.
    """
    return allowlist is None or name in allowlist
