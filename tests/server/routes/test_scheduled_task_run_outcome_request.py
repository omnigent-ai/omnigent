"""Validation of ``PUT /v1/scheduled-tasks/run-outcome`` bodies (``RecordRunOutcomeRequest``)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnigent.server.routes.scheduled_tasks import RecordRunOutcomeRequest


def _request(**overrides: object) -> RecordRunOutcomeRequest:
    body: dict[str, object] = {"session_id": "s", "success": True, "emoji": "✅", "text": "Done"}
    body.update(overrides)
    return RecordRunOutcomeRequest.model_validate(body)


@pytest.mark.parametrize(
    "emoji",
    [
        "🚀",
        " 🚧 ",
        "✔️",
        "1️⃣",
        "#️⃣",
        "🇺🇸",
        "👍🏽",
        "👨‍👩‍👧‍👦",
        "🧑🏾‍💻",
        "🏴󠁧󠁢󠁳󠁣󠁴󠁿",
        "↗️",
    ],
)
def test_one_emoji_is_accepted(emoji: str) -> None:
    """A single emoji passes, including keycaps, flags, skin tones and ZWJ sequences."""
    assert _request(emoji=emoji).emoji == emoji.strip()


@pytest.mark.parametrize(
    "emoji",
    [
        "",
        " ",
        ".",
        "ok",
        "1",
        "你好",
        "🚀🚧",
        "👍🏽👍",
        "🇺",
        "🇺🇸🇰🇷",
        "\u200d🚀",
        "🚀\u200d",
        "\ufe0f",
        # Symbols that are not emoji.
        "∑",
        "±",
        "°",
        "█",
        "€",
    ],
)
def test_anything_but_one_emoji_is_rejected(emoji: str) -> None:
    with pytest.raises(ValidationError, match="exactly one emoji"):
        _request(emoji=emoji)


def test_text_is_collapsed_and_bounded() -> None:
    assert _request(text="  Blocked:\n  CI  ").text == "Blocked: CI"
    assert _request(text="x" * 80).text == "x" * 80
    with pytest.raises(ValidationError, match="at most 80"):
        _request(text="x" * 81)
    with pytest.raises(ValidationError, match="must not be empty"):
        _request(text=" \n ")


@pytest.mark.parametrize("success", ["yes", "true", 1, None])
def test_success_must_be_a_boolean(success: object) -> None:
    with pytest.raises(ValidationError):
        _request(success=success)


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _request(run_id="r")
