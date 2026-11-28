from __future__ import annotations

from omnigent_discord.text import (
    MESSAGE_CHAR_LIMIT,
    normalize_whitespace,
    split_for_messages,
    strip_bot_mention,
    truncate_for_message,
    truncate_option,
)


def test_strip_bot_mention_removes_only_the_bot() -> None:
    assert strip_bot_mention("<@42> hello <@99>", "42") == "hello <@99>"


def test_strip_bot_mention_handles_the_legacy_nickname_form() -> None:
    assert strip_bot_mention("<@!42>   do   it", "42") == "do it"


def test_strip_bot_mention_without_a_known_id_drops_the_first_mention() -> None:
    assert strip_bot_mention("<@42> <@99> hi", None) == "<@99> hi"


def test_normalize_whitespace_collapses_runs() -> None:
    assert normalize_whitespace("  a\n\n b\t c ") == "a b c"


def test_truncate_for_message_marks_the_cut() -> None:
    out = truncate_for_message("x" * 3000)
    assert len(out) <= MESSAGE_CHAR_LIMIT
    assert out.endswith("[truncated]")


def test_truncate_for_message_leaves_short_text_alone() -> None:
    assert truncate_for_message("short") == "short"


def test_truncate_option_elides_with_an_ellipsis() -> None:
    assert truncate_option("y" * 200) == "y" * 99 + "…"


def test_split_for_messages_prefers_a_paragraph_break() -> None:
    text = "a" * 1900 + "\n\n" + "b" * 300
    chunks = split_for_messages(text)
    assert chunks == ["a" * 1900, "b" * 300]


def test_split_for_messages_cuts_an_unbroken_run_at_the_limit() -> None:
    chunks = split_for_messages("z" * 4500)
    assert [len(c) for c in chunks] == [2000, 2000, 500]


def test_split_for_messages_returns_nothing_for_empty_text() -> None:
    assert split_for_messages("") == []


def test_strip_bot_mention_removes_the_bots_role_form_too() -> None:
    # Discord's autocomplete offers a bot's managed role alongside the bot, so
    # the prompt often arrives addressed to `<@&roleid>` — whose id is the
    # role's, not the bot's. Left in place it would ride into the agent prompt.
    assert strip_bot_mention("<@&700000000000000007> do the thing", "42") == "do the thing"


def test_role_mention_mid_sentence_is_left_alone() -> None:
    # Only a leading address is stripped; a role named later is part of what
    # the user actually said.
    assert strip_bot_mention("<@42> ping <@&999> please", "42") == "ping <@&999> please"
