from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.server.routes import _host_worktree
from omnigent.server.routes._sessions.helpers import _remove_session_worktree_best_effort
from omnigent.stores.conversation_store import WORKTREE_KEPT_LABEL_KEY

_HOST_ID = "host-worktree-safety"
_SESSION_ID = "session-worktree-safety"
_WORKTREE_PATH = "/repo-worktrees/feature-safety"
_BRANCH = "feature/safety"


def _configure_cleanup(monkeypatch: pytest.MonkeyPatch, inspection: object) -> tuple[Mock, Mock]:
    conversation_store = Mock()
    conversation_store.has_other_live_session_in_workspace.return_value = False
    host_conn = object()
    host_registry = Mock()
    host_registry.get.return_value = host_conn
    monkeypatch.setattr(
        _host_worktree,
        "list_worktrees_on_host",
        AsyncMock(
            return_value=[
                {"path": _WORKTREE_PATH, "branch": _BRANCH, "is_main": False},
            ]
        ),
    )
    monkeypatch.setattr(
        _host_worktree,
        "inspect_worktree_on_host",
        AsyncMock(return_value=inspection),
    )
    remove = AsyncMock()
    monkeypatch.setattr(_host_worktree, "remove_worktree_on_host", remove)
    return conversation_store, host_registry


@pytest.mark.parametrize(
    "inspection, expected",
    [
        (
            _host_worktree.WorktreeInspection(1, 0, True, "origin/main"),
            {
                "dirty_files": 1,
                "unpushed_commits": 0,
                "merged": True,
                "default_ref": "origin/main",
            },
        ),
        (
            _host_worktree.WorktreeInspection(0, 1, True, "origin/main"),
            {
                "dirty_files": 0,
                "unpushed_commits": 1,
                "merged": True,
                "default_ref": "origin/main",
            },
        ),
        (
            _host_worktree.WorktreeInspection(0, 0, False, "origin/main"),
            {
                "dirty_files": 0,
                "unpushed_commits": 0,
                "merged": False,
                "default_ref": "origin/main",
            },
        ),
        (
            _host_worktree.WorktreeInspection(0, 0, None, None),
            {"dirty_files": 0, "unpushed_commits": 0, "merged": None, "default_ref": None},
        ),
    ],
)
async def test_archive_keeps_worktree_when_inspection_is_unsafe(
    monkeypatch: pytest.MonkeyPatch,
    inspection: _host_worktree.WorktreeInspection,
    expected: dict[str, object],
) -> None:
    conversation_store, host_registry = _configure_cleanup(monkeypatch, inspection)

    await _remove_session_worktree_best_effort(
        host_id=_HOST_ID,
        worktree_path=_WORKTREE_PATH,
        branch=_BRANCH,
        delete_branch=False,
        host_registry=host_registry,
        reason="session-archive",
        conversation_store=conversation_store,
        exclude_conversation_id=_SESSION_ID,
    )

    _host_worktree.remove_worktree_on_host.assert_not_awaited()
    stored = conversation_store.set_labels.call_args.args[1]
    assert json.loads(stored[WORKTREE_KEPT_LABEL_KEY]) == expected


async def test_archive_removes_only_after_all_safety_checks_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_store, host_registry = _configure_cleanup(
        monkeypatch,
        _host_worktree.WorktreeInspection(0, 0, True, "origin/main"),
    )

    await _remove_session_worktree_best_effort(
        host_id=_HOST_ID,
        worktree_path=_WORKTREE_PATH,
        branch=_BRANCH,
        delete_branch=False,
        host_registry=host_registry,
        reason="session-archive",
        conversation_store=conversation_store,
        exclude_conversation_id=_SESSION_ID,
    )

    _host_worktree.remove_worktree_on_host.assert_awaited_once()
    assert _host_worktree.remove_worktree_on_host.await_args.kwargs["delete_branch"] is False
    assert conversation_store.set_labels.call_args.args[1][WORKTREE_KEPT_LABEL_KEY] == "{}"


async def test_archive_records_in_use_reason_without_inspecting_or_removing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_store, host_registry = _configure_cleanup(
        monkeypatch,
        _host_worktree.WorktreeInspection(0, 0, True, "origin/main"),
    )
    conversation_store.has_other_live_session_in_workspace.return_value = True

    await _remove_session_worktree_best_effort(
        host_id=_HOST_ID,
        worktree_path=_WORKTREE_PATH,
        branch=_BRANCH,
        delete_branch=False,
        host_registry=host_registry,
        reason="session-archive",
        conversation_store=conversation_store,
        exclude_conversation_id=_SESSION_ID,
    )

    _host_worktree.inspect_worktree_on_host.assert_not_awaited()
    _host_worktree.remove_worktree_on_host.assert_not_awaited()
    stored = conversation_store.set_labels.call_args.args[1]
    assert json.loads(stored[WORKTREE_KEPT_LABEL_KEY]) == {"reason": "in_use"}


async def test_archive_records_unknown_when_inspection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_store, host_registry = _configure_cleanup(
        monkeypatch,
        _host_worktree.WorktreeInspection(0, 0, True, "origin/main"),
    )
    monkeypatch.setattr(
        _host_worktree,
        "inspect_worktree_on_host",
        AsyncMock(side_effect=_host_worktree.WorktreeProxyError("unavailable")),
    )

    await _remove_session_worktree_best_effort(
        host_id=_HOST_ID,
        worktree_path=_WORKTREE_PATH,
        branch=_BRANCH,
        delete_branch=False,
        host_registry=host_registry,
        reason="session-archive",
        conversation_store=conversation_store,
        exclude_conversation_id=_SESSION_ID,
    )

    _host_worktree.remove_worktree_on_host.assert_not_awaited()
    stored = conversation_store.set_labels.call_args.args[1]
    assert json.loads(stored[WORKTREE_KEPT_LABEL_KEY]) == {"reason": "unknown"}
