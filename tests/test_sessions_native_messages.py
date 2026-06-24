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
