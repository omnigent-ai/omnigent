"""An error item's text as the server's debug log shows it."""

from __future__ import annotations

from omnigent.server.routes._sessions.helpers import _error_item_log_text

# A URL credential whose "@" lies past the redacted prefix, where the sanitizer can't see it.
_CREDENTIAL_PAST_THE_PREFIX = (
    "x" * 270 + " https://probe-user:" + "P" * 3900 + "@example.invalid/path"
)


def test_a_short_text_is_redacted_whole() -> None:
    assert _error_item_log_text("Pi model error: api_key=fakeApiKey456") == (
        "Pi model error: api_key=[REDACTED]"
    )


def test_a_redacted_text_is_clipped_with_the_remainder_counted() -> None:
    assert _error_item_log_text("x" * 400) == "x" * 300 + "… (+100 chars)"


def test_a_credential_running_past_the_redacted_prefix_is_withheld() -> None:
    text = _error_item_log_text(_CREDENTIAL_PAST_THE_PREFIX)

    assert text == f"… (+{len(_CREDENTIAL_PAST_THE_PREFIX)} chars)"


def test_a_long_text_keeps_its_whole_lines_before_the_prefix_bound() -> None:
    head = "Pi model error: overloaded\nretry via https://probe-user:probe-pw@example.invalid"
    message = f"{head}\n{_CREDENTIAL_PAST_THE_PREFIX}"

    text = _error_item_log_text(message)

    assert text == (
        "Pi model error: overloaded\nretry via https://[REDACTED]@example.invalid"
        f"… (+{len(message) - len(head)} chars)"
    )


def test_the_left_out_count_stays_positive_when_redaction_shrinks_the_text() -> None:
    # The kept line loses an escape to the sanitizer, so it ends up shorter than the clip.
    message = "boom\x1b[31m\n" + "x" * 4087

    assert _error_item_log_text(message) == "boom… (+4088 chars)"
