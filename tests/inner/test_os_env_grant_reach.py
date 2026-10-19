"""File-tool reach grants (#2070).

The runner-local file tools (``sys_os_read`` / ``sys_os_write`` /
``sys_os_edit``) are confined to the session workspace by
``_assert_within_reach``. These tests pin the security contract of the grant
extension:

- **Default unchanged:** with NO path grants declared, nothing outside cwd is
  reachable -- byte-for-byte the historical cwd-only confinement.
- A **read** grant admits reads only; it never confers write.
- A **write** grant (directory or single file) admits writes AND reads of that
  subtree (a writable path is readable), so ``edit`` works there.
- Symlink / ``..`` traversal cannot escape a grant into ungranted paths (the
  target is resolved before it is compared to the grant roots).
- Grants may be declared relative to cwd, and may target a single file.
- ``resolve_sandbox`` under ``sandbox.type: none`` carries these grants onto
  the (still-inactive) policy, and still rejects a network restriction.
"""

from __future__ import annotations

import errno
import os
import platform
from pathlib import Path
from unittest.mock import patch

import pytest

from omnigent.entities.environment_filesystem import PathUnreachable
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import _handle_helper_request, create_os_environment
from omnigent.inner.sandbox import (
    SandboxPolicy,
    contained_realpath,
    containment_prefix,
    is_unconfined,
    reachable_roots,
    resolve_sandbox,
)
from omnigent.runner.environment_filesystem import resolve_browse_target


def _grant_policy(
    *,
    read_roots: list[Path] | None = None,
    write_roots: tuple[Path, ...] | list[Path] = (),
    write_files: tuple[Path, ...] | list[Path] = (),
) -> SandboxPolicy:
    """Build an inactive (``type: none``) policy carrying resolved grants.

    Grant roots are canonicalised here exactly as ``resolve_sandbox`` does at
    resolve time, so ``_assert_within_reach``'s resolved-vs-resolved compare
    matches production.
    """
    return SandboxPolicy(
        backend_type="none",
        active=False,
        read_roots=(
            [p.resolve(strict=False) for p in read_roots] if read_roots is not None else None
        ),
        write_roots=[p.resolve(strict=False) for p in write_roots],
        write_files=[p.resolve(strict=False) for p in write_files],
        allow_network=True,
    )


def _req(op: str, path: Path, cwd: Path, policy: SandboxPolicy, **extra: object) -> dict:
    return _handle_helper_request(
        request={"op": op, "path": str(path), **extra},
        cwd=cwd,
        shell_path="/bin/sh",
        sandbox=policy,
    )


# ---------------------------------------------------------------------------
# Default-unchanged: no grants => cwd-only, exactly as before.
# ---------------------------------------------------------------------------


def test_no_grants_blocks_outside_path_for_every_op(tmp_path: Path) -> None:
    """With no grants declared, an absolute out-of-cwd path is blocked."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "f.txt"
    target.write_text("data")

    policy = _grant_policy()  # no grants at all

    read_res = _req("read", target, workspace, policy)
    assert "error" in read_res
    assert "outside the environment root" in read_res["error"]

    write_res = _req("write", target, workspace, policy, content="pwn")
    assert "error" in write_res
    assert "outside the environment root" in write_res["error"]

    edit_res = _req("edit", target, workspace, policy, oldText="data", newText="pwn")
    assert "error" in edit_res
    assert "outside the environment root" in edit_res["error"]

    # Nothing was written through the blocked ops.
    assert target.read_text() == "data"


# ---------------------------------------------------------------------------
# Read grant admits reads only -- never write.
# ---------------------------------------------------------------------------


def test_read_grant_permits_read_but_denies_write_and_edit(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    grant = tmp_path / "ro"
    grant.mkdir()
    target = grant / "f.txt"
    target.write_text("hello")

    policy = _grant_policy(read_roots=[grant])

    read_res = _req("read", target, workspace, policy)
    assert "error" not in read_res
    assert read_res["content"] == "hello"

    write_res = _req("write", target, workspace, policy, content="x")
    assert "error" in write_res
    assert "no sandbox write grant" in write_res["error"]

    edit_res = _req("edit", target, workspace, policy, oldText="hello", newText="x")
    assert "error" in edit_res
    assert "no sandbox write grant" in edit_res["error"]

    # The read-only grant really was read-only.
    assert target.read_text() == "hello"


def test_read_paths_are_directory_roots(tmp_path: Path) -> None:
    """``read_paths`` entries are directory ROOTS: everything under the root is
    readable, siblings outside it are not. (There is no ``read_files`` shape --
    a single readable file is expressed by rooting a ``read_paths`` entry at
    that file, where an exact-path match still succeeds; see the next test.)"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    grant = tmp_path / "ro"
    grant.mkdir()
    child = grant / "nested" / "f.txt"
    child.parent.mkdir()
    child.write_text("deep")
    sibling = tmp_path / "other.txt"
    sibling.write_text("no")

    policy = _grant_policy(read_roots=[grant])

    ok = _req("read", child, workspace, policy)
    assert ok.get("content") == "deep"

    blocked = _req("read", sibling, workspace, policy)
    assert "error" in blocked
    assert "no sandbox read grant" in blocked["error"]


