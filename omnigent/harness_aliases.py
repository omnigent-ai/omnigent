"""Shared harness-name alias helpers.

Keep user-facing shorthand spellings at the edges while the rest of
Omnigent continues to use canonical harness identifiers internally.
"""

from __future__ import annotations

HARNESS_ALIASES: dict[str, str] = {
    "claude": "claude-sdk",
    "native-kiro": "kiro-native",
    "native-pi": "pi-native",
    # The SDK package / runtime dispatch spelling; specs use "openai-agents".
    "openai-agents-sdk": "openai-agents",
    # User-facing spellings for the Google Antigravity SDK harness; the
    # canonical id is "antigravity" (matches the registry / workflow type).
    "agy": "antigravity",
    "google-antigravity": "antigravity",
    # User-facing spelling for Moonshot AI's Kimi Code CLI; the canonical id
    # is "kimi" (matches the binary and the registry / workflow type).
    "kimi-code": "kimi",
    # User-facing reversed spelling for the Goose native-CLI harness; canonical
    # id is "goose-native".
    "native-goose": "goose-native",
    # Reversed spelling for the native Kimi Code TUI harness; canonical id is
    # "kimi-native" (the SDK/headless harness keeps the bare "kimi" id).
    "native-kimi": "kimi-native",
    # Qwen Code harness alias.
    "qwen-code": "qwen",
    # User-facing reversed spelling for the qwen native-CLI harness; canonical
    # id is "qwen-native" (the ACP-piped harness keeps the bare "qwen" name).
    "native-qwen": "qwen-native",
    # OpenCode native-server harness: the bare ``opencode`` name and the
    # reversed ``native-opencode`` spelling both fold to ``opencode-native``
    # (there is no separate SDK ``opencode`` harness, so the bare name is free).
    "opencode": "opencode-native",
    "native-opencode": "opencode-native",
    # User-facing reversed spelling for the Hermes native-CLI (TUI) harness;
    # canonical id is "hermes-native" (the headless subprocess harness keeps the
    # bare "hermes" name, like goose vs goose-native).
    "native-hermes": "hermes-native",
    # User-facing spelling for the GitHub Copilot SDK harness; the canonical id
    # is "copilot" (matches the registry / workflow type).
    "github-copilot": "copilot",
}

# Canonical native-CLI harness spellings. These harnesses type messages into
# a resident terminal process and mirror their transcript back to Omnigent, so
# the runner must not replay Omnigent history or treat a completed queue call
# as a full in-process model turn. ``AgentSpec.harness_kind`` returns these
# canonical spellings for native agents, so no executor-type aliasing is needed
# here.
NATIVE_HARNESSES: frozenset[str] = frozenset(
    {
        "claude-native",
        "native-claude",
        "codex-native",
        "native-codex",
        "pi-native",
        "native-pi",
        "cursor-native",
        "native-cursor",
        "kiro-native",
        "native-kiro",
        # Native Antigravity (agy) TUI bridge used by ``omnigent antigravity``;
        # the in-process SDK counterpart is the canonical ``antigravity``
        # harness (see HARNESS_ALIASES / runtime/harnesses/__init__.py).
        "antigravity-native",
        "native-antigravity",
        "goose-native",
        "native-goose",
        "qwen-native",
        "native-qwen",
        "opencode-native",
        "native-opencode",
        "kimi-native",
        "native-kimi",
        # Native Hermes (TUI) bridge used by ``omnigent hermes``; the headless
        # subprocess counterpart is the canonical ``hermes`` harness (see
        # HARNESS_ALIASES / runtime/harnesses/__init__.py).
        "hermes-native",
        "native-hermes",
    }
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
