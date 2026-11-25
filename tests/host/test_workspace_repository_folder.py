"""The repository folder a Git request names: refused outside the workspace, else checked."""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from connectrpc.code import Code
from connectrpc.errors import ConnectError
from pyqwest import Client
from pyqwest.testing import ASGITransport

from omnigent.api.workspace.v1alpha1.workspace_connect import (
    WorkspaceServiceASGIApplication,
    WorkspaceServiceClient,
)
from omnigent.api.workspace.v1alpha1.workspace_pb2 import (
    CHANGE_STATUS_ADDED,
    CHANGE_STATUS_MODIFIED,
    COMPARISON_PROBLEM_NOT_A_REPOSITORY,
    ListBranchCommitsRequest,
    ListRepositoryRefsRequest,
    ListWorkingCopyChangesRequest,
    ReadBaseFileRequest,
    ResolveWorkingCopyBaseRequest,
)
from omnigent.host.workspace_git.repository_folder import repository_folder
from omnigent.host.workspace_service import WorkspaceService
from omnigent.workspace_fs import WorkspaceReader
from tests.host.test_workspace_git_runner import _git, _repository

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the layout needs symlinks")

_ZERO = "0" * 40
# Contents only the places outside the workspace hold.
_OUTSIDE_MARKER = "other-secret-contents"


