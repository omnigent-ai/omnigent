"""Exact exported-file transactions without native CLI or controller processes."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from omnigent.harnesses.claude_native import editor_transaction
from omnigent.harnesses.claude_native.editor_owner import NativeDraftOwner, NativeEditorProcess
from omnigent.harnesses.claude_native.editor_transaction import (
    NativeDraftEdit,
    NativeDraftEditRefused,
)

pytestmark = pytest.mark.posix_only

_DRAFT = "  exact λ draft\nwith trailing spaces  \n"


@pytest.fixture
def exported_draft(tmp_path: Path) -> SimpleNamespace:
    path = tmp_path / "draft.txt"
    path.write_bytes(_DRAFT.encode())
    owner = NativeDraftOwner(
        "conversation", "native-session", NativeEditorProcess(71, 123.5, "/native/claude", "%7")
    )
    observe = MagicMock(return_value=owner)
    cancelled = MagicMock(return_value=False)
    return SimpleNamespace(
        path=path,
        owner=owner,
        observe=observe,
        cancelled=cancelled,
        edit=lambda: NativeDraftEdit(
            path,
            expected_text=_DRAFT,
            expected_owner=owner,
            current_owner=observe,
            cancelled=cancelled,
        ),
    )


def test_release_imports_exact_bytes_and_an_operation_cannot_be_reused(
    exported_draft: SimpleNamespace,
) -> None:
    context = exported_draft
    operation = context.edit()
    with operation as edit:
        assert context.path.read_bytes() == _DRAFT.encode()
        edit.commit("replacement λ\n  ")
        assert context.path.read_bytes() == "replacement λ\n  ".encode()
        edit.release()
        with pytest.raises(NativeDraftEditRefused):
            edit.commit("duplicate")
    assert context.path.read_bytes() == "replacement λ\n  ".encode()
    with pytest.raises(NativeDraftEditRefused):
        operation.__enter__()


@pytest.mark.parametrize("phase", ["prepared", "committed", "release-cancelled", "partial-write"])
def test_cancel_restores_the_original_even_after_commit_or_partial_write(
    monkeypatch: pytest.MonkeyPatch, exported_draft: SimpleNamespace, phase: str
) -> None:
    context = exported_draft
    original_write = os.pwrite
    calls = 0

    def interrupted_write(descriptor: int, data: bytes, offset: int) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_write(descriptor, data[:3], offset)
        if calls == 2:
            raise OSError("Interrupted write")
        return original_write(descriptor, data, offset)

    if phase == "partial-write":
        monkeypatch.setattr(editor_transaction.os, "pwrite", interrupted_write)
    with context.edit() as edit:
        if phase == "partial-write":
            with pytest.raises(OSError, match="Interrupted write"):
                edit.commit("replacement")
        elif phase != "prepared":
            edit.commit("")
            assert context.path.read_bytes() == b""
        context.cancelled.return_value = True
        if phase == "release-cancelled":
            with pytest.raises(NativeDraftEditRefused, match="cancelled"):
                edit.release()
    assert context.path.read_bytes() == _DRAFT.encode()


@pytest.mark.parametrize("failure", ["notification", "cancellation"])
def test_failed_release_restores_before_the_editor_returns(
    exported_draft: SimpleNamespace, failure: str
) -> None:
    context = exported_draft
    with pytest.raises(OSError if failure == "notification" else NativeDraftEditRefused):
        with context.edit() as edit:
            edit.commit("")
            edit.release()
            if failure == "notification":
                raise OSError("Notification failed")
            context.cancelled.return_value = True
    assert context.path.read_bytes() == _DRAFT.encode()


@pytest.mark.parametrize("phase", ["before-prepare", "before-commit", "after-commit"])
@pytest.mark.parametrize("change", ["text", "replacement", "symlink", "hardlink", "owner"])
def test_changed_exports_and_new_user_input_are_preserved(
    exported_draft: SimpleNamespace, tmp_path: Path, phase: str, change: str
) -> None:
    context = exported_draft
    original = tmp_path / "original-inode.txt"
    newer = b"newer user edit\n"

    def change_export() -> None:
        if change == "text":
            context.path.write_bytes(newer)
        elif change == "replacement":
            context.path.rename(original)
            context.path.write_bytes(newer)
        elif change == "symlink":
            context.path.rename(original)
            context.path.symlink_to(original)
        elif change == "hardlink":
            os.link(context.path, original)
        else:
            context.observe.return_value = replace(context.owner, native_session_id="other-native")

    with pytest.raises((NativeDraftEditRefused, OSError)):
        if phase == "before-prepare":
            change_export()
        with context.edit() as edit:
            if phase == "after-commit":
                edit.commit("")
            if phase != "before-prepare":
                change_export()
            if phase == "after-commit":
                edit.release()
            else:
                edit.commit("")
    if change in {"text", "replacement"}:
        assert context.path.read_bytes() == newer
    else:
        assert context.path.read_bytes() == (b"" if phase == "after-commit" else _DRAFT.encode())


def test_exact_bytes_and_exclusive_descriptor_ownership_are_required(
    exported_draft: SimpleNamespace,
) -> None:
    context = exported_draft
    context.path.write_text(_DRAFT.strip())
    with pytest.raises(NativeDraftEditRefused):
        with context.edit():
            pytest.fail("Whitespace normalization cannot establish ownership")
    context.path.write_bytes(_DRAFT.encode())
    with context.edit():
        with pytest.raises(BlockingIOError):
            with context.edit():
                pytest.fail("A second editor cannot claim the same export")
    assert context.path.read_bytes() == _DRAFT.encode()
