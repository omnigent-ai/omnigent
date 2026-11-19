"""Tests for the kimi-native private ``SYSTEM.md`` writer."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from omnigent.harnesses.kimi_native.system_prompt import write_kimi_system_prompt


def test_global_template_is_extended_without_writing_through_its_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_home = tmp_path / "user-kimi-home"
    user_home.mkdir()
    global_prompt = user_home / "SYSTEM.md"
    global_prompt.write_text("${base_prompt}\nUser rule.\n", encoding="utf-8")
    monkeypatch.setenv("KIMI_CODE_HOME", str(user_home))
    session_home = tmp_path / "kimi-code-home"
    session_home.mkdir()
    # A session home built by an earlier launch links the user's global file.
    (session_home / "SYSTEM.md").symlink_to(global_prompt)

    written = write_kimi_system_prompt(session_home, "Omnigent rule.")

    assert global_prompt.read_text(encoding="utf-8") == "${base_prompt}\nUser rule.\n"
    assert not written.is_symlink()
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert written.read_text(encoding="utf-8") == (
        "${base_prompt}\nUser rule.\n\nOmnigent rule.\n"
    )


def test_base_prompt_template_and_only_kimi_variables_are_neutralised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / "no-user-home"))
    session_home = tmp_path / "kimi-code-home"

    written = write_kimi_system_prompt(session_home, "Keep ${HOME} and ${cwd} and ${base_prompt}.")

    assert written.read_text(encoding="utf-8") == (
        "${base_prompt}\n\nKeep ${HOME} and $​{cwd} and $​{base_prompt}.\n"
    )
