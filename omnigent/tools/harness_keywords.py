"""Harness and vendor names seeded into Omnigent's agent-orchestration text.

Harnesses that load MCP tools lazily (Claude Code's deferred tool search, for
one) find a tool by keyword search over its name and description. An agent
asked to "run Codex as a sub-agent" searches for ``codex``; unless that word
appears in ``sys_session_create`` and friends, the purpose-built path is
invisible and the agent falls back to driving the vendor CLI in a shell.

Kept dependency-free so the claude-native MCP bridge subprocess can import it
cheaply. ``tests/tools/test_harness_keywords.py`` pins it against the native
coding-agent registry and the ACP CLI harnesses so a new harness cannot be
added without being named here.
"""

from __future__ import annotations

CROSS_HARNESS_AGENT_NAMES = (
    "Claude Code, OpenAI Codex, Cursor, OpenCode, Goose, Hermes, Pi, Kiro, "
    "Devin, Qwen Code, Kimi, Antigravity, Grok, jcode"
)
