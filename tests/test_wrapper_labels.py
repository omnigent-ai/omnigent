"""
Parity tests for the wrapper-label string constants.

The same key/value pair lives in five places — four Python modules
import them from :mod:`omnigent._wrapper_labels`, and the server's
session routes hold their own copy for the message-bypass gate. A
silent drift between any of these sites would re-introduce the
resume-misroute bug class: the chat REPL would not detect a claude-native
conversation, the resume dispatcher would not route to the wrapper,
or the server bypass would stop firing.

These tests are tiny — they assert the literals match by value.
Cheap to run, catch every refactor that diverges any of the five
sites.
"""

from __future__ import annotations

from omnigent._wrapper_labels import (
    CLAUDE_NATIVE_WRAPPER_VALUE,
    CODEX_NATIVE_WRAPPER_VALUE,
    KIRO_NATIVE_WRAPPER_VALUE,
    PI_NATIVE_WRAPPER_VALUE,
    WRAPPER_LABEL_KEY,
)


def test_claude_native_wrapper_constants_match_claude_native_module() -> None:
    """
    ``omnigent.claude_native`` imports the same key/value pair.

    The wrapper module stamps the label on every claude-native
    session it creates. If its constants ever drift from the shared
    source, the server's bypass gate (which compares by literal
    string) silently stops matching and the claude-native message
    routing breaks.
    """
    from omnigent import claude_native

    assert claude_native._WRAPPER_LABEL_KEY == WRAPPER_LABEL_KEY
    assert claude_native._WRAPPER_LABEL_VALUE == CLAUDE_NATIVE_WRAPPER_VALUE


def test_claude_native_wrapper_constants_match_chat_module() -> None:
    """
    ``omnigent.chat`` imports the same key/value pair.

    The chat module uses these in ``_is_claude_native_conversation``
    to decide whether to redirect a resume to the claude wrapper. A
    drift here would mean ``omnigent attach <claude-id>``
    silently opens an Omnigent REPL on top of a tmux session it can't see —
    the misroute's root cause.
    """
    from omnigent import chat

    assert chat._CLAUDE_NATIVE_WRAPPER_LABEL_KEY == WRAPPER_LABEL_KEY
    assert chat._CLAUDE_NATIVE_WRAPPER_LABEL_VALUE == CLAUDE_NATIVE_WRAPPER_VALUE


def test_claude_native_wrapper_constants_match_picker_module() -> None:
    """
    ``omnigent.repl._resume_picker`` imports the same key/value pair.

    The picker uses these to render the ``[claude]`` Runtime badge in
    the cross-agent picker. A drift would silently downgrade every
    claude-native row to ``[chat]`` in the picker without any other
    symptom.
    """
    from omnigent.repl import _resume_picker

    assert _resume_picker._CLAUDE_NATIVE_WRAPPER_LABEL_KEY == WRAPPER_LABEL_KEY
    assert _resume_picker._CLAUDE_NATIVE_WRAPPER_LABEL_VALUE == CLAUDE_NATIVE_WRAPPER_VALUE


def test_claude_native_wrapper_constants_match_server_routes() -> None:
    """
    ``omnigent.server.routes.sessions`` carries its own copy of the
    same literal pair (used by the message-bypass gate). The server
    module predates the shared constants module and intentionally
    avoids the import for layering reasons (the server should not
    depend on CLI-side code). The values must still match.

    If this test fails the server-side bypass for claude-native
    messages stops gating on the right label.
    """
    from omnigent.server.routes import sessions as sessions_routes

    assert sessions_routes._CLAUDE_NATIVE_WRAPPER_LABEL_KEY == WRAPPER_LABEL_KEY
    assert sessions_routes._CLAUDE_NATIVE_WRAPPER_LABEL_VALUE == CLAUDE_NATIVE_WRAPPER_VALUE


def test_codex_native_wrapper_constants_match_codex_native_module() -> None:
    """
    ``omnigent.codex_native`` imports the same key/value pair.

    The Codex wrapper stamps this label on every codex-native
    session it creates. If it drifts, resume dispatch and the
    server-side native message bypass stop recognizing the session.
    """
    from omnigent import codex_native

    assert codex_native._WRAPPER_LABEL_KEY == WRAPPER_LABEL_KEY
    assert codex_native._WRAPPER_LABEL_VALUE == CODEX_NATIVE_WRAPPER_VALUE


def test_codex_native_wrapper_constants_match_picker_module() -> None:
    """
    ``omnigent.repl._resume_picker`` imports the Codex wrapper value.

    The picker uses it to render the ``[codex]`` Runtime badge.
    """
    from omnigent.native_coding_agents import CODEX_NATIVE_CODING_AGENT

    assert CODEX_NATIVE_CODING_AGENT.wrapper_label == CODEX_NATIVE_WRAPPER_VALUE


def test_codex_native_wrapper_constants_match_server_routes() -> None:
    """
    ``omnigent.server.routes.sessions`` carries its own copy of the
    codex-native wrapper value for the native message-bypass gate.
    """
    from omnigent.server.routes import sessions as sessions_routes

    assert sessions_routes._CLAUDE_NATIVE_WRAPPER_LABEL_KEY == WRAPPER_LABEL_KEY
    assert sessions_routes._CODEX_NATIVE_WRAPPER_LABEL_VALUE == CODEX_NATIVE_WRAPPER_VALUE


def test_pi_native_wrapper_constants_match_pi_native_module() -> None:
    """``omnigent.pi_native`` imports the same key/value pair."""
    from omnigent import pi_native

    assert pi_native._WRAPPER_LABEL_KEY == WRAPPER_LABEL_KEY
    assert pi_native._WRAPPER_LABEL_VALUE == PI_NATIVE_WRAPPER_VALUE


def test_pi_native_wrapper_constants_match_registry() -> None:
    """The native coding-agent registry owns the Pi wrapper metadata."""
    from omnigent.native_coding_agents import PI_NATIVE_CODING_AGENT

    assert PI_NATIVE_CODING_AGENT.agent_name == "pi-native-ui"
    assert PI_NATIVE_CODING_AGENT.harness == "pi-native"
    assert PI_NATIVE_CODING_AGENT.wrapper_label == PI_NATIVE_WRAPPER_VALUE
    assert PI_NATIVE_CODING_AGENT.terminal_name == "pi"


def test_kiro_native_wrapper_constants_match_registry() -> None:
    """The native coding-agent registry owns the Kiro wrapper metadata."""
    from omnigent.native_coding_agents import KIRO_NATIVE_CODING_AGENT

    assert KIRO_NATIVE_CODING_AGENT.agent_name == "kiro-native-ui"
    assert KIRO_NATIVE_CODING_AGENT.harness == "kiro-native"
    assert KIRO_NATIVE_CODING_AGENT.wrapper_label == KIRO_NATIVE_WRAPPER_VALUE
    assert KIRO_NATIVE_CODING_AGENT.terminal_name == "kiro"
