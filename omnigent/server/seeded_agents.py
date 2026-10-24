"""
Operator control over which packaged built-in agents a deployment offers.

The server seeds a fixed roster on every lifespan startup — the native CLI
harnesses, the builtin ACP rows, polly and debby — and the Web UI's
new-session picker lists whatever ``GET /v1/agents`` returns. A deployment
that only runs its own agents has no way to trim that roster.

:data:`SEEDED_AGENTS_ENV` names the packaged built-ins to keep:

- **unset** → seed and serve every packaged built-in (the default).
- **set** → seed and serve only the named ones. An empty value keeps none.

Agents supplied through ``OMNIGENT_BUILTIN_AGENT_DIRS`` are never filtered:
they are the operator's own roster, which is the whole point of trimming the
packaged one.

The suppressed names live on ``app.state.suppressed_agent_names``, written
once per lifespan startup and read by ``GET /v1/agents``, so two apps in one
process (and two tests) never share one roster.

Suppression hides, it never deletes. ``conversations.agent_id`` is a
cascade-delete foreign key onto ``agents.id``, so removing a seeded row would
take every conversation that ever used it. A suppressed agent keeps its row
and its history; only the discovery list omits it, and clearing the env var
brings it back.
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
