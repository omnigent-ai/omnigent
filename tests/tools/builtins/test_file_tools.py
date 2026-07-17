"""Unit tests for list_files and download_file builtin tools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.download_file import DownloadFileTool
from omnigent.tools.builtins.list_files import ListFilesTool

# ── Stubs ─────────────────────────────────────────────────


@dataclass
class _FakeFile:
    """
    Minimal stub for StoredFile.

    :param id: File ID.
    :param filename: Original filename.
    :param bytes: File size.
    :param content_type: MIME type.
    :param created_at: Unix timestamp.
    :param session_id: Owning session/conversation id, or ``None``
        for global (unscoped) files.
    """

    id: str
    filename: str
    bytes: int
    content_type: str | None
    created_at: int
    session_id: str | None = None


@dataclass
class _FakePage:
    """
    Minimal stub for PagedList.

    :param data: List of items.
    :param has_more: Whether there are more pages.
    :param first_id: First item ID.
    :param last_id: Last item ID.
    """

    data: list[Any]
    has_more: bool = False
    first_id: str | None = None
    last_id: str | None = None


class _FakeFileStore:
    """
    Stub file store for testing.

    :param files: Pre-populated file records.
    """

    def __init__(self, files: list[_FakeFile] | None = None) -> None:
        self._files = {f.id: f for f in (files or [])}

    def list(
        self,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
        session_id: str | None = None,
        include_unscoped: bool = False,
    ) -> _FakePage:
        """
        Return files, optionally filtered by session_id.

        :param limit: Max results.
        :param after: Ignored in stub.
        :param before: Ignored in stub.
        :param order: Ignored in stub.
        :param session_id: When set, only return files belonging
            to this session. ``None`` returns all files.
        :param include_unscoped: When ``True`` and ``session_id``
            is set, also include global files (``session_id=None``).
        :returns: A page of files.
        """
        if session_id is not None:
            data = [
                f
                for f in self._files.values()
                if f.session_id == session_id or (include_unscoped and f.session_id is None)
            ]
        else:
            data = list(self._files.values())
        return _FakePage(data=data[:limit])

    def get(self, file_id: str) -> _FakeFile | None:
        """
        Look up a file by ID.

        :param file_id: The file ID.
        :returns: The file record, or None.
        """
        return self._files.get(file_id)


class _FakeArtifactStore:
    """
    Stub artifact store for testing.

    :param blobs: Pre-populated key → bytes mapping.
    """

    def __init__(self, blobs: dict[str, bytes] | None = None) -> None:
        self._blobs = dict(blobs or {})

    def get(self, key: str) -> bytes:
        """
        Retrieve blob by key.

        :param key: Artifact key.
        :returns: The blob bytes.
        :raises KeyError: If not found.
        """
        if key not in self._blobs:
            raise KeyError(key)
        return self._blobs[key]


@pytest.fixture()
def tool_ctx(tmp_path: Path) -> ToolContext:
    """
    ToolContext with a temporary workspace and conversation_id.

    :param tmp_path: Pytest temp directory.
    :returns: A ToolContext with workspace and conversation_id set.
    """
    return ToolContext(
        task_id="task_test",
        agent_id="agent_test",
        workspace=tmp_path,
        conversation_id="conv_alice",
    )


# ── list_files tests ─────────────────────────────────────


def test_list_files_returns_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tool_ctx: ToolContext,
) -> None:
    """
    list_files returns file metadata for session-owned files.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tool_ctx: Tool execution context.
    """
    files = [
        _FakeFile("file_1", "report.pdf", 1024, "application/pdf", 1000, session_id="conv_alice"),
        _FakeFile("file_2", "chart.png", 2048, "image/png", 2000, session_id="conv_alice"),
    ]
    monkeypatch.setattr(
        "omnigent.runtime.get_file_store",
        lambda: _FakeFileStore(files),
    )

    tool = ListFilesTool()
    result = json.loads(tool.invoke("{}", tool_ctx))

    assert len(result["files"]) == 2
    assert result["files"][0]["file_id"] == "file_1"
    assert result["files"][0]["filename"] == "report.pdf"
    assert result["files"][0]["bytes"] == 1024
    assert result["files"][0]["content_type"] == "application/pdf"
    assert result["files"][1]["file_id"] == "file_2"


def test_list_files_empty(
    monkeypatch: pytest.MonkeyPatch,
    tool_ctx: ToolContext,
) -> None:
    """
    list_files returns empty list when no files exist.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tool_ctx: Tool execution context.
    """
    monkeypatch.setattr(
        "omnigent.runtime.get_file_store",
        lambda: _FakeFileStore([]),
    )

    tool = ListFilesTool()
    result = json.loads(tool.invoke("{}", tool_ctx))

    assert result["files"] == []


def test_list_files_allows_empty_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tool_ctx: ToolContext,
) -> None:
    """
    list_files has no required arguments, so an empty argument
    string should behave like an empty JSON object.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tool_ctx: Tool execution context.
    """
    monkeypatch.setattr(
        "omnigent.runtime.get_file_store",
        lambda: _FakeFileStore(
            [
                _FakeFile(
                    "file_1",
                    "report.pdf",
                    1024,
                    "application/pdf",
                    1000,
                    session_id="conv_alice",
                ),
            ]
        ),
    )

    tool = ListFilesTool()
    result = json.loads(tool.invoke("", tool_ctx))

    assert result["files"][0]["file_id"] == "file_1"


@pytest.mark.parametrize("arguments", ["not-json", "[]"])
def test_list_files_rejects_malformed_arguments(
    arguments: str,
    tool_ctx: ToolContext,
) -> None:
    """
    Malformed or non-object arguments return a JSON error instead
    of raising out of the tool invocation.

    :param arguments: Raw tool argument string.
    :param tool_ctx: Tool execution context.
    """
    tool = ListFilesTool()
    result = json.loads(tool.invoke(arguments, tool_ctx))

    assert "error" in result


def test_list_files_respects_limit(
    monkeypatch: pytest.MonkeyPatch,
    tool_ctx: ToolContext,
) -> None:
    """
    list_files caps at the requested limit.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tool_ctx: Tool execution context.
    """
    files = [
        _FakeFile(f"file_{i}", f"f{i}.txt", 100, None, i, session_id="conv_alice")
        for i in range(50)
    ]
    monkeypatch.setattr(
        "omnigent.runtime.get_file_store",
        lambda: _FakeFileStore(files),
    )

    tool = ListFilesTool()
    result = json.loads(tool.invoke('{"limit": 5}', tool_ctx))

    assert len(result["files"]) == 5


def test_list_files_caps_limit_at_100(
    monkeypatch: pytest.MonkeyPatch,
    tool_ctx: ToolContext,
) -> None:
    """
    list_files clamps oversized limits to the documented maximum.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tool_ctx: Tool execution context.
    """
    files = [
        _FakeFile(f"file_{i}", f"f{i}.txt", 100, None, i, session_id="conv_alice")
        for i in range(150)
    ]
    monkeypatch.setattr(
        "omnigent.runtime.get_file_store",
        lambda: _FakeFileStore(files),
    )

    tool = ListFilesTool()
    result = json.loads(tool.invoke('{"limit": 150}', tool_ctx))

    assert len(result["files"]) == 100


@pytest.mark.parametrize("limit", [0, -1, "5", True])
def test_list_files_rejects_invalid_limit(
    limit: object,
    tool_ctx: ToolContext,
) -> None:
    """
    Invalid limits are rejected before they reach the store.

    :param limit: Invalid limit value to encode in the tool call.
    :param tool_ctx: Tool execution context.
    """
    tool = ListFilesTool()
    result = json.loads(tool.invoke(json.dumps({"limit": limit}), tool_ctx))

    assert "error" in result
    assert "limit" in result["error"]


def test_list_files_rejects_non_string_after(tool_ctx: ToolContext) -> None:
    """
    The pagination cursor must be a string file id.

    :param tool_ctx: Tool execution context.
    """
    tool = ListFilesTool()
    result = json.loads(tool.invoke('{"after": 123}', tool_ctx))

    assert result == {"error": "'after' must be a string"}


def test_list_files_excludes_other_sessions(
    monkeypatch: pytest.MonkeyPatch,
    tool_ctx: ToolContext,
) -> None:
    """
    list_files only returns files belonging to the calling
    conversation — files from other sessions are invisible.

    Regression test for file enumeration across sessions.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tool_ctx: Tool execution context with
        ``conversation_id="conv_alice"``.
    """
    files = [
        _FakeFile("file_a", "alice.txt", 100, "text/plain", 1000, session_id="conv_alice"),
        _FakeFile("file_b", "bob.txt", 200, "text/plain", 2000, session_id="conv_bob"),
        _FakeFile("file_g", "global.txt", 300, "text/plain", 3000, session_id=None),
    ]
    monkeypatch.setattr(
        "omnigent.runtime.get_file_store",
        lambda: _FakeFileStore(files),
    )

    tool = ListFilesTool()
    result = json.loads(tool.invoke("{}", tool_ctx))

    returned_ids = {f["file_id"] for f in result["files"]}
    # Alice's own file and global files are visible; Bob's is hidden.
    assert "file_a" in returned_ids
    assert "file_b" not in returned_ids, "Bob's file must not be visible to Alice"
    assert "file_g" in returned_ids, "Global (unscoped) files must be visible"


# ── download_file tests ──────────────────────────────────


def test_download_file_saves_to_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tool_ctx: ToolContext,
) -> None:
    """
    download_file retrieves content and writes it to the workspace.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tool_ctx: Tool execution context.
    """
    content = b"hello world"
    monkeypatch.setattr(
        "omnigent.runtime.get_file_store",
        lambda: _FakeFileStore(
            [
                _FakeFile(
                    "file_abc",
                    "hello.txt",
                    len(content),
                    "text/plain",
                    1000,
                    session_id="conv_alice",
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        "omnigent.runtime.get_artifact_store",
        lambda: _FakeArtifactStore({"file_abc": content}),
    )

    tool = DownloadFileTool()
    result = json.loads(tool.invoke('{"file_id": "file_abc"}', tool_ctx))

    assert result["filename"] == "hello.txt"
    assert result["bytes"] == 11
    assert result["content_type"] == "text/plain"

    saved = Path(result["path"])
    assert saved.exists()
    assert saved.read_bytes() == content
    assert saved.name == "hello.txt"


@pytest.mark.parametrize(
    "malicious_filename",
    [
        "../escape.txt",
        "../../escape.txt",
        "foo/../../bar.txt",
        "/etc/passwd",
        "/tmp/abs-escape.txt",
    ],
)
def test_download_file_confines_untrusted_filename_to_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tool_ctx: ToolContext,
    malicious_filename: str,
) -> None:
    """
    A malicious stored filename cannot write outside the workspace.

    The stored filename is untrusted metadata (persisted verbatim from
    whoever uploaded the file). Traversal sequences and absolute paths
    must be reduced to a basename and confined to the workspace, never
    escaping it.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tool_ctx: Tool execution context.
    :param malicious_filename: A traversal/absolute filename to reject.
    """
    content = b"payload"
    monkeypatch.setattr(
        "omnigent.runtime.get_file_store",
        lambda: _FakeFileStore(
            [
                _FakeFile(
                    "file_evil",
                    malicious_filename,
                    len(content),
                    "text/plain",
                    1000,
                    session_id="conv_alice",
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        "omnigent.runtime.get_artifact_store",
        lambda: _FakeArtifactStore({"file_evil": content}),
    )

    tool = DownloadFileTool()
    result = json.loads(tool.invoke('{"file_id": "file_evil"}', tool_ctx))

    # The write must land strictly inside the workspace.
    saved = Path(result["path"])
    workspace = tool_ctx.workspace
    assert workspace is not None
    assert saved.resolve().is_relative_to(workspace.resolve())
    # Nothing was written outside the workspace.
    assert not Path("/etc/passwd").is_symlink()
    assert saved.exists()
    assert saved.read_bytes() == content


def test_download_file_basenames_store_filename(
    monkeypatch: pytest.MonkeyPatch,
    tool_ctx: ToolContext,
) -> None:
    """
    A filename with leading directory components is saved by basename.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tool_ctx: Tool execution context.
    """
    content = b"report-bytes"
    monkeypatch.setattr(
        "omnigent.runtime.get_file_store",
        lambda: _FakeFileStore(
            [
                _FakeFile(
                    "file_nested",
                    "reports/2026/q2.csv",
                    len(content),
                    "text/csv",
                    1000,
                    session_id="conv_alice",
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        "omnigent.runtime.get_artifact_store",
        lambda: _FakeArtifactStore({"file_nested": content}),
    )

    tool = DownloadFileTool()
    result = json.loads(tool.invoke('{"file_id": "file_nested"}', tool_ctx))

    saved = Path(result["path"])
    workspace = tool_ctx.workspace
    assert workspace is not None
    assert saved.name == "q2.csv"
    assert saved.parent.resolve() == workspace.resolve()
    assert saved.read_bytes() == content


def test_download_file_not_found(
    monkeypatch: pytest.MonkeyPatch,
    tool_ctx: ToolContext,
) -> None:
    """
    download_file returns error for unknown file_id.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tool_ctx: Tool execution context.
    """
    monkeypatch.setattr(
        "omnigent.runtime.get_file_store",
        lambda: _FakeFileStore([]),
    )
    monkeypatch.setattr(
        "omnigent.runtime.get_artifact_store",
        lambda: _FakeArtifactStore({}),
    )

    tool = DownloadFileTool()
    result = json.loads(tool.invoke('{"file_id": "file_nope"}', tool_ctx))

    assert "error" in result
    assert "not found" in result["error"].lower()


@pytest.mark.parametrize("arguments", ["", "not-json", "[]"])
def test_download_file_rejects_invalid_arguments(
    arguments: str,
    tool_ctx: ToolContext,
) -> None:
    """
    Invalid raw arguments return a JSON error instead of raising.

    :param arguments: Raw tool argument string.
    :param tool_ctx: Tool execution context.
    """
    tool = DownloadFileTool()
    result = json.loads(tool.invoke(arguments, tool_ctx))

    assert "error" in result


@pytest.mark.parametrize("file_id", [123, True])
def test_download_file_rejects_non_string_file_id(
    file_id: object,
    tool_ctx: ToolContext,
) -> None:
    """
    ``file_id`` must be a string before querying the file store.

    :param file_id: Invalid file id value.
    :param tool_ctx: Tool execution context.
    """
    tool = DownloadFileTool()
    result = json.loads(tool.invoke(json.dumps({"file_id": file_id}), tool_ctx))

    assert result == {"error": "'file_id' must be a string"}


def test_download_file_missing_content(
    monkeypatch: pytest.MonkeyPatch,
    tool_ctx: ToolContext,
) -> None:
    """
    download_file returns error when metadata exists but content is missing.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tool_ctx: Tool execution context.
    """
    monkeypatch.setattr(
        "omnigent.runtime.get_file_store",
        lambda: _FakeFileStore(
            [
                _FakeFile(
                    "file_orphan",
                    "ghost.bin",
                    100,
                    None,
                    1000,
                    session_id="conv_alice",
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        "omnigent.runtime.get_artifact_store",
        lambda: _FakeArtifactStore({}),
    )

    tool = DownloadFileTool()
    result = json.loads(tool.invoke('{"file_id": "file_orphan"}', tool_ctx))

    assert "error" in result
    assert "content" in result["error"].lower()


def test_download_file_rejects_cross_session_file(
    monkeypatch: pytest.MonkeyPatch,
    tool_ctx: ToolContext,
) -> None:
    """
    download_file rejects a file that belongs to a different session.

    Regression test for cross-user file download via
    leaked file_id.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tool_ctx: Tool execution context with
        ``conversation_id="conv_alice"``.
    """
    content = b"secret data"
    monkeypatch.setattr(
        "omnigent.runtime.get_file_store",
        lambda: _FakeFileStore(
            [
                _FakeFile(
                    "file_bob",
                    "secret.txt",
                    len(content),
                    "text/plain",
                    1000,
                    session_id="conv_bob",
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        "omnigent.runtime.get_artifact_store",
        lambda: _FakeArtifactStore({"file_bob": content}),
    )

    tool = DownloadFileTool()
    result = json.loads(tool.invoke('{"file_id": "file_bob"}', tool_ctx))

    assert "error" in result
    assert "not found" in result["error"].lower()


def test_download_file_allows_global_file(
    monkeypatch: pytest.MonkeyPatch,
    tool_ctx: ToolContext,
) -> None:
    """
    download_file allows access to global (unscoped) files from
    any session.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tool_ctx: Tool execution context.
    """
    content = b"shared resource"
    monkeypatch.setattr(
        "omnigent.runtime.get_file_store",
        lambda: _FakeFileStore(
            [
                _FakeFile(
                    "file_global",
                    "shared.txt",
                    len(content),
                    "text/plain",
                    1000,
                    session_id=None,
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        "omnigent.runtime.get_artifact_store",
        lambda: _FakeArtifactStore({"file_global": content}),
    )

    tool = DownloadFileTool()
    result = json.loads(tool.invoke('{"file_id": "file_global"}', tool_ctx))

    assert "error" not in result
    assert result["filename"] == "shared.txt"
    assert result["bytes"] == len(content)
