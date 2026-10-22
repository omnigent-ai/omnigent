"""Terminals a user may open from the workspace rail for a session's agent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnigent.inner.datamodel import TerminalEnvSpec

if TYPE_CHECKING:
    from omnigent.inner.datamodel import AgentDef
    from omnigent.spec.types import AgentSpec

#: Shell offered to the user when the agent declares no ``terminals:``.
DEFAULT_USER_SHELL = "bash"


def user_shell_terminals(spec: AgentSpec | AgentDef | None) -> dict[str, TerminalEnvSpec]:
    """Return the terminals a user may launch for *spec*, keyed by name.

    Declared ``terminals:`` are offered as-is. An agent declaring none gets a
    default ``bash`` shell that inherits the agent's ``os_env`` (it has none of
    its own) and is never offered to the agent as a tool.

    :param spec: The session agent's spec, or ``None`` when none resolves.
    :returns: Terminal specs in offer order; empty when *spec* is ``None``.
    """
    if spec is None:
        return {}
    declared = getattr(spec, "terminals", None)
    if declared:
        return dict(declared)
    return {DEFAULT_USER_SHELL: TerminalEnvSpec(command=DEFAULT_USER_SHELL)}
