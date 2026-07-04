# tests/inner/test_opencode_helpers.py
import pytest

from omnigent.inner.opencode_executor import (
    _latest_user_text,
    _parse_truthy,
    _split_provider_model,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("0", False),
        ("", False),
        (None, False),
        ("nope", False),
    ],
)
def test_parse_truthy(raw, expected):
    assert _parse_truthy(raw) is expected


def test_split_provider_model_with_slash():
    assert _split_provider_model("anthropic/claude-sonnet-4-5") == (
        "anthropic",
        "claude-sonnet-4-5",
    )


def test_split_provider_model_no_slash():
    assert _split_provider_model("gpt-5") == (None, "gpt-5")


def test_split_provider_model_none():
    assert _split_provider_model(None) == (None, None)


def test_latest_user_text_plain_string():
    msgs = [{"role": "user", "content": "hello"}]
    assert _latest_user_text(msgs) == "hello"


def test_latest_user_text_blocks():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "a"},
                {"type": "input_text", "text": "b"},
            ],
        }
    ]
    assert _latest_user_text(msgs) == "a\nb"


def test_latest_user_text_prefers_last_user():
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]
    assert _latest_user_text(msgs) == "second"
