"""Active-sandbox file tools must not reject the writable workspace.

With an ACTIVE ``linux_bwrap`` sandbox that declares the workspace
writable (``write_paths: ["."]``) plus at least one external
``read_paths`` grant, the file-tool read gate must not consult only
``read_roots`` and reject ``sys_os_read`` (and therefore ``sys_os_edit``,
which reads first) for normal files inside the session workspace —
``sys_os_write`` and sandboxed shell commands reach them, and a writable
path must also be readable. The same gate must also admit reads of an
exact ``write_files`` grant. Finally, declaring ``"."`` in ``read_paths``
alongside a writable workspace must not layer a later read-only bind over
the writable cwd bind in the bwrap argv, which would remount the whole
workspace read-only.

These tests drive the real ``linux_bwrap`` resolver plus the real helper
request handler — the exact code ``sys_os_read`` / ``sys_os_write`` /
``sys_os_edit`` execute — so they fail for the reported reason and pass
once writable grants admit reads and the cwd bind is not overmounted.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import _handle_helper_request
from omnigent.inner.sandbox import SandboxPolicy, _get_backend

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bwrap") is None,
    reason="requires Linux with bubblewrap (bwrap) installed",
)

_BIND_OPTS = ("--bind", "--bind-try", "--ro-bind", "--ro-bind-try")
_WRITABLE_BIND_OPTS = ("--bind", "--bind-try")


def _resolve_policy(
    cwd: Path,
    *,
    write_paths: list[str],
    read_paths: list[str] | None = None,
    write_files: list[str] | None = None,
) -> SandboxPolicy:
    """Resolve an active ``linux_bwrap`` policy exactly as production does."""
    spec = OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            write_paths=write_paths,
            read_paths=read_paths,
            write_files=write_files,
        ),
    )
    return _get_backend("linux_bwrap").resolve(spec, cwd)


def _req(op: str, path: Path, cwd: Path, policy: SandboxPolicy, **extra: object) -> dict:
    """Issue one file-tool helper request through the production handler."""
    return _handle_helper_request(
        request={"op": op, "path": str(path), **extra},
        cwd=cwd,
        shell_path="/bin/sh",
        sandbox=policy,
    )


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.txt").write_text("hello workspace\n")
    return ws


@pytest.fixture()
def external(tmp_path: Path) -> Path:
    ext = tmp_path / "cache"
    ext.mkdir()
    (ext / "cached.txt").write_text("external cache\n")
    return ext


def test_workspace_read_succeeds_despite_external_read_grant(
    workspace: Path, external: Path, tmp_path: Path
) -> None:
    """A writable workspace stays readable when external read_paths exist.

    Declaring any external ``read_paths`` arms the read gate; it must not
    then check only ``read_roots`` and reject in-cwd reads with
    ``Read access ... is blocked by sandbox``.
    """
    policy = _resolve_policy(workspace, write_paths=["."], read_paths=[str(external)])
    assert policy.active

    res = _req("read", workspace / "notes.txt", workspace, policy)
    assert "error" not in res, f"workspace read rejected: {res}"
    assert res["content"] == "hello workspace\n"

    # The external read grant keeps working.
    ext_res = _req("read", external / "cached.txt", workspace, policy)
    assert ext_res.get("content") == "external cache\n"

    # Security invariant: paths outside every grant stay blocked.
    secret = tmp_path / "secret.txt"
    secret.write_text("no")
    blocked = _req("read", secret, workspace, policy)
    assert "error" in blocked
    assert "no sandbox read grant" in blocked["error"]


def test_workspace_edit_succeeds_despite_external_read_grant(
    workspace: Path, external: Path
) -> None:
    """Edit (a read-then-write) works on workspace files.

    ``sys_os_edit`` reads the file first, so a broken read gate rejects
    edits of the writable workspace too.
    """
    policy = _resolve_policy(workspace, write_paths=["."], read_paths=[str(external)])

    res = _req(
        "edit",
        workspace / "notes.txt",
        workspace,
        policy,
        oldText="hello",
        newText="HELLO",
    )
    assert "error" not in res, f"workspace edit rejected: {res}"
    assert (workspace / "notes.txt").read_text() == "HELLO workspace\n"

    # Writes were never affected; pin that as a control.
    wrote = _req("write", workspace / "notes2.txt", workspace, policy, content="x")
    assert "error" not in wrote
    assert (workspace / "notes2.txt").read_text() == "x"


def test_write_files_grant_is_readable_and_editable(workspace: Path, tmp_path: Path) -> None:
    """An exact ``write_files`` grant admits reads and edits, not just writes.

    With the read gate armed by an unrelated ``read_paths`` entry, the
    exact-file write grant must not end up writable but unreadable.
    """
    external_dir = tmp_path / "ext"
    external_dir.mkdir()
    grant_file = external_dir / "state.json"
    grant_file.write_text('{"n": 1}')

    policy = _resolve_policy(
        workspace,
        write_paths=["."],
        read_paths=[str(external_dir / "elsewhere")],  # unrelated grant arms the gate
        write_files=[str(grant_file)],
    )

    wrote = _req("write", grant_file, workspace, policy, content='{"n": 2}')
    assert "error" not in wrote, f"write_files write rejected: {wrote}"

    read_back = _req("read", grant_file, workspace, policy)
    assert "error" not in read_back, f"write_files grant unreadable: {read_back}"
    assert read_back["content"] == '{"n": 2}'

    edited = _req("edit", grant_file, workspace, policy, oldText='{"n": 2}', newText='{"n": 3}')
    assert "error" not in edited, f"write_files grant uneditable: {edited}"
    assert grant_file.read_text() == '{"n": 3}'


def test_cwd_read_grant_does_not_remount_workspace_read_only(
    workspace: Path, external: Path
) -> None:
    """Adding ``"."`` to read_paths must not overmount the writable cwd bind.

    Bwrap layers later mounts over earlier ones: emitting the cwd's
    writable ``--bind`` and then a read-only bind of the same path makes
    the later mount win, so the whole workspace would become read-only for
    sandboxed commands.
    """
    policy = _resolve_policy(workspace, write_paths=["."], read_paths=[".", str(external)])
    backend = _get_backend("linux_bwrap")
    argv = backend.wrap_launcher_argv(["/usr/bin/true"], policy, workspace)

    ws_resolved = workspace.resolve()
    mounts: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] in _BIND_OPTS and i + 2 < len(argv):
            if Path(argv[i + 2]) == ws_resolved:
                mounts.append(argv[i])
            i += 3
        else:
            i += 1

    assert mounts, f"workspace {ws_resolved} is never bind-mounted: {argv}"
    assert mounts[-1] in _WRITABLE_BIND_OPTS, (
        "a read-only bind is layered over the writable cwd bind, remounting "
        f"the whole workspace read-only (workspace mounts in order: {mounts})"
    )
