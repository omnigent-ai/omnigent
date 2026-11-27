"""Keep auxiliary title traffic separate from exact native conversation evidence."""

from __future__ import annotations

import copy
import json

import pytest

from tests._helpers import native_claude


@pytest.mark.parametrize(
    "scenario",
    [
        "title-keeps-newline",
        "title-trims-newline",
        "conversation-loses-newline",
        "title-only",
        "duplicate-conversation",
        "wrong-session",
        "wrong-model",
        "missing-title-instructions",
        "changed-title-text",
    ],
)
def test_title_classification_preserves_exact_conversation_requirements(
    scenario: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt = "oracle-marker\n\nsecond line\n"
    model = "oracle-test-model"
    native_session = "native-session"

    def request(text: str, *, title: bool = False) -> dict:
        return {
            "body": {
                "model": model,
                "stream": True,
                "metadata": {"user_id": json.dumps({"session_id": native_session})},
                "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}],
                "tools": [] if title else [{"name": "oracle-tool"}],
                "system": (
                    "You are naming a coding session so the user can pick it out "
                    "of a long list of sessions."
                    if title
                    else "Conversation instructions."
                ),
            }
        }

    title_text = prompt if scenario == "title-keeps-newline" else prompt.removesuffix("\n")
    conversation = request(prompt)
    title = request(
        f"<session>\n{title_text}\n</session>\n\n"
        "Write the title in the predominant language of the session.",
        title=True,
    )
    requests = [conversation, title]
    if scenario == "conversation-loses-newline":
        conversation["body"]["messages"][0]["content"][0]["text"] = prompt.removesuffix("\n")
    elif scenario == "title-only":
        requests = [title]
    elif scenario == "duplicate-conversation":
        requests.append(copy.deepcopy(conversation))
    elif scenario == "wrong-session":
        conversation["body"]["metadata"]["user_id"] = json.dumps({"session_id": "another"})
    elif scenario == "wrong-model":
        conversation["body"]["model"] = "another-model"
    elif scenario == "missing-title-instructions":
        title["body"]["system"] = "Unrelated instructions."
    elif scenario == "changed-title-text":
        title["body"]["messages"][0]["content"][0]["text"] = title["body"]["messages"][0][
            "content"
        ][0]["text"].replace("second", "changed")
    monkeypatch.setattr(native_claude, "get_mock_message_requests", lambda _url: requests)
    kwargs = {"native_session": native_session, "user_text": prompt}
    if scenario in {"title-keeps-newline", "title-trims-newline"}:
        assert (
            native_claude._assert_mock_claude_turn("unused", "oracle-marker", model, **kwargs)
            == (conversation["body"])
        )
    else:
        with pytest.raises(AssertionError):
            native_claude._assert_mock_claude_turn("unused", "oracle-marker", model, **kwargs)
