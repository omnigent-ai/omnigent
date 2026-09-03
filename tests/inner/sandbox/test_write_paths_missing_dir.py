"""Regression: a ``write_paths`` grant is silently dropped when the
granted directory does not exist yet (linux_bwrap backend).

The user journey: declare ``sandbox.write_paths: ["docs/specs"]`` in an
agent's os_env spec against a workspace where ``docs/specs`` has not been
created yet, then have the agent write a file there. The grant should be
honored (or at minimum fail loud); instead the bwrap launcher emits the
write root as ``--bind-try``, which bubblewrap documents as *ignoring* a
missing source — so the mount silently never happens, cwd stays read-only,
and the agent's write fails with a permission error despite the explicit
grant. Nothing pre-creates the directory and no warning is logged.

Two layers, so the bug is pinned on any Linux host:

- ``test_missing_write_root_survives_into_bwrap_argv`` needs no user
  namespaces: it asserts the launcher does not hand bwrap a droppable
  ``--bind-try`` whose source is missing. Fails today everywhere on Linux.
- ``test_write_into_missing_granted_dir_succeeds`` drives the real helper
  under bwrap end-to-end (skips on hosts where bwrap cannot create a
  namespace, e.g. seccomp-restricted CI). Fails today wherever it runs.

``test_write_into_precreated_granted_dir_succeeds`` is the control: the
identical journey with the directory pre-created passes today, proving the
failure is specifically the missing-directory case.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import create_os_environment
from tests.inner.sandbox.conftest import _repo_root_for_pythonpath, run_async

_BWRAP = shutil.which("bwrap")

linux_bwrap_only = pytest.mark.skipif(
    not sys.platform.startswith("linux") or _BWRAP is None,
    reason="linux_bwrap requires Linux + bwrap on PATH",
)


def _bwrap_functional() -> bool:
    """Whether bwrap can actually create a namespace on this host.

    A seccomp-confined CI runner can have ``bwrap`` on PATH while the
    kernel denies unprivileged user namespaces; the runtime tests skip
    there instead of failing for a reason unrelated to the bug.
    """
    if _BWRAP is None or not sys.platform.startswith("linux"):
        return False
    try:
        proc = subprocess.run(
            [_BWRAP, "--ro-bind", "/", "/", "/bin/true"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _missing_dir_spec(workspace: Path) -> OSEnvSpec:
    """The reported journey's spec: a grant on a not-yet-existing dir."""
    return OSEnvSpec(
        type="caller_process",
        cwd=str(workspace),
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            # Repo root so the sandboxed helper can import omnigent.*.
            read_paths=[_repo_root_for_pythonpath()],
            write_paths=["docs/specs"],
            allow_network=False,
        ),
    )


@linux_bwrap_only
def test_missing_write_root_survives_into_bwrap_argv(tmp_path: Path) -> None:
    """A granted-but-missing write root must not be droppable by bwrap.

    ``--bind-try`` with a missing source is silently skipped by bwrap, so
    emitting the grant that way — without pre-creating the directory — is
    exactly the silent drop this bug reports. The grant survives iff the
    source directory exists by spawn time (backend pre-creates it) or the
    launcher uses a hard ``--bind`` that fails loud instead of silently.
    """
    from omnigent.inner.bwrap_sandbox import BwrapSandboxBackend

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "README.md").write_text("hi\n")

    backend = BwrapSandboxBackend()
    spec = _missing_dir_spec(workspace)
    policy = backend.resolve(spec, workspace)
    missing_root = (workspace / "docs" / "specs").resolve()
    assert missing_root in [p.resolve() for p in policy.write_roots], (
        "precondition: the grant must survive policy resolution"
    )

    argv = backend.wrap_launcher_argv(["/bin/true"], policy, workspace)

    bind_flag = None
    for i, arg in enumerate(argv):
        if arg in ("--bind", "--bind-try") and Path(argv[i + 1]) == missing_root:
            bind_flag = arg
            break
    assert bind_flag is not None, (
        f"write_paths grant for {missing_root} was dropped from the bwrap argv entirely"
    )
    if bind_flag == "--bind-try":
        assert missing_root.is_dir(), (
            "write_paths grant is silently dropped: the not-yet-existing "
            f"directory {missing_root} is emitted as --bind-try (bwrap "
            "ignores a missing source) and the backend never pre-creates "
            "it, so the helper gets no writable mount and no error"
        )


@pytest.mark.skipif(
    not _bwrap_functional(),
    reason="bwrap cannot create namespaces on this host",
)
def test_write_into_missing_granted_dir_succeeds(
    tmp_path: Path,
    sandbox_pythonpath_env: None,
) -> None:
    """End-to-end: the reported journey through the real sandboxed helper.

    With ``write_paths: ["docs/specs"]`` granted and the directory not yet
    created, the agent-visible write op must succeed — the grant says so.
    Today it fails: the bind is silently dropped, cwd is read-only, and
    the helper's ``mkdir docs`` hits the RO mount.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "README.md").write_text("hi\n")

    os_env = create_os_environment(_missing_dir_spec(workspace))
    try:
        result = run_async(
            os_env.write(str(workspace / "docs" / "specs" / "note.md"), "granted\n")
        )
    finally:
        os_env.close()

    assert "error" not in result, (
        f"write into granted docs/specs failed despite write_paths grant: {result}"
    )
    assert (workspace / "docs" / "specs" / "note.md").read_text() == "granted\n"


@pytest.mark.skipif(
    not _bwrap_functional(),
    reason="bwrap cannot create namespaces on this host",
)
def test_write_into_precreated_granted_dir_succeeds(
    tmp_path: Path,
    sandbox_pythonpath_env: None,
) -> None:
    """Control: the identical journey with the directory pre-created works.

    Passing today, this isolates the regression to the missing-directory
    case — if this control ever fails, the failure of the test above is
    environmental, not the missing-directory bug.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "README.md").write_text("hi\n")
    (workspace / "docs" / "specs").mkdir(parents=True)

    os_env = create_os_environment(_missing_dir_spec(workspace))
    try:
        result = run_async(
            os_env.write(str(workspace / "docs" / "specs" / "note.md"), "granted\n")
        )
    finally:
        os_env.close()

    assert "error" not in result, f"control write into pre-created docs/specs failed: {result}"
    assert (workspace / "docs" / "specs" / "note.md").read_text() == "granted\n"
