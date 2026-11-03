"""Unit tests for the upload_file built-in tool."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.tools.base import ToolContext
from omnigent.tools.builtins import upload_file
from omnigent.tools.builtins.upload_file import UploadFileTool


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """
    Create a workspace directory with a test file.

    :param tmp_path: Pytest temp directory.
    :returns: The workspace path.
    """
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    (ws / "results.csv").write_text("a,b\n1,2\n3,4\n")
    subdir = ws / "output"
    subdir.mkdir()
    (subdir / "deep.txt").write_text("deep file content")
    return ws


@pytest.fixture()
def tool_ctx(workspace: Path) -> ToolContext:
    """
    Build a ToolContext with the test workspace.

    :param workspace: The workspace directory.
    :returns: A configured ToolContext.
    """
    return ToolContext(
        task_id="task_001",
        agent_id="ag_001",
        workspace=workspace,
        conversation_id="conv_001",
    )


def test_upload_file_stores_and_returns_file_id(
    workspace: Path,
    tool_ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``upload_file`` reads a file from the workspace, stores it
    in the file store + artifact store, and returns a JSON
    result with ``file_id``, ``filename``, and ``content_type``.
    """
    stored_files: list[dict[str, Any]] = []
    stored_artifacts: list[tuple[str, bytes]] = []

    class _FakeFileRecord:
        """Minimal file record returned by create()."""

        def __init__(self, file_id: str) -> None:
            self.id = file_id

    class _FakeFileStore:
        """Stub that captures create() calls."""

        def create(
            self,
            filename: str,
            bytes: int,
            content_type: str,
            session_id: str | None = None,
        ) -> _FakeFileRecord:
            """
            Record the create call and return a fake file record.

            :param filename: Original filename.
            :param bytes: File size.
            :param content_type: MIME type.
            :param session_id: Owning session id.
            :returns: A fake file record with a predictable ID.
            """
            stored_files.append(
                {
                    "filename": filename,
                    "bytes": bytes,
                    "content_type": content_type,
                    "session_id": session_id,
                }
            )
            return _FakeFileRecord("file_test123")

    class _FakeArtifactStore:
        """Stub that captures put() calls."""

        def put(self, key: str, data: bytes) -> None:
            """
            Record the put call.

            :param key: Artifact key (file_id).
            :param data: File bytes.
            """
            stored_artifacts.append((key, data))

    # _upload() does: from omnigent.runtime import get_file_store, get_artifact_store
    # The import resolves at call time, so patch the runtime module.
    monkeypatch.setattr(
        "omnigent.runtime.get_file_store",
        lambda: _FakeFileStore(),
    )
    monkeypatch.setattr(
        "omnigent.runtime.get_artifact_store",
        lambda: _FakeArtifactStore(),
    )

    tool = UploadFileTool()
    result = tool.invoke('{"path": "chart.png"}', tool_ctx)
    parsed = json.loads(result)

    # File was stored with correct metadata.
    assert len(stored_files) == 1, f"Expected 1 file_store.create call, got {len(stored_files)}."
    assert stored_files[0]["filename"] == "chart.png"
    assert stored_files[0]["content_type"] == "image/png"
    assert stored_files[0]["session_id"] == "conv_001"

    # Binary was stored in artifact store.
    assert len(stored_artifacts) == 1, (
        f"Expected 1 artifact_store.put call, got {len(stored_artifacts)}."
    )
    assert stored_artifacts[0][0] == "file_test123"

    # Result JSON has the expected fields.
    assert parsed["file_id"] == "file_test123"
    assert parsed["filename"] == "chart.png"
    assert parsed["content_type"] == "image/png"


def test_upload_file_rejects_path_traversal(
    workspace: Path,
    tool_ctx: ToolContext,
) -> None:
    """
    Paths that escape the workspace via ``../`` are rejected.
    """
    tool = UploadFileTool()
    result = tool.invoke('{"path": "../../../etc/passwd"}', tool_ctx)

    # Must return an error, not a file_id.
    assert "Error" in result, f"Expected path traversal to be rejected. Got: {result}"
    assert "escapes" in result.lower() or "error" in result.lower()