def test_read_paths_entry_rooted_at_a_file_matches_only_that_file(tmp_path: Path) -> None:
    """A ``read_paths`` entry that resolves to a single file matches that exact
    file only (containment against a file root reduces to equality)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    granted = tmp_path / "single.txt"
    granted.write_text("hi")
    sibling = tmp_path / "other.txt"
    sibling.write_text("no")

    policy = _grant_policy(read_roots=[granted])

    ok = _req("read", granted, workspace, policy)
    assert ok.get("content") == "hi"

    blocked = _req("read", sibling, workspace, policy)
    assert "error" in blocked
    assert "no sandbox read grant" in blocked["error"]


# ---------------------------------------------------------------------------
# Write grant admits write + edit + read of the subtree.
# ---------------------------------------------------------------------------


def test_write_grant_permits_write_edit_and_read(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    grant = tmp_path / "rw"
    grant.mkdir()
    target = grant / "f.txt"
    target.write_text("hello")

    policy = _grant_policy(write_roots=[grant])

    # Writable implies readable.
    read_res = _req("read", target, workspace, policy)
    assert read_res.get("content") == "hello"

    write_res = _req("write", target, workspace, policy, content="new")
    assert "error" not in write_res
    assert target.read_text() == "new"

    target.write_text("hello")
    edit_res = _req("edit", target, workspace, policy, oldText="hello", newText="bye")
    assert "error" not in edit_res
    assert target.read_text() == "bye"


def test_active_policy_reads_workspace_and_write_grants(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    workspace_file = workspace / "workspace.txt"
    workspace_file.write_text("workspace")
    read_grant = tmp_path / "ro"
    read_grant.mkdir()
    write_grant = tmp_path / "rw"
    write_grant.mkdir()
    write_file = tmp_path / "single.txt"
    write_file.write_text("single")
    write_target = write_grant / "target.txt"
    write_target.write_text("target")
    policy = SandboxPolicy(
        backend_type="linux_bwrap",
        active=True,
        read_roots=[read_grant.resolve()],
        write_roots=[workspace.resolve(), write_grant.resolve()],
        write_files=[write_file.resolve()],
        allow_network=False,
    )

    assert _req("read", workspace_file, workspace, policy).get("content") == "workspace"
    assert _req("read", write_target, workspace, policy).get("content") == "target"
    assert _req("read", write_file, workspace, policy).get("content") == "single"


def test_write_files_grant_is_file_scoped(tmp_path: Path) -> None:
    """A single-file write grant covers exactly that file, not its siblings."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    granted = cfg / "a.json"
    granted.write_text("{}")
    sibling = cfg / "b.json"
    sibling.write_text("{}")

    policy = _grant_policy(write_files=[granted])

    write_res = _req("write", granted, workspace, policy, content='{"ok":1}')
    assert "error" not in write_res
    assert granted.read_text() == '{"ok":1}'

    # The granted file is also readable (write implies read).
    read_res = _req("read", granted, workspace, policy)
    assert "error" not in read_res

    # The sibling in the same directory is NOT granted -- blocked for both.
    blocked_write = _req("write", sibling, workspace, policy, content="pwn")
    assert "error" in blocked_write
    assert "no sandbox write grant" in blocked_write["error"]
    blocked_read = _req("read", sibling, workspace, policy)
    assert "error" in blocked_read
    assert "no sandbox read grant" in blocked_read["error"]
    assert sibling.read_text() == "{}"


