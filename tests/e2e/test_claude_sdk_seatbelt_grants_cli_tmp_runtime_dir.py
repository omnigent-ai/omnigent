"""The claude-sdk seatbelt profile must write-grant the Claude CLI's real
``/tmp/claude-<uid>`` runtime dir, or the CLI dies at connect with EPERM.

On macOS ``tempfile.gettempdir()`` is the per-user ``/var/folders/.../T``, so
a tempdir-anchored grant never covers the ``/tmp/claude-<uid>`` spelling the
CLI opens (kernel-canonical ``/private/tmp/claude-<uid>``). The profile
builder is platform-independent, so this runs on Linux CI with a macOS-shaped
tempdir and a kernel-style matcher that canonicalises ``/tmp`` and ``/var``
before comparing ``subpath``/``literal`` rules.
"""

from __future__ import annotations

# Warm the darwin-branching stdlib import before ``sys.platform`` is patched to
# "darwin" below, so nothing re-imports it through the macOS-only branch.
import ctypes.util  # noqa: F401
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest

import omnigent.inner.seatbelt_sandbox as sb
from omnigent.inner.claude_sdk_executor import (
    _claude_internal_write_files,
    _claude_internal_write_roots,
)
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.sandbox import (
    create_private_tmpdir,
    with_additional_read_roots,
    with_additional_write_files,
    with_additional_write_roots,
)
from omnigent.inner.seatbelt_sandbox import SeatbeltSandboxBackend, _build_profile

pytestmark = pytest.mark.skipif(
    not hasattr(os, "getuid"),
    reason="the Claude CLI's per-uid /tmp/claude-<uid> runtime dir is POSIX-only",
)

_WRITE_ALLOW_RE = re.compile(r'\(allow file-write\* \((subpath|literal) "((?:[^"\\]|\\.)*)"\)\)')


def _macos_canonical(path: str) -> str:
    """``/tmp``, ``/var`` and ``/etc`` are symlinks into ``/private`` on macOS."""
    for prefix in ("/tmp", "/var", "/etc"):
        if path == prefix or path.startswith(prefix + "/"):
            return "/private" + path
    return path


def _write_allows(profile: str) -> list[tuple[str, str]]:
    return [
        (kind, raw.replace('\\"', '"').replace("\\\\", "\\"))
        for kind, raw in _WRITE_ALLOW_RE.findall(profile)
    ]


def _kernel_write_allowed(profile: str, target: str) -> bool:
    """Model the deny-default kernel: does any ``file-write*`` allow cover *target*?"""
    canonical_target = _macos_canonical(target)
    for kind, path in _write_allows(profile):
        canonical = _macos_canonical(path).rstrip("/")
        if canonical_target == canonical:
            return True
        if kind == "subpath" and canonical_target.startswith(canonical + "/"):
            return True
    return False


@pytest.fixture
def macos_shaped_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make ``tempfile.gettempdir()`` a per-user ``/var/folders``-style dir."""
    per_user_tmp = tmp_path / "var" / "folders" / "zz" / "zzabc123def" / "T"
    per_user_tmp.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(per_user_tmp))
    for key in ("TMPDIR", "TMP", "TEMP", "TEMPDIR"):
        monkeypatch.setenv(key, str(per_user_tmp))
    return per_user_tmp


@pytest.fixture
def cli_runtime_dir() -> Iterator[Path]:
    """The CLI's real ``/tmp/claude-<uid>``, removed afterwards if this test created it."""
    runtime_dir = Path("/tmp") / f"claude-{os.getuid()}"
    existed = runtime_dir.exists()
    yield runtime_dir
    if not existed:
        shutil.rmtree(runtime_dir, ignore_errors=True)


def test_seatbelt_profile_grants_claude_cli_tmp_runtime_dir(
    tmp_path: Path, macos_shaped_tmpdir: Path, cli_runtime_dir: Path
) -> None:
    """Resolve the ``darwin_seatbelt`` policy as ``prepare_claude_cli_path`` does,
    add the CLI grants plus the launcher's scratch tmpdir, and check the rendered
    profile covers the CLI's runtime dir."""
    cwd = (tmp_path / "workspace").resolve()
    cwd.mkdir()
    spec = OSEnvSpec(
        type="caller_process",
        cwd=str(cwd),
        sandbox=OSEnvSandboxSpec(type="darwin_seatbelt", write_paths=["."], allow_network=True),
    )
    with (
        mock.patch.object(sb.sys, "platform", "darwin"),
        mock.patch.object(sb.shutil, "which", return_value="/usr/bin/sandbox-exec"),
    ):
        sandbox = SeatbeltSandboxBackend().resolve(spec, cwd)

    claude_roots = _claude_internal_write_roots()
    sandbox = with_additional_read_roots(sandbox, claude_roots)
    sandbox = with_additional_write_roots(sandbox, claude_roots)
    sandbox = with_additional_write_files(sandbox, _claude_internal_write_files())
    sandbox = with_additional_write_roots(sandbox, [create_private_tmpdir()])

    profile = _build_profile(sandbox, cwd)

    assert _kernel_write_allowed(profile, str(cli_runtime_dir)), (
        f"seatbelt profile does not write-grant the Claude CLI runtime dir {cli_runtime_dir}; "
        "the CLI's open() there is denied (EPERM) and the Claude SDK connect fails.\n"
        "file-write* allows:\n  " + "\n  ".join(f"({k} {p!r})" for k, p in _write_allows(profile))
    )
