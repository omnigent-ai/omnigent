"""Metadata for native coding-agent terminal integrations."""

from __future__ import annotations

from dataclasses import dataclass

from omnigent._wrapper_labels import (
    ANTIGRAVITY_NATIVE_WRAPPER_VALUE,
    CLAUDE_NATIVE_WRAPPER_VALUE,
    CODEX_NATIVE_WRAPPER_VALUE,
    CURSOR_NATIVE_WRAPPER_VALUE,
    GOOSE_NATIVE_WRAPPER_VALUE,
    HERMES_NATIVE_WRAPPER_VALUE,
    KIMI_NATIVE_WRAPPER_VALUE,
    KIRO_NATIVE_WRAPPER_VALUE,
    OPENCODE_NATIVE_WRAPPER_VALUE,
    PI_NATIVE_WRAPPER_VALUE,
    QWEN_NATIVE_WRAPPER_VALUE,
    UI_MODE_LABEL_KEY,
    UI_MODE_TERMINAL_VALUE,
    WRAPPER_LABEL_KEY,
)
from omnigent.harness_aliases import canonicalize_harness


@dataclass(frozen=True)
class NativeCodingAgent:
    """Stable wire metadata for a native coding-agent TUI."""

    key: str
    display_name: str
    agent_name: str
    harness: str
    wrapper_label: str
    terminal_name: str
    subagent_wrapper_label: str | None = None

    @property
    def presentation_labels(self) -> dict[str, str]:
        """Return labels that make sessions render terminal-first."""
        return {
            UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
            WRAPPER_LABEL_KEY: self.wrapper_label,
        }


CLAUDE_NATIVE_CODING_AGENT = NativeCodingAgent(
    key="claude",
    display_name="Claude",
    agent_name="claude-native-ui",
    harness="claude-native",
    wrapper_label=CLAUDE_NATIVE_WRAPPER_VALUE,
    terminal_name="claude",
    subagent_wrapper_label="claude-code-native-ui-subagent",
)

CODEX_NATIVE_CODING_AGENT = NativeCodingAgent(
    key="codex",
    display_name="Codex",
    agent_name="codex-native-ui",
    harness="codex-native",
    wrapper_label=CODEX_NATIVE_WRAPPER_VALUE,
    terminal_name="codex",
    subagent_wrapper_label="codex-native-ui-subagent",
)

PI_NATIVE_CODING_AGENT = NativeCodingAgent(
    key="pi",
    display_name="Pi",
    agent_name="pi-native-ui",
    harness="pi-native",
    wrapper_label=PI_NATIVE_WRAPPER_VALUE,
    terminal_name="pi",
)

OPENCODE_NATIVE_CODING_AGENT = NativeCodingAgent(
    key="opencode",
    display_name="OpenCode",
    agent_name="opencode-native-ui",
    harness="opencode-native",
    wrapper_label=OPENCODE_NATIVE_WRAPPER_VALUE,
    terminal_name="opencode",
    subagent_wrapper_label="opencode-native-ui-subagent",
)

CURSOR_NATIVE_CODING_AGENT = NativeCodingAgent(
    key="cursor",
    display_name="Cursor",
    agent_name="cursor-native-ui",
    harness="cursor-native",
    wrapper_label=CURSOR_NATIVE_WRAPPER_VALUE,
    terminal_name="cursor",
)

KIRO_NATIVE_CODING_AGENT = NativeCodingAgent(
    key="kiro",
    display_name="Kiro",
    agent_name="kiro-native-ui",
    harness="kiro-native",
    wrapper_label=KIRO_NATIVE_WRAPPER_VALUE,
    terminal_name="kiro",
)

GOOSE_NATIVE_CODING_AGENT = NativeCodingAgent(
    key="goose",
    display_name="Goose",
    agent_name="goose-native-ui",
    harness="goose-native",
    wrapper_label=GOOSE_NATIVE_WRAPPER_VALUE,
    terminal_name="goose",
)

ANTIGRAVITY_NATIVE_CODING_AGENT = NativeCodingAgent(
    key="antigravity",
    display_name="Antigravity",
    agent_name="antigravity-native-ui",
    harness="antigravity-native",
    wrapper_label=ANTIGRAVITY_NATIVE_WRAPPER_VALUE,
    terminal_name="antigravity",
)
QWEN_NATIVE_CODING_AGENT = NativeCodingAgent(
    key="qwen",
    display_name="Qwen Code",
    agent_name="qwen-native-ui",
    harness="qwen-native",
    wrapper_label=QWEN_NATIVE_WRAPPER_VALUE,
    terminal_name="qwen",
)

KIMI_NATIVE_CODING_AGENT = NativeCodingAgent(
    key="kimi",
    display_name="Kimi",
    agent_name="kimi-native-ui",
    harness="kimi-native",
    wrapper_label=KIMI_NATIVE_WRAPPER_VALUE,
    terminal_name="kimi",
)

HERMES_NATIVE_CODING_AGENT = NativeCodingAgent(
    key="hermes",
    display_name="Hermes",
    agent_name="hermes-native-ui",
    harness="hermes-native",
    wrapper_label=HERMES_NATIVE_WRAPPER_VALUE,
    terminal_name="hermes",
)

NATIVE_CODING_AGENTS: tuple[NativeCodingAgent, ...] = (
    CLAUDE_NATIVE_CODING_AGENT,
    CODEX_NATIVE_CODING_AGENT,
    PI_NATIVE_CODING_AGENT,
    OPENCODE_NATIVE_CODING_AGENT,
    CURSOR_NATIVE_CODING_AGENT,
    KIRO_NATIVE_CODING_AGENT,
    GOOSE_NATIVE_CODING_AGENT,
    ANTIGRAVITY_NATIVE_CODING_AGENT,
    QWEN_NATIVE_CODING_AGENT,
    KIMI_NATIVE_CODING_AGENT,
    HERMES_NATIVE_CODING_AGENT,
)

_BY_AGENT_NAME = {agent.agent_name: agent for agent in NATIVE_CODING_AGENTS}
_BY_HARNESS = {agent.harness: agent for agent in NATIVE_CODING_AGENTS}
_BY_WRAPPER_LABEL = {agent.wrapper_label: agent for agent in NATIVE_CODING_AGENTS}
_BY_TERMINAL_NAME = {agent.terminal_name: agent for agent in NATIVE_CODING_AGENTS}


def native_coding_agent_for_agent_name(name: str | None) -> NativeCodingAgent | None:
    """Return the native coding-agent metadata for *name*, if any."""
    return _BY_AGENT_NAME.get(name or "")


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