def _outside(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{_OUTSIDE_MARKER}.txt").write_text(f"{_OUTSIDE_MARKER}\n")
    return path


def _layout(tmp_path: Path) -> Path:
    """A workspace holding a worktree, a nested repository, and links that lead astray."""
    workspace = _repository(tmp_path / "workspace")
    (workspace / "src").mkdir()
    (workspace / "src" / "b.txt").write_text("untracked\n")
    _git(workspace, "worktree", "add", "-q", "-b", "feature", ".worktrees/feature")
    feature = workspace / ".worktrees" / "feature"
    (feature / "a.txt").write_text("changed in the worktree\n")
    (feature / "new.txt").write_text("new\n")
    _repository(workspace / "vendor" / "lib")
    # A real folder, so the case-insensitive check is tested on case-sensitive disks too.
    (workspace / "vendor" / ".GIT").mkdir()
    other = _repository(_outside(tmp_path / "other"))
    # A repository inside the workspace whose config puts its working tree outside it.
    elsewhere = _repository(workspace / "elsewhere")
    _git(elsewhere, "config", "core.worktree", str(_outside(tmp_path / "outside-tree")))
    (workspace / "out").symlink_to("../other")
    (workspace / "out-chain").symlink_to("out-step")
    (workspace / "out-step").symlink_to("../other")
    (workspace / "up").symlink_to("..")
    (workspace / "into-git").symlink_to(".git")
    (workspace / "lib-git").symlink_to("vendor/lib/.git")
    (workspace / "worktree-link").symlink_to(".worktrees/feature")
    # A `.git` file inside the workspace that points at a repository outside it.
    (workspace / "forged").mkdir()
    (workspace / "forged" / ".git").write_text(f"gitdir: {other / '.git'}\n")
    return workspace


@pytest.fixture(scope="module")
def refusals_layout(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One layout for the refusals, which only read it."""
    return _layout(tmp_path_factory.mktemp("refusals"))


async def _refusal(workspace: Path, repository: str) -> str:
    with pytest.raises(ConnectError) as refused:
        await repository_folder(WorkspaceReader(workspace), repository)
    assert refused.value.code == Code.INVALID_ARGUMENT
    assert _OUTSIDE_MARKER not in refused.value.message
    if not Path(repository).is_absolute():
        assert str(workspace.parent) not in refused.value.message
    return refused.value.message


@pytest.mark.parametrize(
    ("repository", "reason"),
    [
        ("..", "isn't a folder in the workspace"),
        ("../other", "isn't a folder in the workspace"),
        ("src/../..", "isn't a folder in the workspace"),
        ("ABSOLUTE_OTHER", "isn't a folder in the workspace"),
        ("src\x00", "isn't a folder in the workspace"),
        ("out", "isn't a folder in the workspace"),
        ("out-chain", "isn't a folder in the workspace"),
        ("up", "isn't a folder in the workspace"),
        ("a.txt", "isn't a folder in the workspace"),
        ("missing", "isn't a folder in the workspace"),
        (".git", "is inside a Git directory"),
        ("vendor/.GIT", "is inside a Git directory"),
        (".git/refs", "is inside a Git directory"),
        ("into-git", "is inside a Git directory"),
        ("lib-git", "is inside a Git directory"),
        ("vendor/lib/.git", "is inside a Git directory"),
        (".worktrees/feature/.git", "isn't a folder in the workspace"),
        ("forged", "belongs to a repository outside the workspace"),
        ("elsewhere", "has its working tree outside the workspace"),
    ],
)
async def test_a_folder_outside_the_workspace_or_in_a_git_directory_is_refused(
    refusals_layout: Path, repository: str, reason: str
) -> None:
    """The refusal names only what the caller sent, and says why."""
    workspace = refusals_layout
    if repository == "ABSOLUTE_OTHER":
        repository = str(workspace.parent / "other")

    message = await _refusal(workspace, repository)

    assert message.endswith(reason)


async def test_a_nested_git_directory_aimed_at_the_repository_around_the_workspace_is_refused(
    tmp_path: Path,
) -> None:
    """A `.git` kept inside, whose core.worktree is the outer repository's tree, isn't the own."""
    outer = _repository(tmp_path / "outer")
    workspace = outer / "workspace"
    (workspace / "sub").mkdir(parents=True)
    _git(workspace / "sub", "init", "-q", "-b", "main")
    _git(workspace / "sub", "config", "core.worktree", str(outer))

    message = await _refusal(workspace, "sub")

    assert message.endswith("has its working tree outside the workspace")


async def test_the_workspace_root_and_its_own_folders_are_accepted(tmp_path: Path) -> None:
    """The root is checked as before; a worktree, link or nested repository carries its own."""
    workspace = _layout(tmp_path).resolve()
    reader = WorkspaceReader(workspace)

    root = await repository_folder(reader, "")
    feature = await repository_folder(reader, ".worktrees/feature")
    linked = await repository_folder(reader, "worktree-link")
    nested = await repository_folder(reader, "vendor/lib")
    plain = await repository_folder(reader, "src")

    assert root.path == workspace and root.repository is None
    assert feature.path == linked.path == workspace / ".worktrees" / "feature"
    assert feature.repository is not None
    assert feature.repository.top_level.resolve() == feature.path
    assert nested.repository is not None
    assert nested.repository.top_level.resolve() == workspace / "vendor" / "lib"
    # A plain folder belongs to the workspace's own repository.
    assert plain.repository is not None
    assert plain.repository.top_level.resolve() == workspace


async def test_a_workspace_that_is_a_linked_worktree_compares_its_own_folders(
    tmp_path: Path,
) -> None:
    """Its Git directory is outside it, but it's the workspace's own, so it's accepted."""
    main = _repository(tmp_path / "main")
    _git(main, "worktree", "add", "-q", "-b", "work", str(tmp_path / "work"))
    workspace = (tmp_path / "work").resolve()
    (workspace / "src").mkdir()

    folder = await repository_folder(WorkspaceReader(workspace), "src")

    assert folder.path == workspace / "src"
    assert folder.repository is not None
    assert folder.repository.common_directory.resolve() == (main / ".git").resolve()


async def test_a_folder_outside_any_repository_is_accepted_without_one(tmp_path: Path) -> None:
    """The comparison itself then answers that it isn't a repository."""
    workspace = tmp_path.resolve() / "plain"
    (workspace / "docs").mkdir(parents=True)

    folder = await repository_folder(WorkspaceReader(workspace), "docs")

    assert folder.path == workspace / "docs"
    assert folder.repository is None


def _client(workspace: Path) -> WorkspaceServiceClient:
    reader = WorkspaceReader(workspace)
    app = WorkspaceServiceASGIApplication(WorkspaceService(lambda _ctx: reader))
    return WorkspaceServiceClient("http://workspace.test", http_client=Client(ASGITransport(app)))


async def _read_base(client: WorkspaceServiceClient, repository: str) -> list[Any]:
    request = ReadBaseFileRequest(commit=_ZERO, path="a.txt", object=_ZERO, repository=repository)
    return [message async for message in client.read_base_file(request)]


_CALLS: dict[str, Callable[[WorkspaceServiceClient, str], Awaitable[Any]]] = {
    "ResolveWorkingCopyBase": lambda client, repository: client.resolve_working_copy_base(
        ResolveWorkingCopyBaseRequest(repository=repository)
    ),
    "ListWorkingCopyChanges": lambda client, repository: client.list_working_copy_changes(
        ListWorkingCopyChangesRequest(repository=repository)
    ),
    "ReadBaseFile": _read_base,
    "ListRepositoryRefs": lambda client, repository: client.list_repository_refs(
        ListRepositoryRefsRequest(repository=repository)
    ),
    "ListBranchCommits": lambda client, repository: client.list_branch_commits(
        ListBranchCommitsRequest(repository=repository)
    ),
}


@pytest.mark.parametrize("method", list(_CALLS))
@pytest.mark.parametrize(
    "repository",
    ["..", "ABSOLUTE_OTHER", "out", "a.txt", ".git", "into-git", "forged", "elsewhere"],
)
async def test_every_git_method_refuses_a_folder_outside_the_workspace(
    refusals_layout: Path, method: str, repository: str
) -> None:
    """Each request is refused before its comparison runs, and shows nothing from outside."""
    if repository == "ABSOLUTE_OTHER":
        repository = str(refusals_layout.parent / "other")

    with pytest.raises(ConnectError) as refused:
        await _CALLS[method](_client(refusals_layout), repository)

    assert refused.value.code == Code.INVALID_ARGUMENT
    assert _OUTSIDE_MARKER not in refused.value.message


async def test_a_worktree_inside_the_workspace_is_compared_on_its_own(tmp_path: Path) -> None:
    """Its changes, base, refs and base files are the worktree's, its paths relative to it."""
    workspace = _layout(tmp_path)
    feature = workspace / ".worktrees" / "feature"
    client = _client(workspace)

    for repository in (".worktrees/feature", "worktree-link", ".worktrees/feature/"):
        changes = await client.list_working_copy_changes(
            ListWorkingCopyChangesRequest(repository=repository)
        )
        statuses = {file.path: file.status for file in changes.files}
        assert statuses == {"a.txt": CHANGE_STATUS_MODIFIED, "new.txt": CHANGE_STATUS_ADDED}
        assert changes.base.head == _git(feature, "rev-parse", "HEAD").strip()

    base = await client.resolve_working_copy_base(
        ResolveWorkingCopyBaseRequest(repository=".worktrees/feature")
    )
    refs = await client.list_repository_refs(
        ListRepositoryRefsRequest(repository=".worktrees/feature")
    )
    commits = await client.list_branch_commits(
        ListBranchCommitsRequest(repository=".worktrees/feature", base_revision="main")
    )
    blob = _git(feature, "rev-parse", "HEAD:a.txt").strip()
    read = [
        message
        async for message in client.read_base_file(
            ReadBaseFileRequest(
                commit=base.base.head, path="a.txt", object=blob, repository=".worktrees/feature"
            )
        )
    ]

    assert {"main", "feature"} <= set(refs.refs)
    assert not commits.base.HasField("unavailable")
    assert b"".join(message.data for message in read) == b"a\n"


async def test_a_nested_repository_and_a_plain_folder_are_compared(tmp_path: Path) -> None:
    """A repository stored inside the workspace is its own; a plain folder lists beneath it."""
    workspace = _layout(tmp_path)
    client = _client(workspace)
    (workspace / "vendor" / "lib" / "a.txt").write_text("library, edited\n")

    nested = await client.list_working_copy_changes(
        ListWorkingCopyChangesRequest(repository="vendor/lib")
    )
    folder = await client.list_working_copy_changes(
        ListWorkingCopyChangesRequest(repository="src")
    )

    assert [file.path for file in nested.files] == ["a.txt"]
    assert nested.base.head == _git(workspace / "vendor" / "lib", "rev-parse", "HEAD").strip()
    assert [file.path for file in folder.files] == ["b.txt"]
    assert folder.base.head == _git(workspace, "rev-parse", "HEAD").strip()


async def test_a_plain_folder_outside_any_repository_answers_not_a_repository(
    tmp_path: Path,
) -> None:
    """Without Git, a confined folder is still answered with the comparison's own reason."""
    workspace = tmp_path / "plain"
    (workspace / "docs").mkdir(parents=True)

    changes = await _client(workspace).list_working_copy_changes(
        ListWorkingCopyChangesRequest(repository="docs")
    )

    assert changes.base.unavailable.problem == COMPARISON_PROBLEM_NOT_A_REPOSITORY