def test_upload_file_rejects_missing_file(
    workspace: Path,
    tool_ctx: ToolContext,
) -> None:
    """
    Non-existent files return an error.
    """
    tool = UploadFileTool()
    result = tool.invoke('{"path": "nonexistent.txt"}', tool_ctx)

    assert "Error" in result
    assert "not found" in result.lower()


def test_upload_file_rejects_empty_path(
    tool_ctx: ToolContext,
) -> None:
    """
    Empty path returns an error.
    """
    tool = UploadFileTool()
    result = tool.invoke('{"path": ""}', tool_ctx)

    assert "Error" in result
    assert "empty" in result.lower()


def test_upload_file_rejects_invalid_arguments(tool_ctx: ToolContext) -> None:
    """Malformed and non-object JSON return tool errors instead of raising."""
    tool = UploadFileTool()
    malformed = tool.invoke("{", tool_ctx)
    non_object = tool.invoke("[]", tool_ctx)
    assert "Error" in malformed and "malformed JSON" in malformed
    assert "Error" in non_object and "JSON object" in non_object


@pytest.mark.parametrize("path", [123, True])
def test_upload_file_rejects_non_string_path(tool_ctx: ToolContext, path: object) -> None:
    """Non-string paths are rejected before path resolution."""
    tool = UploadFileTool()
    result = tool.invoke(json.dumps({"path": path}), tool_ctx)
    assert "Error" in result
    assert "non-empty string" in result


@pytest.mark.posix_only
def test_upload_file_refuses_directory_swapped_for_symlink_after_check(
    workspace: Path,
    tool_ctx: ToolContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A directory swapped for an escaping symlink after the containment check
    is refused before anything reaches the artifact store.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "deep.txt").write_text("host secret")
    stored: list[bytes] = []
    monkeypatch.setattr(
        "omnigent.runtime.get_file_store",
        lambda: SimpleNamespace(create=lambda **_: SimpleNamespace(id="file_1")),
    )
    monkeypatch.setattr(
        "omnigent.runtime.get_artifact_store",
        lambda: SimpleNamespace(put=lambda _key, data: stored.append(data)),
    )
    real_safe_resolve = upload_file.safe_resolve

    def _check_then_swap(path: str, root: Path) -> Path:
        resolved = real_safe_resolve(path, root)
        (workspace / "output").rename(workspace / "output.orig")
        (workspace / "output").symlink_to(outside)
        return resolved

    monkeypatch.setattr(upload_file, "safe_resolve", _check_then_swap)

    result = UploadFileTool().invoke('{"path": "output/deep.txt"}', tool_ctx)

    assert result.startswith("Error"), result
    assert stored == [], "upload_file stored a file read through the swapped symlink"


@pytest.mark.posix_only
@pytest.mark.parametrize("swapped", ["output", "output/deep.txt"])
def test_safe_open_refuses_path_swapped_for_symlink_after_check(
    workspace: Path,
    tmp_path: Path,
    swapped: str,
) -> None:
    """
    ``safe_open`` refuses a directory or file that became an escaping
    symlink after ``safe_resolve`` accepted the path.

    :param swapped: Workspace-relative entry replaced by the symlink.
    """
    outside = tmp_path / "outside"
    (outside / "output").mkdir(parents=True)
    (outside / "output" / "deep.txt").write_text("host secret")
    resolved = upload_file.safe_resolve("output/deep.txt", workspace)
    (workspace / swapped).rename(workspace / "moved")
    (workspace / swapped).symlink_to(outside / swapped)

    with pytest.raises(OSError):
        upload_file.safe_open(resolved, workspace)


@pytest.mark.posix_only
def test_safe_open_refuses_fifo_without_blocking(workspace: Path) -> None:
    """A FIFO is refused as not a regular file instead of blocking the open."""
    os.mkfifo(workspace / "pipe")
    resolved = upload_file.safe_resolve("pipe", workspace)

    with pytest.raises(ValueError, match="Not a regular file"):
        upload_file.safe_open(resolved, workspace)
