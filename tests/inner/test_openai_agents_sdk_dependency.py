"""Compatibility checks for the OpenAI Agents SDK dependency pair."""

from agents.usage import Usage


def test_default_usage_is_compatible_with_openai_models() -> None:
    usage = Usage()

    assert usage.input_tokens_details.cached_tokens == 0
