"""Metadata for native coding-agent terminal integrations."""

from __future__ import annotations

from typing import Any

from omnigent._platform import installed_interactive_shells
from omnigent.harness_aliases import canonicalize_harness
from omnigent.harness_plugins import (
    NativeCodingAgent,
)
from omnigent.harness_plugins import (
    native_agents as _registry_native_agents,
)

NATIVE_CODING_AGENTS = _registry_native_agents()

_BY_AGENT_NAME = {agent.agent_name: agent for agent in NATIVE_CODING_AGENTS}
_BY_HARNESS = {agent.harness: agent for agent in NATIVE_CODING_AGENTS}
_BY_WRAPPER_LABEL = {agent.wrapper_label: agent for agent in NATIVE_CODING_AGENTS}
_BY_TERMINAL_NAME = {agent.terminal_name: agent for agent in NATIVE_CODING_AGENTS}


def native_coding_agent_for_agent_name(name: str | None) -> NativeCodingAgent | None:
    """Return the native coding-agent metadata for *name*, if any."""
    return _BY_AGENT_NAME.get(name or "")


def public_agent_name(name: str | None) -> str | None:
    """Return a user-facing agent name, hiding internal native-UI wrapper names.

    Native coding-agent wrappers carry an internal ``<tool>-native-ui`` agent
    name (e.g. ``pi-native-ui``) that is an Omnigent implementation detail. When
    such a name is projected into tool output the model reads — and may repeat
    back to the user (``sys_session_get_info`` answering "what agent are you?")
    — expose the clean public display name (e.g. ``Pi``) instead, so the
    ``-native-ui`` wrapper name never leaks. Any non-wrapper name, including
    ``None``, passes through unchanged.

    :param name: The raw bound agent name from a session snapshot, or ``None``.
    :returns: The wrapper's display name when *name* is a native-UI wrapper,
        else *name* unchanged.
    """
    agent = native_coding_agent_for_agent_name(name)
    return agent.display_name if agent is not None else name


def native_coding_agent_for_harness(harness: str | None) -> NativeCodingAgent | None:
    """Return the native coding-agent metadata for *harness*, if any.

    Canonicalizes first, so a reversed alias (e.g. ``native-pi``) resolves to
    the same agent as its canonical spelling (``pi-native``) and keeps
    terminal-first presentation labels.
    """
    return _BY_HARNESS.get(canonicalize_harness(harness) or "")


def native_coding_agent_for_wrapper_label(wrapper: str | None) -> NativeCodingAgent | None:
    """Return the native coding-agent metadata for *wrapper*, if any."""
    return _BY_WRAPPER_LABEL.get(wrapper or "")


def native_coding_agent_for_terminal_name(name: str | None) -> NativeCodingAgent | None:
    """Return the native coding-agent metadata for *name*, if any."""
    return _BY_TERMINAL_NAME.get(name or "")


def native_shell_terminal_spec() -> dict[str, Any]:
    """The user-shell terminals every native wrapper declares.

    Native sessions expose the web UI's "+ New shell" affordance, which lets a
    user open an interactive shell. We declare one terminal per installed shell
    (:func:`omnigent._platform.installed_interactive_shells`), keyed and
    commanded by the shell basename (``zsh``/``bash``/``fish``), with the user's
    ``$SHELL`` first so the UI can treat it as the click default and offer the
    rest behind a picker. ``caller_process`` / no sandbox matches the native
    CLI's own unsandboxed stance on the user's workspace. The block is always
    non-empty, which is also what gates the MCP relay's ``sys_terminal_*``
    advertisement.

    :returns: A ``terminals:`` mapping, e.g. ``{"zsh": {...}, "bash": {...}}``,
        with the user's login shell first.
    """
    return {
        shell: {
            "command": shell,
            "allow_cwd_override": True,
            "os_env": {
                "type": "caller_process",
                "cwd": ".",
                "sandbox": {"type": "none"},
            },
        }
        for shell in installed_interactive_shells()
    }
