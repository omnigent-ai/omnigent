"""Shared harness-name alias helpers.

Keep user-facing shorthand spellings at the edges while the rest of
Omnigent continues to use canonical harness identifiers internally.
"""

from __future__ import annotations

HARNESS_ALIASES: dict[str, str] = {
    "claude": "claude-sdk",
    # The SDK package / runtime dispatch spelling; specs use "openai-agents".
    "openai-agents-sdk": "openai-agents",
}

# Canonical native-CLI harness spellings. These are the only harnesses that
# own an on-disk runtime transcript (Claude Code's project JSONL / Codex's
# rollout); a fork into one carries history only via a transcript rebuild
# (same-format clone on the same-family diagonal, or build-from-AP-items for
# an SDK source). ``AgentSpec.harness_kind`` returns these canonical
# spellings for native agents, so no executor-type aliasing is needed here.
NATIVE_HARNESSES: frozenset[str] = frozenset(
    {"claude-native", "native-claude", "codex-native", "native-codex"}
)


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

    Native harnesses (Claude Code, Codex) boot from their own on-disk
    runtime transcript and ignore the Omnigent transcript, so a fork into
    one needs a transcript rebuild to carry history. Accepts the canonical
    native spellings that :attr:`AgentSpec.harness_kind` returns
    (``"claude-native"`` / ``"codex-native"`` and the reversed
    ``"native-claude"`` / ``"native-codex"`` forms).

    :param harness: A harness id, e.g. ``"codex-native"`` or ``"claude_sdk"``;
        ``None`` returns ``False``.
    :returns: ``True`` for a native CLI harness, else ``False``.
    """
    if harness is None:
        return False
    return (canonicalize_harness(harness) or harness) in NATIVE_HARNESSES