# ---------------------------------------------------------------------------
# Traversal cannot escape a grant (resolve before compare).
# ---------------------------------------------------------------------------


def test_symlink_inside_grant_cannot_escape_grant(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    grant = tmp_path / "rw"
    grant.mkdir()
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    secret = secret_dir / "s.txt"
    secret.write_text("top")

    # A symlink that lives inside the grant but points OUTSIDE it.
    link = grant / "escape.txt"
    link.symlink_to(secret)

    policy = _grant_policy(write_roots=[grant])

    read_res = _req("read", link, workspace, policy)
    assert "error" in read_res
    assert "outside the environment root" in read_res["error"]

    write_res = _req("write", link, workspace, policy, content="pwn")
    assert "error" in write_res
    assert "outside the environment root" in write_res["error"]
    assert secret.read_text() == "top"


def test_dotdot_traversal_from_grant_cannot_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    grant = tmp_path / "rw"
    grant.mkdir()
    sibling_dir = tmp_path / "sibling"
    sibling_dir.mkdir()
    sibling = sibling_dir / "s.txt"
    sibling.write_text("safe")

    policy = _grant_policy(write_roots=[grant])

    # grant/../sibling/s.txt resolves to an ungranted path.
    traversal = grant / ".." / "sibling" / "s.txt"
    res = _req("write", traversal, workspace, policy, content="pwn")
    assert "error" in res
    assert "outside the environment root" in res["error"]
    assert sibling.read_text() == "safe"


# ---------------------------------------------------------------------------
# Delete is confined like write: parent resolved, leaf never followed.
# ---------------------------------------------------------------------------


_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
_SEARCH_ONLY_OPEN_SUPPORTED = hasattr(os, "O_PATH") or (
    hasattr(os, "O_SEARCH")
    and (platform.system() != "Darwin" or int(platform.mac_ver()[0].split(".")[0]) >= 13)
)


@pytest.fixture(params=["dir_fd", "read_only_dirs"])
def delete_mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """How the helper reaches the entry: the platform's directory descriptors
    (search-only on Linux), descriptors that need read permission (platforms
    without a search-only open)."""
    if request.param == "read_only_dirs":
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        monkeypatch.setattr("omnigent.inner.os_env._DIR_TRAVERSE_FLAGS", flags)
    return request.param


@pytest.fixture
def delete_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Workspace with links to a write grant and to an ungranted directory."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    grant = tmp_path / "rw"
    grant.mkdir()
    (grant / "granted.txt").write_text("granted")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_text("keep me")
    (workspace / "to_grant").symlink_to(grant)
    (workspace / "to_outside").symlink_to(outside)
    return workspace, grant, outside


def test_delete_follows_parents_only_into_write_reach(
    delete_layout: tuple[Path, Path, Path], delete_mode: str
) -> None:
    workspace, grant, outside = delete_layout
    policy = _grant_policy(write_roots=[grant])

    escaped = _req("delete", Path("to_outside/victim.txt"), workspace, policy)
    granted = _req("delete", Path("to_grant/granted.txt"), workspace, policy)

    assert escaped["code"] == "invalid_path"
    assert str(outside) not in escaped["error"]
    assert (outside / "victim.txt").read_text() == "keep me"
    assert granted == {"deleted": True, "type": "file", "bytes_deleted": len("granted")}
    assert not (grant / "granted.txt").exists()


@pytest.mark.parametrize("recursive", [False, True])
def test_delete_removes_a_leaf_link_not_its_target(
    delete_layout: tuple[Path, Path, Path], delete_mode: str, recursive: bool
) -> None:
    workspace, _grant, outside = delete_layout

    res = _req("delete", Path("to_outside"), workspace, _grant_policy(), recursive=recursive)

    assert res == {"deleted": True, "type": "symlink", "bytes_deleted": None}
    assert not (workspace / "to_outside").is_symlink()
    assert (outside / "victim.txt").read_text() == "keep me"


def test_delete_directory_needs_recursive_unless_empty(tmp_path: Path, delete_mode: str) -> None:
    (tmp_path / "empty").mkdir()
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "keep.txt").write_text("keep")
    policy = _grant_policy()

    emptied = _req("delete", Path("empty"), tmp_path, policy)
    refused = _req("delete", Path("full"), tmp_path, policy)
    kept = (tmp_path / "full" / "keep.txt").read_text()
    removed = _req("delete", Path("full"), tmp_path, policy, recursive=True)

    assert emptied == {"deleted": True, "type": "directory", "bytes_deleted": None}
    assert refused["code"] == "directory_not_empty"
    assert kept == "keep"
    assert removed["type"] == "directory"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(_IS_ROOT, reason="root bypasses permission checks")
