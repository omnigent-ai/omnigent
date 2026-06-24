"""Shared harness-name alias helpers.

Keep user-facing shorthand spellings at the edges while the rest of
Omnigent continues to use canonical harness identifiers internally.
"""

from __future__ import annotations

from omnigent.runtime.harness_descriptors import harness_alias_map, native_harness_ids

# User-facing shorthand → canonical harness id. Derived from the single
# :data:`~omnigent.runtime.harness_descriptors.HARNESS_DESCRIPTORS`
# registration so it can never drift from the other harness views (the
# conformance suite asserts the parity). Spellings include ``claude`` →
# ``claude-sdk``, ``native-pi`` → ``pi-native``, ``openai-agents-sdk`` →
# ``openai-agents``, the Antigravity aliases, and ``native-opencode`` →
# ``opencode-native``.
HARNESS_ALIASES: dict[str, str] = harness_alias_map()

# Canonical native-CLI harness spellings (plus reversed ``native-<x>``
# aliases, e.g. ``native-claude`` / ``native-codex`` / ``native-cursor``).
# These harnesses type messages into a resident terminal process and mirror
# their transcript back to Omnigent, so the runner must not replay Omnigent
# history or treat a completed queue call as a full in-process model turn.
# Derived from the descriptor registry (claude/codex/pi/cursor-native plus
# opencode-native).
NATIVE_HARNESSES: frozenset[str] = native_harness_ids()


def canonicalize_harness(harness: str | None) -> str | None:
    """Return the canonical harness identifier for *harness*.

    Unknown names are returned unchanged so callers can still produce
    their normal validation error messages.
    """
    if harness is None:
        return None
    return HARNESS_ALIASES.get(harness, harness)


def is_claude_sdk_harness_name(harness: str | None) -> bool:
    """Return ``True`` for the canonical Claude SDK harness and aliases."""
    return canonicalize_harness(harness) == "claude-sdk"


def is_native_harness(harness: str | None) -> bool:
    """Return whether *harness* is a native CLI harness.

    Native harnesses boot a vendor TUI in a terminal and route user messages
    into that running process. Accepts the canonical native spellings that
    :attr:`AgentSpec.harness_kind` returns plus their reversed aliases.

    :param harness: A harness id, e.g. ``"codex-native"`` or ``"claude_sdk"``;
        ``None`` returns ``False``.
    :returns: ``True`` for a native CLI harness, else ``False``.
    """
    if harness is None:
        return False
    return (canonicalize_harness(harness) or harness) in NATIVE_HARNESSES
