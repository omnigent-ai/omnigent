"""Unit tests for the commandcode-native CLI launch helpers."""

from __future__ import annotations

from omnigent.harnesses.commandcode_native.main import is_valid_commandcode_session_id


def test_is_valid_commandcode_session_id_accepts_uuid() -> None:
    assert is_valid_commandcode_session_id("33fd92f7-cf15-4ef4-8145-d95d04f679d6")


def test_is_valid_commandcode_session_id_rejects_non_uuid() -> None:
    # The id feeds a transcript-path component and ``cmd --resume`` argv, so
    # anything that could break out of either must not pass.
    assert not is_valid_commandcode_session_id(None)
    assert not is_valid_commandcode_session_id("")
    assert not is_valid_commandcode_session_id("sess")
    assert not is_valid_commandcode_session_id("../escape")
    assert not is_valid_commandcode_session_id("--mod")
    assert not is_valid_commandcode_session_id("33fd92f7-cf15-4ef4-8145-d95d04f679d6 extra")
