"""Checks that the claude-sdk seatbelt profile write-grants the Claude CLI's fixed
runtime root when the system temp dir differs from it (macOS: ``/var/folders/.../T``),
and that the grant is emitted at the root's resolved path when that root is a
symlink (macOS ``/tmp`` -> ``/private/tmp``), the spelling the kernel matches.
"""

from __future__ import annotations

# Warm the darwin-branching stdlib import before ``sys.platform`` is patched to
# "darwin" below, so nothing re-imports it through the macOS-only branch.
import ctypes.util  # noqa: F401
import os
import re
import tempfile
from pathlib import Path
from unittest import mock

import pytest

import omnigent.inner.seatbelt_sandbox as sb
from omnigent._platform import stable_user_id
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


def _write_allows(profile: str) -> list[tuple[str, str]]:
    return [
        (kind, raw.replace('\\"', '"').replace("\\\\", "\\"))
        for kind, raw in _WRITE_ALLOW_RE.findall(profile)
    ]


@pytest.fixture
def macos_shaped_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make ``tempfile.gettempdir()`` a per-user ``/var/folders``-style dir."""
    per_user_tmp = tmp_path / "var" / "folders" / "zz" / "zzabc123def" / "T"
    per_user_tmp.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(per_user_tmp))
    for key in ("TMPDIR", "TMP", "TEMP", "TEMPDIR"):
        monkeypatch.setenv(key, str(per_user_tmp))
    return per_user_tmp


def test_seatbelt_profile_grants_claude_cli_tmp_runtime_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, macos_shaped_tmpdir: Path
) -> None:
    """Resolve the ``darwin_seatbelt`` policy as ``prepare_claude_cli_path`` does,
    add the CLI grants plus the launcher's scratch tmpdir, and check the rendered
    profile covers the CLI's runtime dir at its resolved path."""
    # Keep the ~/.claude and ~/.npm grants the helper creates out of the real home.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Test-owned stand-in for /tmp -> /private/tmp: the alias is the spelling the
    # CLI opens, the resolved path is what the kernel matches.
    real_root = tmp_path / "private" / "tmp"
    real_root.mkdir(parents=True)
    alias_root = tmp_path / "tmp"
    alias_root.symlink_to(real_root)
    runtime_leaf = f"claude-{stable_user_id()}"

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

    with mock.patch("omnigent.inner.claude_sdk_executor._CLAUDE_CLI_TMP_ROOT", alias_root):
        claude_roots = _claude_internal_write_roots()
    sandbox = with_additional_read_roots(sandbox, claude_roots)
    sandbox = with_additional_write_roots(sandbox, claude_roots)
    sandbox = with_additional_write_files(sandbox, _claude_internal_write_files())
    sandbox = with_additional_write_roots(sandbox, [create_private_tmpdir()])

    profile = _build_profile(sandbox, cwd)

    allows = _write_allows(profile)
    resolved_runtime_dir = str((real_root / runtime_leaf).resolve(strict=False))
    assert ("subpath", resolved_runtime_dir) in allows, (
        "seatbelt profile does not write-grant the Claude CLI runtime dir "
        f"{alias_root / runtime_leaf} (resolved {resolved_runtime_dir}); the CLI's open() "
        "there is denied (EPERM) and the Claude SDK connect fails.\nfile-write* allows:\n  "
        + "\n  ".join(f"({k} {p!r})" for k, p in allows)
    )
    # The kernel canonicalises the accessed path before matching, so a rule spelled
    # through the symlink would never match.
    alias_prefix = str(alias_root) + "/"
    assert not any(path.startswith(alias_prefix) for _, path in allows), allows
