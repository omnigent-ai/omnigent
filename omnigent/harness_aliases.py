"""Shared harness-name alias helpers.

Keep user-facing shorthand spellings at the edges while the rest of
Omnigent continues to use canonical harness identifiers internally.
"""

from __future__ import annotations

from omnigent.harness_plugins import harness_aliases, native_harnesses

HARNESS_ALIASES: dict[str, str] = harness_aliases()

# Canonical native-CLI harness spellings. These harnesses type messages into
# a resident terminal process and mirror their transcript back to Omnigent, so
# the runner must not replay Omnigent history or treat a completed queue call
# as a full in-process model turn. ``AgentSpec.harness_kind`` returns these
# canonical spellings for native agents, so no executor-type aliasing is needed
# here.
NATIVE_HARNESSES: frozenset[str] = native_harnesses()


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


def native_terminal_name(harness: str | None) -> str | None:
    """Return the tmux terminal short-name a native harness runs its CLI in.

    Native CLI panes are keyed ``(conversation_id, <short-name>, "main")`` in the
    terminal registry, where the short name is the canonical native harness id
    with the ``-native`` suffix dropped — e.g. ``"claude-native"`` -> ``"claude"``,
    ``"native-codex"`` -> ``"codex"``, ``"opencode-native"`` -> ``"opencode"``.

    :param harness: A harness id (canonical or reversed alias), e.g.
        ``"cursor-native"``; ``None`` or a non-native harness returns ``None``.
    :returns: The terminal short-name, e.g. ``"cursor"``, or ``None`` when
        *harness* is not a native CLI harness.
    """
    if not is_native_harness(harness):
        return None
    canonical = canonicalize_harness(harness) or harness
    # Canonical native ids are ``<name>-native``; some accepted aliases keep the
    # reversed ``native-<name>`` spelling (not all are folded by
    # ``canonicalize_harness``), so strip either affix.
    return canonical.removesuffix("-native").removeprefix("native-")