@pytest.mark.parametrize("unreadable", ["parent", "grant_root"])
def test_delete_under_a_searchable_unreadable_directory(
    tmp_path: Path, delete_mode: str, unreadable: str
) -> None:
    """Search-only traversal works; a failed open must preserve the entry."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    if unreadable == "parent":
        locked = workspace / "locked"
        policy = _grant_policy()
    else:
        locked = tmp_path / "rw"
        (workspace / "locked").symlink_to(locked)
        policy = _grant_policy(write_roots=[locked])
    locked.mkdir()
    (locked / "victim.txt").write_text("x")
    locked.chmod(0o300)
    try:
        res = _req("delete", Path("locked/victim.txt"), workspace, policy)
    finally:
        locked.chmod(0o700)

    if delete_mode == "read_only_dirs" or not _SEARCH_ONLY_OPEN_SUPPORTED:
        assert res["code"] == "permission_denied"
        assert (locked / "victim.txt").read_text() == "x"
    else:
        assert res == {"deleted": True, "type": "file", "bytes_deleted": 1}
        assert list(locked.iterdir()) == []


@pytest.mark.parametrize("failure", ["unsupported", "root_open_denied", "parent_open_denied"])
@pytest.mark.parametrize("recursive", [False, True])
def test_delete_refuses_when_safe_traversal_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, recursive: bool
) -> None:
    """A full helper request must stop before removal when traversal fails."""
    from omnigent.inner import os_env

    workspace = tmp_path.resolve()
    parent = workspace / "parent"
    parent.mkdir()
    target = Path("parent/entry")
    kept = workspace / target
    if recursive:
        kept.mkdir()
        kept /= "keep.txt"
    kept.write_text("keep")

    monkeypatch.setattr(os_env, "_NOFOLLOW_DELETE_SUPPORTED", failure != "unsupported")
    real_open = os.open

    def open_directory(path: str | Path, flags: int, *, dir_fd: int | None = None) -> int:
        if (failure == "root_open_denied" and path == workspace) or (
            failure == "parent_open_denied" and path == "parent"
        ):
            raise PermissionError(errno.EACCES, "Directory open refused", str(path))
        return real_open(path, flags, dir_fd=dir_fd)

    with (
        patch.object(os, "open", side_effect=open_directory),
        patch.object(os_env, "_delete_leaf") as remove,
    ):
        res = _req("delete", target, workspace, _grant_policy(), recursive=recursive)

    remove.assert_not_called()
    assert res["code"] == "permission_denied"
    assert "deleted" not in res
    assert kept.read_text() == "keep"


@pytest.mark.skipif(_IS_ROOT, reason="root bypasses permission checks")
def test_delete_refused_by_permissions_is_not_reported_missing(
    tmp_path: Path, delete_mode: str
) -> None:
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    (sealed / "keep.txt").write_text("keep")
    sealed.chmod(0o500)
    try:
        res = _req("delete", Path("sealed/keep.txt"), tmp_path, _grant_policy())
    finally:
        sealed.chmod(0o700)

    assert res["code"] == "permission_denied"
    assert (sealed / "keep.txt").read_text() == "keep"


def test_delete_without_grants_refuses_a_link_into_a_read_grant(
    delete_layout: tuple[Path, Path, Path],
) -> None:
    workspace, _grant, outside = delete_layout
    policy = _grant_policy(read_roots=[outside])

    res = _req("delete", Path("to_outside/victim.txt"), workspace, policy)

    assert res["code"] == "invalid_path"
    assert (outside / "victim.txt").read_text() == "keep me"


def test_delete_refuses_the_root_reached_through_a_link(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "keep.txt").write_text("keep")
    (workspace / "up").symlink_to(tmp_path)
    policy = _grant_policy(write_roots=[tmp_path])

    res = _req("delete", Path("up/ws"), workspace, policy, recursive=True)

    assert res["code"] == "invalid_path"
    assert (workspace / "keep.txt").exists()


def test_delete_refuses_a_component_that_became_a_symlink(
    delete_layout: tuple[Path, Path, Path], delete_mode: str
) -> None:
    """The walk after the reach check does not follow a swapped-in link."""
    from omnigent.inner.os_env import _delete_beneath

    workspace, _grant, outside = delete_layout

    res = _delete_beneath(workspace, ("to_outside",), "victim.txt", recursive=False)

    assert res["code"] == "invalid_path"
    assert (outside / "victim.txt").read_text() == "keep me"


def test_delete_rejects_a_malformed_request(tmp_path: Path) -> None:
    policy = _grant_policy()

    assert _req("delete", Path("x"), tmp_path, policy, recursive="yes")["code"] == "invalid_path"
    assert _req("delete", Path("a/.."), tmp_path, policy)["code"] == "invalid_path"


# ---------------------------------------------------------------------------
# resolve_sandbox(type=none) grant plumbing.
# ---------------------------------------------------------------------------


def test_resolve_sandbox_none_default_declares_no_grants(tmp_path: Path) -> None:
    """No grants declared => policy carries no reach extension (default)."""
    cwd = tmp_path.resolve()
    spec = OSEnvSpec(type="caller_process", cwd=str(cwd), sandbox=OSEnvSandboxSpec(type="none"))
    policy = resolve_sandbox(spec, cwd)
    assert policy.active is False
    assert policy.read_roots is None
    assert policy.write_roots == []
    assert policy.write_files == []


def test_resolve_sandbox_none_populates_grants_relative_to_cwd(tmp_path: Path) -> None:
    cwd = (tmp_path / "ws").resolve()
    cwd.mkdir()
    spec = OSEnvSpec(
        type="caller_process",
        cwd=str(cwd),
        sandbox=OSEnvSandboxSpec(
            type="none",
            read_paths=["../sibling"],
            write_paths=["../sibling/out"],
            write_files=["../sibling/f.json"],
        ),
    )
    policy = resolve_sandbox(spec, cwd)
    assert policy.active is False
    assert policy.read_roots == [(cwd / ".." / "sibling").resolve()]
    assert policy.write_roots == [(cwd / ".." / "sibling" / "out").resolve()]
    assert policy.write_files == [(cwd / ".." / "sibling" / "f.json").resolve()]


def test_resolve_sandbox_none_still_rejects_network_restriction(tmp_path: Path) -> None:
    spec = OSEnvSpec(
        type="caller_process",
        cwd=str(tmp_path),
        sandbox=OSEnvSandboxSpec(type="none", allow_network=False),
    )
    with pytest.raises(ValueError, match="cannot restrict network"):
        resolve_sandbox(spec, tmp_path.resolve())


def test_declared_write_grant_enables_edit_outside_cwd_end_to_end(tmp_path: Path) -> None:
    """The #2070 scenario: a declared write grant lets ``edit`` reach a sibling
    checkout that would otherwise be blocked as outside the workspace root."""
    cwd = (tmp_path / "repo").resolve()
    cwd.mkdir()
    sibling = (tmp_path / "sibling").resolve()
    sibling.mkdir()
    target = sibling / "file.txt"
    target.write_text("alpha")

    spec = OSEnvSpec(
        type="caller_process",
        cwd=str(cwd),
        sandbox=OSEnvSandboxSpec(type="none", write_paths=["../sibling"]),
    )
    policy = resolve_sandbox(spec, cwd)

    res = _handle_helper_request(
        request={"op": "edit", "path": str(target), "oldText": "alpha", "newText": "beta"},
        cwd=cwd,
        shell_path="/bin/sh",
        sandbox=policy,
    )
    assert "error" not in res
    assert target.read_text() == "beta"

    # A DIFFERENT sibling that was never granted stays blocked.
    ungranted = (tmp_path / "other").resolve()
    ungranted.mkdir()
    other = ungranted / "x.txt"
    other.write_text("keep")
    blocked = _handle_helper_request(
        request={"op": "write", "path": str(other), "content": "pwn"},
        cwd=cwd,
        shell_path="/bin/sh",
        sandbox=policy,
    )
    assert "error" in blocked
    assert "outside the environment root" in blocked["error"]
    assert other.read_text() == "keep"


def test_inactive_policy_with_grants_survives_jsonable_round_trip(tmp_path: Path) -> None:
    """An inactive (``type: none``) policy now carries non-empty grants; the
    helper subprocess reconstructs the policy from JSON, so the grants must
    round-trip intact (else the reach guard would silently see no grants in the
    helper)."""
    read_root = (tmp_path / "ro").resolve()
    write_root = (tmp_path / "rw").resolve()
    write_file = (tmp_path / "rw" / "f.json").resolve()
    policy = _grant_policy(
        read_roots=[read_root], write_roots=[write_root], write_files=[write_file]
    )

    rebuilt = SandboxPolicy.from_jsonable(policy.to_jsonable())

    assert rebuilt.active is False
    assert rebuilt.read_roots == [read_root]
    assert rebuilt.write_roots == [write_root]
    assert rebuilt.write_files == [write_file]


def test_reachable_roots_lists_cwd_and_every_grant(tmp_path: Path) -> None:
    """``reachable_roots`` is the list ``_assert_within_reach`` enforces and the
    filesystem APIs advertise; it must name cwd plus each declared grant with
    the access and match-kind that enforcement applies."""
    cwd = (tmp_path / "ws").resolve()
    read_root = (tmp_path / "ro").resolve()
    write_root = (tmp_path / "rw").resolve()
    write_file = (tmp_path / "single.json").resolve()
    policy = _grant_policy(
        read_roots=[read_root], write_roots=[write_root], write_files=[write_file]
    )

    roots = reachable_roots(cwd, policy)

    assert [(r.path, r.access, r.origin, r.kind) for r in roots] == [
        (cwd, "write", "cwd", "tree"),
        (write_root, "write", "write_paths", "tree"),
        (write_file, "write", "write_files", "file"),
        (read_root, "read", "read_paths", "tree"),
    ]


def test_reachable_roots_with_no_grants_is_cwd_alone(tmp_path: Path) -> None:
    """The default-unchanged invariant, stated on the advertised list: with no
    grants the only reachable root is cwd, so the browser cannot offer more
    than the file tools allow."""
    cwd = (tmp_path / "ws").resolve()

    roots = reachable_roots(cwd, _grant_policy())

    assert [(r.path, r.access) for r in roots] == [(cwd, "write")]


def test_reachable_roots_not_widened_when_sandbox_inactive(tmp_path: Path) -> None:
    """``sandbox.type: none`` leaves the shell unconfined, but the FILE TOOLS
    stay confined. ``reachable_roots`` must not grow just because the policy is
    inactive -- widening it would silently hand ``sys_os_read`` the whole
    filesystem. Browsers consult ``is_unconfined`` instead."""
    cwd = (tmp_path / "ws").resolve()
    spec = OSEnvSpec(type="caller_process", cwd=".", sandbox=OSEnvSandboxSpec(type="none"))
    policy = resolve_sandbox(spec, cwd)

    assert is_unconfined(policy) is True
    assert [r.path for r in reachable_roots(cwd, policy)] == [cwd]


def test_reachable_root_contains_matches_enforcement_shape(tmp_path: Path) -> None:
    """A ``tree`` grant covers its subtree; a ``file`` grant covers exactly one
    path. This is the distinction ``_assert_within_reach`` relies on, so a
    sibling file must not ride in on a single-file grant."""
    write_file = (tmp_path / "dir" / "f.json").resolve()
    tree_root = (tmp_path / "dir").resolve()
    policy = _grant_policy(write_roots=[tree_root], write_files=[write_file])

    _cwd, tree, single = reachable_roots(tmp_path / "ws", policy)

    assert tree.contains(tree_root / "nested" / "deep.txt") is True
    assert single.contains(write_file) is True
    assert single.contains(write_file.parent / "sibling.json") is False


def test_is_unconfined_false_for_an_active_backend(tmp_path: Path) -> None:
    """An active bwrap / seatbelt policy IS confined, so a browser must not be
    told it can range over the filesystem."""
    policy = SandboxPolicy(
        backend_type="linux_bwrap",
        active=True,
        read_roots=None,
        write_roots=[tmp_path.resolve()],
        write_files=[],
        allow_network=True,
    )

    assert is_unconfined(policy) is False


def test_browse_target_allows_grants_but_not_beyond_when_confined(tmp_path: Path) -> None:
    """Browsing authorization reuses the same grants the file tools enforce. A
    CONFINED environment gets no widening: a path outside every grant is
    refused even though a human is asking."""
    cwd = (tmp_path / "ws").resolve()
    granted = (tmp_path / "data").resolve()
    granted.mkdir(parents=True)
    outside = (tmp_path / "elsewhere").resolve()
    outside.mkdir()
    roots = reachable_roots(cwd, _grant_policy(read_roots=[granted]))

    assert resolve_browse_target(str(granted), roots, unconfined=False) == granted

    with pytest.raises(PathUnreachable) as excinfo:
        resolve_browse_target(str(outside), roots, unconfined=False)
    assert str(cwd) in excinfo.value.reachable_roots


def test_browse_target_admits_reads_and_writes_when_unconfined(tmp_path: Path) -> None:
    """With no OS-level sandbox the co-resident shell can already read AND write
    anywhere, so permitting both beyond the grants grants no reach that was
    being withheld. The caller has also cleared the permission level that
    grants shell, so this is not a new capability for them either."""
    cwd = (tmp_path / "ws").resolve()
    outside = (tmp_path / "elsewhere").resolve()
    outside.mkdir(parents=True)
    roots = reachable_roots(cwd, _grant_policy())

    assert resolve_browse_target(str(outside), roots, unconfined=True) == outside
    assert resolve_browse_target(str(outside), roots, unconfined=True, need_write=True) == outside


def test_browse_target_never_widens_writes_when_confined(tmp_path: Path) -> None:
    """The invariant the unconfined widening must NOT leak into: a confined
    environment's grants are the whole story, and a read grant still never
    confers write — the human browsing does not get to exceed the sandbox."""
    cwd = (tmp_path / "ws").resolve()
    read_only = (tmp_path / "ro").resolve()
    read_only.mkdir(parents=True)
    outside = (tmp_path / "elsewhere").resolve()
    outside.mkdir()
    roots = reachable_roots(cwd, _grant_policy(read_roots=[read_only]))

    # Readable, but a read grant admits no write.
    assert resolve_browse_target(str(read_only), roots, unconfined=False) == read_only
    with pytest.raises(PathUnreachable):
        resolve_browse_target(str(read_only), roots, unconfined=False, need_write=True)

    # Ungranted entirely: neither read nor write.
    with pytest.raises(PathUnreachable):
        resolve_browse_target(str(outside), roots, unconfined=False, need_write=True)


def test_browse_target_resolves_traversal_before_authorizing(tmp_path: Path) -> None:
    """An absolute path carrying ``..`` is normalized and resolved BEFORE the
    grant comparison, so it cannot be pointed out of a grant by traversal."""
    cwd = (tmp_path / "ws").resolve()
    granted = (tmp_path / "data").resolve()
    granted.mkdir(parents=True)
    roots = reachable_roots(cwd, _grant_policy(read_roots=[granted]))

    escaping = str(granted / ".." / "elsewhere")

    with pytest.raises(PathUnreachable):
        resolve_browse_target(escaping, roots, unconfined=False)


@pytest.mark.asyncio
async def test_unconfined_write_inside_a_read_grant_still_succeeds(tmp_path: Path) -> None:
    """An unconfined environment can write anywhere its shell can -- including
    a path that happens to sit inside a declared READ grant.

    The routing decision must consider the access being requested, not just
    containment: a write landing in a read grant is not something the guarded
    helper will perform, so treating it as "covered" would deny a write the
    environment is otherwise allowed to make.
    """
    from omnigent.runner.environment_filesystem import CallerProcessFilesystem

    cwd = tmp_path / "ws"
    cwd.mkdir()
    read_only = tmp_path / "ro"
    read_only.mkdir()
    target = read_only / "note.txt"

    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(cwd),
            sandbox=OSEnvSandboxSpec(type="none", read_paths=[str(read_only)]),
        )
    )
    assert os_env is not None
    fs = CallerProcessFilesystem(os_env)

    result = await fs.write(str(target), b"written\n")

    assert result.created is True
    assert target.read_text() == "written\n"


def test_containment_prefix_appends_exactly_one_separator() -> None:
    """The trailing separator is what makes a prefix comparison sound, so it
    must be added exactly once — and never doubled on the filesystem root,
    which would turn "/" into "//" and match nothing."""
    assert containment_prefix("/data") == "/data" + os.sep
    assert containment_prefix(Path("/data")) == "/data" + os.sep
    assert containment_prefix(os.sep) == os.sep


def test_contained_realpath_refuses_a_sibling_sharing_a_prefix(tmp_path: Path) -> None:
    """The classic prefix bug: a boundary at ``/data`` must not admit
    ``/database``. Guarding with a trailing separator on both sides is exactly
    what prevents it -- and the boundary directory itself must still match."""
    root = (tmp_path / "data").resolve()
    root.mkdir()
    sibling = (tmp_path / "database").resolve()
    sibling.mkdir()
    prefix = containment_prefix(root)

    assert contained_realpath(str(sibling), prefix) is None
    assert contained_realpath(str(root), prefix) is not None


def test_contained_realpath_decides_on_the_symlink_target(tmp_path: Path) -> None:
    """A symlink INSIDE the boundary that points outside must be refused: the
    decision is made on the resolved target, not on the path as written."""
    root = (tmp_path / "ws").resolve()
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (root / "escape.txt").symlink_to(outside)
    prefix = containment_prefix(root)

    assert contained_realpath(str(root / "escape.txt"), prefix) is None
    assert contained_realpath(str(root / "ordinary.txt"), prefix) is not None


def test_contained_realpath_collapses_dotdot_before_deciding(tmp_path: Path) -> None:
    """``..`` is resolved before the comparison, so it cannot aim the final
    path out of the boundary that admitted the string it appeared in."""
    root = (tmp_path / "ws").resolve()
    root.mkdir()

    assert contained_realpath(str(root / ".." / "elsewhere"), containment_prefix(root)) is None


def test_contained_realpath_returns_an_ordinary_path(tmp_path: Path) -> None:
    """The trailing separator the comparison needs is an internal detail: it
    must not leak into the return, or every caller inherits a path shape that
    breaks string equality against the same path written normally."""
    root = (tmp_path / "ws").resolve()
    root.mkdir()
    target = root / "f.txt"
    target.write_text("x")

    got = contained_realpath(str(target), containment_prefix(root))

    assert got == str(target)
    assert not got.endswith(os.sep)


def test_contained_realpath_admits_a_symlink_loop_but_it_reaches_nothing(
    tmp_path: Path,
) -> None:
    """A symlink cycle inside the boundary is admitted, and that is safe.

    This is the ONE behavioural difference from the previous
    ``Path.resolve()`` + ``relative_to`` idiom: ``resolve()`` raised ``ELOOP``
    (refused at the check), whereas ``realpath`` returns the path unresolved,
    so containment admits it. Nothing escapes -- the cycle stays under the
    boundary and every syscall through it fails with ELOOP, so the refusal
    just happens at the read instead of at the check. Pinned so nobody
    "hardens" this back into a special case believing it was a hole.
    """
    root = (tmp_path / "ws").resolve()
    root.mkdir()
    (root / "a").symlink_to(root / "b")
    (root / "b").symlink_to(root / "a")

    admitted = contained_realpath(str(root / "a"), containment_prefix(root))

    assert admitted is not None
    assert admitted.startswith(str(root))
    with pytest.raises(OSError):
        (Path(admitted)).read_bytes()
