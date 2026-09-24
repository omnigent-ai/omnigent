"""Workspace resolution must not depend on a stale process cwd."""

from pathlib import Path

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.native import orchestration


@pytest.mark.parametrize("source", ["session", "environment"])
def test_explicit_workspace_works_without_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path))
    session_workspace = str(workspace) if source == "session" else None
    if source == "environment":
        monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))

    def missing_cwd() -> Path:
        raise FileNotFoundError("process cwd was removed")

    monkeypatch.setattr(Path, "cwd", missing_cwd)
    assert orchestration._claude_session_workspace(session_workspace) == workspace


@pytest.mark.parametrize("source", ["session", "environment"])
@pytest.mark.parametrize("kind", ["missing", "file"])
def test_invalid_explicit_workspace_does_not_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, kind: str
) -> None:
    workspace = tmp_path / "project"
    if kind == "file":
        workspace.write_text("not a directory")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path))
    session_workspace = str(workspace) if source == "session" else None
    if source == "environment":
        monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))

    with pytest.raises(OmnigentError) as failure:
        orchestration._claude_session_workspace(session_workspace)
    assert failure.value.code == ErrorCode.WORKSPACE_MISSING


def test_implicit_workspace_uses_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_RUNNER_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)
    assert orchestration._claude_session_workspace(None) == tmp_path


def test_workspace_keeps_symlink_path_for_resume(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    linked_workspace = tmp_path / "project-link"
    linked_workspace.symlink_to(workspace, target_is_directory=True)
    assert orchestration._claude_session_workspace(str(linked_workspace)) == linked_workspace


def test_missing_implicit_cwd_is_a_workspace_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_RUNNER_WORKSPACE", raising=False)

    def missing_cwd() -> Path:
        raise FileNotFoundError("process cwd was removed")

    monkeypatch.setattr(Path, "cwd", missing_cwd)
    with pytest.raises(OmnigentError) as failure:
        orchestration._claude_session_workspace(None)
    assert failure.value.code == ErrorCode.WORKSPACE_MISSING


def test_workspace_error_has_safe_recovery_message() -> None:
    error = OmnigentError(
        "private path and implementation details", code=ErrorCode.WORKSPACE_MISSING
    )
    payload = orchestration._native_terminal_start_error_payload(
        error, "Claude", session_id="conv_workspace"
    )
    assert payload["code"] == ErrorCode.WORKSPACE_MISSING
    assert "workspace" in payload["message"]
    assert "Restore" in payload["message"]
    assert "restart" in payload["message"]
    assert "new session" in payload["message"]
    assert "private path" not in payload["message"]
    assert payload["error_id"] in payload["message"]


def test_unrelated_missing_file_is_not_a_workspace_error() -> None:
    payload = orchestration._native_terminal_start_error_payload(
        FileNotFoundError("missing executable"), "Claude", session_id="conv_workspace"
    )
    assert payload["code"] == "native_terminal_start_failed"
