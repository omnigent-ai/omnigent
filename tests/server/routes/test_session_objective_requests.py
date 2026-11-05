"""Validation of the objective and outcome request bodies of the objectives API."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnigent.server.session_objectives import RecordObjectiveRequest, RecordOutcomeRequest


def _request(**overrides: object) -> RecordOutcomeRequest:
    body: dict[str, object] = {"success": True, "emoji": "✅", "text": "Done"}
    body.update(overrides)
    return RecordOutcomeRequest.model_validate(body)


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
        "👨🏻\u200d👩🏻\u200d👧🏻\u200d👦🏻",
        "🏳️\u200d🌈",
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
        # A selector or skin tone repeated, or tags after anything but the black flag.
        "✅\ufe0f\ufe0f",
        "👍\U0001f3fd\U0001f3fd",
        "🚀\U000e0067",
        # A flag or a keycap cannot be joined into a sequence.
        "🇺🇸\u200d🚀",
        "1\ufe0f\u20e3\u200d1\ufe0f\u20e3",
        "\U0001f680\u200d1\ufe0f\u20e3",
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


@pytest.mark.parametrize("field", ["emoji", "text"])
def test_unpaired_surrogates_are_rejected(field: str) -> None:
    """Raw JSON can carry a lone surrogate, which cannot be stored as UTF-8."""
    with pytest.raises(ValidationError):
        _request(**{field: "a\ud800" if field == "text" else "\ud83d"})


@pytest.mark.parametrize("success", ["yes", "true", 1, None])
def test_success_must_be_a_boolean(success: object) -> None:
    with pytest.raises(ValidationError):
        _request(success=success)


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _request(run_id="r")
    with pytest.raises(ValidationError):
        RecordObjectiveRequest.model_validate({"objective": "Ship it", "run_id": "r"})


def test_objective_id_is_normalized_and_must_be_a_uuid() -> None:
    dashed = "0123abcd-0123-4567-89ab-0123456789ab"
    assert _request(objective_id=dashed).objective_id == dashed.replace("-", "")
    with pytest.raises(ValidationError, match="objective's ID"):
        _request(objective_id="latest")


def test_objective_is_collapsed_and_bounded() -> None:
    def objective(value: str) -> str:
        return RecordObjectiveRequest.model_validate({"objective": value}).objective

    assert objective("  Get the\n nightly  build green ") == "Get the nightly build green"
    assert objective("x" * 200) == "x" * 200
    with pytest.raises(ValidationError, match="at most 200"):
        objective("x" * 201)
    with pytest.raises(ValidationError, match="must not be empty"):
        objective("\t ")
    with pytest.raises(ValidationError, match="valid Unicode"):
        objective("a\ud800")
