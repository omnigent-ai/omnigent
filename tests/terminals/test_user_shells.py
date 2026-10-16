"""Unit tests for :mod:`omnigent.terminals.user_shells`."""

from __future__ import annotations

from omnigent.inner.datamodel import AgentDef, TerminalEnvSpec
from omnigent.spec.types import AgentSpec
from omnigent.terminals.user_shells import DEFAULT_USER_SHELL, user_shell_terminals


def test_declared_terminals_are_offered_verbatim_in_spec_order() -> None:
    declared = {
        "zsh": TerminalEnvSpec(command="zsh"),
        "py": TerminalEnvSpec(command="python3"),
    }
    offered = user_shell_terminals(AgentSpec(spec_version=1, terminals=declared))

    assert list(offered) == ["zsh", "py"]
    assert offered["zsh"] is declared["zsh"]
    assert offered["py"] is declared["py"]


def test_agent_without_terminals_offers_default_shell_that_inherits_os_env() -> None:
    offered = user_shell_terminals(AgentSpec(spec_version=1))

    assert list(offered) == [DEFAULT_USER_SHELL]
    shell = offered[DEFAULT_USER_SHELL]
    assert shell.command == DEFAULT_USER_SHELL
    # No os_env of its own: the launch inherits the agent's os_env and sandbox.
    assert shell.os_env is None
    assert shell.allow_cwd_override is False
    assert shell.allow_sandbox_override is False


def test_legacy_agent_def_without_terminals_offers_default_shell() -> None:
    assert list(user_shell_terminals(AgentDef(name="plain"))) == [DEFAULT_USER_SHELL]


def test_no_resolvable_spec_offers_nothing() -> None:
    assert user_shell_terminals(None) == {}
