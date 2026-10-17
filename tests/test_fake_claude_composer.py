"""The scripted native panes consume the editing protocol they receive."""

import pytest

from tests._helpers.fake_claude_composer import FakeClaudeComposer


def _feed(composer: FakeClaudeComposer, data: bytes) -> list[str]:
    submissions = []
    for byte in data:
        submitted = composer.feed(bytes([byte]))
        if submitted is not None:
            submissions.append(submitted)
    return submissions


@pytest.mark.parametrize("keys", [b"\x01\x0b", b"\x1b[97;5u\x1b[107;5u"])
def test_clear_keys_remove_the_draft_without_becoming_text(keys: bytes) -> None:
    composer = FakeClaudeComposer()
    _feed(composer, b"stale draft")
    assert _feed(composer, keys) == []
    assert composer.draft == ""
    assert _feed(composer, b"replacement\r") == ["replacement"]
    assert _feed(composer, b"\r") == []


def test_kill_keys_keep_their_line_and_cursor_semantics() -> None:
    composer = FakeClaudeComposer()
    _feed(composer, b"\x1b[200~first\rsecond\x1b[201~")
    _feed(composer, b"\x1b[107;5u")
    assert composer.draft == "first\nsecond"
    _feed(composer, b"\x1b[117;5u")
    assert composer.draft == "first\n"
    _feed(composer, b"\x1b[117;5u")
    assert composer.draft == "first"
    _feed(composer, b"\x1b[117;5u")
    assert composer.draft == ""


def test_bracketed_paste_keeps_newlines_until_submit() -> None:
    composer = FakeClaudeComposer()
    assert _feed(composer, b"\x1b[200~first\rsecond\x1b[201~") == []
    assert composer.draft == "first\nsecond"
    assert _feed(composer, b"\r") == ["first\nsecond"]


def test_unknown_escape_sequences_never_leak_into_the_draft() -> None:
    composer = FakeClaudeComposer()
    _feed(composer, b"before\x1b[999~after")
    assert composer.draft == "beforeafter"
