"""Tests for cursor-native cold-resume arg injection.

``_cursor_native_resume_args`` builds the ``["--resume", chat_id]`` suffix
that ``_auto_create_cursor_terminal`` appends to cursor-agent's launch args
when the Omnigent session already has an ``external_session_id`` (set by the
forwarder the first time it discovers the cursor chat store). A missing or
empty id means a brand-new session — no ``--resume`` is injected.
"""

from __future__ import annotations

import pytest

from omnigent.runner.app import _cursor_native_resume_args


@pytest.mark.parametrize(
    ("chat_id", "existing_args", "expected"),
    [
        # Brand-new session — forwarder hasn't written external_session_id yet.
        (None, [], []),
        ("", [], []),
        # Cold resume with a valid chat_id and no prior --resume in args.
        ("chat-uuid-abc123", [], ["--resume", "chat-uuid-abc123"]),
        ("chat-uuid-abc123", ["--approve-mcps"], ["--resume", "chat-uuid-abc123"]),
        # User already passed --resume via passthrough args — don't duplicate it.
        ("chat-uuid-abc123", ["--resume", "other-id"], []),
        # --resume combined with other flags.
        ("chat-uuid-abc123", ["--approve-mcps", "--resume", "other-id"], []),
    ],
    ids=[
        "no-chat-id-none",
        "no-chat-id-empty",
        "fresh-args",
        "with-other-flags",
        "resume-already-present",
        "resume-present-with-other-flags",
    ],
)
def test_cursor_native_resume_args(
    chat_id: str | None,
    existing_args: list[str],
    expected: list[str],
) -> None:
    assert _cursor_native_resume_args(chat_id, existing_args) == expected
