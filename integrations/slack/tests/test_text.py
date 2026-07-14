from omnigent_slack.text import (
    split_for_slack,
    strip_bot_mention,
    to_mrkdwn,
    truncate_for_slack,
)


def test_to_mrkdwn_converts_markdown_to_slack_dialect() -> None:
    result = to_mrkdwn("# Title\n\n**bold** and [link](https://example.com)")
    # Bold collapses to single asterisks, headings lose '#', links become <url|text>.
    assert "**" not in result
    assert "*bold*" in result
    assert "<https://example.com|link>" in result
    assert "#" not in result


def test_to_mrkdwn_preserves_code_blocks() -> None:
    result = to_mrkdwn("```python\nprint('hi')\n```")
    assert "```" in result
    assert "print('hi')" in result


def test_strip_bot_mention_removes_target_mention() -> None:
    assert strip_bot_mention("<@B123>   hello   world", "B123") == "hello world"


def test_strip_bot_mention_falls_back_to_first_mention() -> None:
    assert strip_bot_mention("<@B123> hello <@U456>", None) == "hello <@U456>"


def test_truncate_for_slack() -> None:
    result = truncate_for_slack("a" * 20, limit=15)
    assert result.endswith("[truncated]")
    assert len(result) <= 15


def test_split_for_slack_returns_single_chunk_when_within_limit() -> None:
    assert split_for_slack("hello", limit=10) == ["hello"]


def test_split_for_slack_empty_yields_one_empty_chunk() -> None:
    assert split_for_slack("", limit=10) == [""]


def test_split_for_slack_preserves_all_content_and_respects_limit() -> None:
    text = "\n".join(f"line {i}" for i in range(200))
    chunks = split_for_slack(text, limit=40)
    assert all(len(chunk) <= 40 for chunk in chunks)
    assert "".join(chunks) == text


def test_split_for_slack_breaks_on_whitespace_when_possible() -> None:
    chunks = split_for_slack("aaaa bbbb cccc", limit=6)
    # Breaks after a space rather than mid-word.
    assert chunks[0] == "aaaa "
    assert "".join(chunks) == "aaaa bbbb cccc"


def test_split_for_slack_hard_cuts_runs_without_whitespace() -> None:
    text = "a" * 25
    chunks = split_for_slack(text, limit=10)
    assert chunks == ["a" * 10, "a" * 10, "a" * 5]
