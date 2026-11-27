"""Native editor ownership without a CLI, tmux, server or provider process."""

from __future__ import annotations

import math
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import psutil
import pytest

from omnigent.harnesses.claude_native import editor_owner
from omnigent.harnesses.claude_native.editor_owner import (
    NativeDraftOwner,
    NativeEditorProcess,
    exported_draft_matches_owner,
    observe_native_editor_process,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX editor-file ownership")


@pytest.mark.parametrize("with_shell", [False, True], ids=["editor", "hook-through-shell"])
def test_hook_and_editor_observe_the_same_live_selected_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, with_shell: bool
) -> None:
    binary = tmp_path / "claude"
    binary.touch()
    selected = MagicMock(spec=psutil.Process)
    selected.pid = 71
    selected.exe.return_value = str(binary)
    selected.create_time.return_value = 123.5
    selected.is_running.return_value = True
    parents = [selected]
    if with_shell:
        shell = MagicMock(spec=psutil.Process)
        shell.exe.return_value = str(tmp_path)
        parents.insert(0, shell)
    process = MagicMock()
    process.parents.return_value = parents
    monkeypatch.setattr(
        editor_owner,
        "psutil",
        SimpleNamespace(Process=MagicMock(return_value=process), Error=psutil.Error),
    )
    assert observe_native_editor_process(binary, environment={"TMUX_PANE": "%7"}) == (
        NativeEditorProcess(71, 123.5, str(binary), "%7")
    )


@pytest.mark.parametrize(
    "problem", ["missing", "other-executable", "gone", "unreadable", "non-finite", "no-pane"]
)
def test_unverified_native_processes_cannot_supply_editor_ownership(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, problem: str
) -> None:
    binary = tmp_path / "claude"
    if problem != "missing":
        binary.touch()
    selected = MagicMock(spec=psutil.Process)
    selected.pid = 71
    selected.exe.return_value = str(tmp_path if problem == "other-executable" else binary)
    selected.create_time.return_value = math.nan if problem == "non-finite" else 123.5
    selected.is_running.return_value = problem != "gone"
    if problem == "unreadable":
        selected.exe.side_effect = psutil.AccessDenied(71)
    process = MagicMock()
    process.parents.return_value = [selected]
    monkeypatch.setattr(
        editor_owner,
        "psutil",
        SimpleNamespace(Process=MagicMock(return_value=process), Error=psutil.Error),
    )
    environment = {} if problem == "no-pane" else {"TMUX_PANE": "%7"}
    assert observe_native_editor_process(binary, environment=environment) is None


@pytest.mark.parametrize(
    "difference",
    ["none", "bridge-session", "native-session", "pid", "incarnation", "executable", "pane"],
)
def test_exported_text_requires_both_sessions_and_the_native_process_incarnation(
    tmp_path: Path, difference: str
) -> None:
    expected = NativeDraftOwner(
        "conversation", "native", NativeEditorProcess(71, 123.5, "/cli", "%7")
    )
    current = expected
    if difference == "bridge-session":
        current = replace(current, bridge_session_id="other-conversation")
    elif difference == "native-session":
        current = replace(current, native_session_id="other-native")
    elif difference != "none":
        field, value = {
            "pid": ("pid", 72),
            "incarnation": ("created_at", 124.5),
            "executable": ("executable", "/other-cli"),
            "pane": ("pane_id", "%8"),
        }[difference]
        current = replace(current, process=replace(current.process, **{field: value}))
    source = tmp_path / "native-draft.md"
    draft = "  application-wrapped draft\r\n\nsecond line 😀  \n"
    source.write_bytes(draft.encode())
    assert exported_draft_matches_owner(
        source,
        expected_text=draft,
        expected_owner=expected,
        current_owner=lambda: current,
        cancelled=lambda: False,
    ) is (difference == "none")
    assert source.read_bytes() == draft.encode()


@pytest.mark.parametrize(
    "change",
    ["trailing-newline", "leading-space", "flattened-lines", "changed-owner", "cancelled"],
)
def test_changed_or_cancelled_exports_are_rejected_without_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: str
) -> None:
    expected = NativeDraftOwner(
        "conversation", "native", NativeEditorProcess(71, 123.5, "/cli", "%7")
    )
    source = tmp_path / "native-draft.md"
    draft = "  wrapped draft\n\nsecond line 😀  \n"
    source.write_bytes(draft.encode())
    claimed = {
        "trailing-newline": draft[:-1],
        "leading-space": draft[1:],
        "flattened-lines": draft.replace("\n", " "),
    }.get(change, draft)
    owner = MagicMock(side_effect=[expected, None] if change == "changed-owner" else None)
    owner.return_value = expected
    cancelled = MagicMock(side_effect=[False, True] if change == "cancelled" else None)
    cancelled.return_value = False
    opened: list[int] = []
    closed: list[int] = []

    def open_export(*args: object) -> int:
        descriptor = os.open(*args)
        opened.append(descriptor)
        return descriptor

    def close_export(descriptor: int) -> None:
        closed.append(descriptor)
        os.close(descriptor)

    monkeypatch.setattr(
        editor_owner,
        "os",
        SimpleNamespace(
            open=open_export,
            close=close_export,
            read=os.read,
            fstat=os.fstat,
            getuid=os.getuid,
            O_RDONLY=os.O_RDONLY,
            O_NOFOLLOW=os.O_NOFOLLOW,
            O_NONBLOCK=os.O_NONBLOCK,
        ),
    )
    assert not exported_draft_matches_owner(
        source,
        expected_text=claimed,
        expected_owner=expected,
        current_owner=owner,
        cancelled=cancelled,
    )
    assert opened == closed and len(opened) == 1
    assert source.read_bytes() == draft.encode()
