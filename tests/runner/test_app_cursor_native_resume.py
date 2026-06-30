"""Tests for cursor-native cold-resume arg injection.

``_cursor_native_resume_args`` builds the ``["--resume", chat_id]`` suffix
that ``_auto_create_cursor_terminal`` appends to cursor-agent's launch args
when the Omnigent session already has an ``external_session_id`` (set by the
forwarder the first time it discovers the cursor chat store). A missing or
empty id means a brand-new session — no ``--resume`` is injected. A malformed
id (not a UUID-shaped hex+dash string) is rejected defensively.
"""

from __future__ import annotations

import pytest

from omnigent.runner.app import _cursor_native_resume_args

# A real cursor chat id is a UUID (hex + dashes).
_CHAT = "0ef42bbf-3b80-4bec-ac39-ca46531cbc47"


@pytest.mark.parametrize(
    ("chat_id", "existing_args", "expected"),
    [
        # Brand-new session — forwarder hasn't written external_session_id yet.
        (None, [], []),
        ("", [], []),
        # Cold resume with a valid chat_id and no prior --resume in args.
        (_CHAT, [], ["--resume", _CHAT]),
        (_CHAT, ["--approve-mcps"], ["--resume", _CHAT]),
        # User already passed --resume via passthrough args — don't duplicate it.
        (_CHAT, ["--resume", "other-id"], []),
        (_CHAT, ["--approve-mcps", "--resume", "other-id"], []),
        # The joined ``--resume=<id>`` passthrough form must also dedup.
        (_CHAT, ["--resume=other-id"], []),
        (_CHAT, ["--approve-mcps", "--resume=other-id"], []),
    ],
    ids=[
        "no-chat-id-none",
        "no-chat-id-empty",
        "fresh-args",
        "with-other-flags",
        "resume-already-present",
        "resume-present-with-other-flags",
        "resume-equals-form",
        "resume-equals-form-with-other-flags",
    ],
)
def test_cursor_native_resume_args(
    chat_id: str | None,
    existing_args: list[str],
    expected: list[str],
) -> None:
    assert _cursor_native_resume_args(chat_id, existing_args) == expected


@pytest.mark.parametrize(
    "bad_id",
    [
        "chat-uuid-abc123",  # contains non-hex letters
        "deadbeef",  # hex but not UUID-shaped
        "----",  # dashes only
        "0",  # single digit
        "../../etc/passwd",  # path traversal shape
        "id with spaces",
        "$(rm -rf /)",
        "0ef42bbf;reboot",
        "0ef42bbf-3b80-4bec-ac39-ca46531cbc47x",  # trailing junk
    ],
    ids=[
        "non-hex",
        "short-hex",
        "dashes-only",
        "single-digit",
        "traversal",
        "spaces",
        "shell",
        "semicolon",
        "trailing-junk",
    ],
)
def test_malformed_chat_id_is_rejected(bad_id: str) -> None:
    """A chat id that isn't UUID-shaped is never injected as a --resume arg.

    The strict UUID guard (shared with the runner / CLI via
    ``is_valid_cursor_chat_id``) keeps a malformed id out of the argv. The
    runner logs the rejection at its single validation site; this function
    just returns no args.
    """
    assert _cursor_native_resume_args(bad_id, []) == []
