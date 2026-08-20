from omnigent_slack.text import strip_bot_mention, truncate_for_slack


def test_strip_bot_mention_removes_target_mention() -> None:
    assert strip_bot_mention("<@B123>   hello   world", "B123") == "hello world"


def test_strip_bot_mention_falls_back_to_first_mention() -> None:
    assert strip_bot_mention("<@B123> hello <@U456>", None) == "hello <@U456>"


def test_truncate_for_slack() -> None:
    result = truncate_for_slack("a" * 20, limit=15)
    assert result.endswith("[truncated]")
    assert len(result) <= 15
