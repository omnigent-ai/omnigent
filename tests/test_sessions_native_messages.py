"""Tests for native terminal message dispatch helpers."""

from __future__ import annotations

import httpx
import pytest

from omnigent.entities.conversation import Conversation
from omnigent.server.schemas import SessionEventInput


def _conversation_with_wrapper(wrapper: str) -> Conversation:
    """
    Build a conversation row carrying one wrapper label.

    :param wrapper: Wrapper label value, e.g. ``"codex-native-ui"``.
    :returns: Conversation with that label and a bound agent_id.
    """
    return Conversation(
        id="conv_test",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_test",
        agent_id="ag_native_test",
        labels={"omnigent.wrapper": wrapper},
    )


def _message_event() -> SessionEventInput:
    """
    Build one user message event for native dispatch tests.

    :returns: Sessions API message input.
    """
    return SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        },
    )


def test_codex_native_session_uses_codex_harness_for_web_messages() -> None:
    """
    Codex-native sessions use the native bypass and dispatch web
    messages into the ``codex-native`` harness instead of the normal
    Omnigent persistence path.
    """
    from omnigent.server.routes import sessions as sessions_routes

    conv = _conversation_with_wrapper("codex-native-ui")

    assert sessions_routes._is_native_terminal_session(conv) is True
    # agent_id must be forwarded so the runner can resolve the harness
    # spec on the first message, before POST /v1/sessions caches it —
    # otherwise the turn falls back to "runner-test-default" and drops.
    assert sessions_routes._build_native_terminal_message_event(conv, _message_event()) == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "hello"}],
        "model": "codex-native-ui",
        "harness": "codex-native",
        "agent_id": "ag_native_test",
    }


def test_kiro_native_session_uses_kiro_harness_for_web_messages() -> None:
    """Kiro-native web messages use the native bypass, like Codex."""
    from omnigent.server.routes import sessions as sessions_routes

    conv = _conversation_with_wrapper("kiro-native-ui")

    assert sessions_routes._is_native_terminal_session(conv) is True
    assert sessions_routes._build_native_terminal_message_event(conv, _message_event()) == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "hello"}],
        "model": "kiro-native-ui",
        "harness": "kiro-native",
        "agent_id": "ag_native_test",
    }


def test_antigravity_native_session_uses_antigravity_harness_for_web_messages() -> None:
    """
    Antigravity-native sessions use the native bypass and dispatch web
    messages into the ``antigravity-native`` harness, mirroring the
    codex/claude native-terminal wrappers. Without this the web UI would
    persist the message itself instead of forwarding it to the agy terminal,
    and the runner would never see the turn.
    """
    from omnigent.server.routes import sessions as sessions_routes

    conv = _conversation_with_wrapper("antigravity-native-ui")

    assert sessions_routes._is_native_terminal_session(conv) is True
    assert sessions_routes._build_native_terminal_message_event(conv, _message_event()) == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "hello"}],
        "model": "antigravity-native-ui",
        "harness": "antigravity-native",
        "agent_id": "ag_native_test",
    }


def test_antigravity_native_runtime_maps_wrapper_to_agy_terminal() -> None:
    """
    The wrapper label resolves to the agy display name, model, harness, and
    the ``antigravity`` runner terminal resource name. The ensure-readiness
    probe (``_ensure_native_terminal_ready``) routes off exactly these two
    helpers, so a missing antigravity branch would 400 the first web message.
    """
    from omnigent.server.routes import sessions as sessions_routes

    conv = _conversation_with_wrapper("antigravity-native-ui")

    display_name, model, harness = sessions_routes._native_terminal_runtime(conv)
    assert (display_name, model, harness) == (
        "Antigravity",
        "antigravity-native-ui",
        "antigravity-native",
    )
    assert sessions_routes._native_terminal_name_for_harness(harness) == "antigravity"


def test_transcript_forwarded_native_sessions_use_native_bypass() -> None:
    """Transcript-forwarded native sessions skip AP-side message persistence."""
    from omnigent.server.routes import sessions as sessions_routes

    assert sessions_routes._is_native_terminal_session(
        _conversation_with_wrapper("claude-code-native-ui")
    )
    assert sessions_routes._is_native_terminal_session(
        _conversation_with_wrapper("codex-native-ui")
    )
    assert sessions_routes._is_native_terminal_session(
        _conversation_with_wrapper("kiro-native-ui")
    )


