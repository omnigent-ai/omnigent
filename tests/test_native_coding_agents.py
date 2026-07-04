"""Native coding-agent harness lookups, including reversed-alias folding."""

from __future__ import annotations

from omnigent._wrapper_labels import (
    KIRO_NATIVE_WRAPPER_VALUE,
    PI_NATIVE_WRAPPER_VALUE,
    UI_MODE_LABEL_KEY,
    UI_MODE_TERMINAL_VALUE,
    WRAPPER_LABEL_KEY,
)
from omnigent.harness_plugins import KIRO_NATIVE_CODING_AGENT, PI_NATIVE_CODING_AGENT
from omnigent.native_coding_agents import (
    native_coding_agent_for_harness,
    native_coding_agent_for_wrapper_label,
    public_agent_name,
)


def test_native_pi_alias_resolves_like_canonical() -> None:
    """``native-pi`` resolves to the same native agent as ``pi-native``.

    ``AgentSpec.harness_kind`` returns the raw ``executor.config.harness``, so
    an agent authored as ``native-pi`` must still resolve — else fork/switch
    would drop its terminal-first presentation labels. ``canonicalize_harness``
    folds the alias before the lookup.
    """
    agent = native_coding_agent_for_harness("native-pi")
    assert agent is PI_NATIVE_CODING_AGENT
    assert agent is native_coding_agent_for_harness("pi-native")
    assert agent.presentation_labels == {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: PI_NATIVE_WRAPPER_VALUE,
    }


def test_canonical_native_harnesses_resolve() -> None:
    """The canonical native spellings all resolve to their agents."""
    for harness in (
        "claude-native",
        "codex-native",
        "pi-native",
        "cursor-native",
        "kimi-native",
        "kiro-native",
    ):
        assert native_coding_agent_for_harness(harness) is not None


def test_native_kimi_alias_resolves_like_canonical() -> None:
    """``native-kimi`` resolves to the same native agent as ``kimi-native``.

    Mirrors the ``native-pi`` fold: ``canonicalize_harness`` maps the reversed
    spelling to the canonical id so a forked/switched kimi-native agent keeps
    its terminal-first presentation labels.
    """
    agent = native_coding_agent_for_harness("native-kimi")
    assert agent is not None
    assert agent is native_coding_agent_for_harness("kimi-native")
    assert agent.terminal_name == "kimi"


def test_kiro_native_agent_metadata_and_aliases() -> None:
    """Kiro has a canonical native identity and reversed alias."""
    assert KIRO_NATIVE_CODING_AGENT.key == "kiro"
    assert KIRO_NATIVE_CODING_AGENT.display_name == "Kiro"
    assert KIRO_NATIVE_CODING_AGENT.agent_name == "kiro-native-ui"
    assert KIRO_NATIVE_CODING_AGENT.harness == "kiro-native"
    assert KIRO_NATIVE_CODING_AGENT.wrapper_label == KIRO_NATIVE_WRAPPER_VALUE
    assert KIRO_NATIVE_CODING_AGENT.terminal_name == "kiro"
    assert native_coding_agent_for_harness("native-kiro") is KIRO_NATIVE_CODING_AGENT
    assert native_coding_agent_for_harness("kiro-native") is KIRO_NATIVE_CODING_AGENT
    assert (
        native_coding_agent_for_wrapper_label(KIRO_NATIVE_WRAPPER_VALUE)
        is KIRO_NATIVE_CODING_AGENT
    )


def test_unknown_harness_returns_none() -> None:
    """A non-native harness stays unresolved (no terminal presentation)."""
    assert native_coding_agent_for_harness("claude-sdk") is None
    assert native_coding_agent_for_harness(None) is None


def test_public_agent_name_hides_native_ui_wrapper_names() -> None:
    """Native-UI wrapper agent names map to their clean public display name.

    The raw ``<tool>-native-ui`` name is an internal implementation detail; a
    session describing itself (``sys_session_get_info``) must surface the
    display name (e.g. ``Pi``) so the model never repeats ``pi-native-ui`` to
    the user.
    """
    assert public_agent_name("pi-native-ui") == "Pi"
    assert public_agent_name("claude-native-ui") == "Claude"
    assert public_agent_name("codex-native-ui") == "Codex"
    assert public_agent_name("cursor-native-ui") == "Cursor"


def test_public_agent_name_passes_through_regular_names() -> None:
    """Non-wrapper names (and ``None``) are returned unchanged.

    Regular Omnigent agents have user-meaningful names that are safe to expose,
    so only the native-UI wrappers are rewritten.
    """
    assert public_agent_name("researcher") == "researcher"
    assert public_agent_name("nessie") == "nessie"
    assert public_agent_name("") == ""
    assert public_agent_name(None) is None