def test_unknown_wrapper_session_does_not_use_native_bypass() -> None:
    """
    Non-native wrapper labels must not enter the native terminal
    bypass, otherwise Omnigent would skip persistence for regular sessions.
    """
    from omnigent.server.routes import sessions as sessions_routes

    conv = _conversation_with_wrapper("regular-chat")

    assert sessions_routes._is_native_terminal_session(conv) is False


@pytest.mark.parametrize(
    "response,expected",
    [
        # Runner attached a degrade reason → it becomes the banner notice.
        (
            httpx.Response(200, json={"policy_hook_disabled_reason": "codex too old"}),
            "codex too old",
        ),
        # Healthy session: no key → no notice (enforcement active).
        (httpx.Response(200, json={"resource": "view"}), None),
        # Whitespace-only reason is treated as absent (would fail ErrorData).
        (httpx.Response(200, json={"policy_hook_disabled_reason": "   "}), None),
        # Non-dict body (defensive) → no notice.
        (httpx.Response(200, json=["not", "a", "dict"]), None),
        # Non-JSON 2xx body must not crash the readiness probe.
        (httpx.Response(200, text="<<not json>>"), None),
    ],
)
def test_policy_notice_from_ensure_response(
    response: httpx.Response, expected: str | None
) -> None:
    """
    The ensure-response parser fires a banner only for a real reason.

    This gate decides whether a non-fatal "policy not enforced" banner is
    posted. It must return the reason verbatim when present, and ``None``
    (no banner) for a healthy session, a blank reason, a non-dict body, or
    a non-JSON 2xx body — the last of which must not turn a successful
    readiness probe into a crash.
    """
    from omnigent.server.routes import sessions as sessions_routes

    assert sessions_routes._policy_notice_from_ensure_response(response) == expected


# ── native routing is harness-driven, not presentation-driven ────────


def test_custom_native_harness_session_without_wrapper_label_is_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chat-first custom agent on a native harness is still single-writer.

    A user agent that declares ``executor.harness: codex-native`` but is not a
    built-in ``*-native-ui`` wrapper (e.g. a ``polly`` orchestrator) carries NO
    ``omnigent.wrapper`` label — it renders chat-first on purpose. Its runner
    still runs a native transcript forwarder, so the persist decision must
    treat it as native via the RESOLVED harness; otherwise the inbound user
    message is persisted AP-side AND mirrored by the forwarder (double input).
    """
    from omnigent.server.routes import sessions as sessions_routes

    conv = Conversation(
        id="conv_polly",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_polly",
        agent_id="ag_polly",
        labels={},  # chat-first: no wrapper / ui presentation labels
    )
    monkeypatch.setattr(sessions_routes, "_resolve_harness", lambda _c: "codex-native")
    assert sessions_routes._is_native_terminal_session(conv) is True
    # The native dispatch branch resolves runtime strings from the SAME
    # resolver, so a label-less native session no longer raises
    # "Unsupported native terminal session".
    display_name, _model, harness = sessions_routes._native_terminal_runtime(conv)
    assert (display_name, harness) == ("Codex", "codex-native")


def test_custom_sdk_harness_session_is_not_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An SDK-harness session keeps the normal persist-before-forward path.

    SDK harnesses have no transcript forwarder, so the server's single
    persisted copy is correct — the harness fallback must not over-fire.
    """
    from omnigent.server.routes import sessions as sessions_routes

    conv = Conversation(
        id="conv_sdk",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_sdk",
        agent_id="ag_sdk",
        labels={},
    )
    monkeypatch.setattr(sessions_routes, "_resolve_harness", lambda _c: "claude-sdk")
    assert sessions_routes._is_native_terminal_session(conv) is False


def test_wrapper_label_session_is_native_without_resolving_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapper-label path short-circuits before the harness fallback.

    Built-in terminal-first wrapper sessions are recognized by label alone, so
    the (spec-loading) harness resolution never runs for them.
    """
    from omnigent.server.routes import sessions as sessions_routes

    conv = _conversation_with_wrapper("codex-native-ui")

    def _must_not_run(_c: object) -> str:
        raise AssertionError("harness resolution must not run when the wrapper label matches")

    monkeypatch.setattr(sessions_routes, "_resolve_harness", _must_not_run)
    assert sessions_routes._is_native_terminal_session(conv) is True
